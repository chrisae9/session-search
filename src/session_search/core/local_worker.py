"""One isolated native model process, with bounded admission and query waits."""

import atexit
from dataclasses import asdict
import multiprocessing
import os
import threading
import time
from pathlib import Path

from session_search.core.embeddings import EmbeddingIdentity, LocalEmbedder


def _serve(connection, model_path, identity):
    # Native initialization redirects process-wide output. Keep it away from the
    # parent's MCP transport even if the underlying library prints unexpectedly.
    with open(os.devnull, 'wb') as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    provider = LocalEmbedder(Path(model_path), EmbeddingIdentity(**identity))
    try:
        while True:
            request = connection.recv()
            if request is None:
                return
            text, query = request
            try:
                connection.send(('ok', provider.embed(text, query=query)))
            except Exception:
                # No transcript-bearing exception or native diagnostic crosses IPC.
                connection.send(('error', None))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class IsolatedLocalEmbedder:
    def __init__(self, provider: LocalEmbedder, *, timeout: float = 2):
        self.identity = provider.identity
        self.model_path = provider.model_path
        self.timeout = timeout
        self.lock = threading.Lock()
        self.connection = None
        self.process = None
        self.pending = None
        self.pending_started = None
        atexit.register(self.close)

    def _start(self):
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=_serve,
            args=(child, str(self.model_path), asdict(self.identity)), daemon=True)
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            raise
        child.close()
        self.connection, self.process = parent, process

    def embed(self, text: str, *, query: bool = False):
        # Bound the pipe message as well as the number of outstanding requests.
        if len(text.encode('utf-8')) > 32768:
            raise ValueError('local inference request exceeds IPC limit')
        if not self.lock.acquire(blocking=False):
            raise TimeoutError('local inference is busy')
        try:
            if self.process is None:
                if not self.model_path.is_file():
                    raise FileNotFoundError('provision the local embedding model explicitly')
                self._start()
            if not self.process.is_alive():
                self._stop()
                raise OSError('local inference worker exited')
            request = (text, query)
            if self.pending is not None:
                if not self.connection.poll(0):
                    if time.monotonic() - self.pending_started > 60:
                        self._stop()
                        raise TimeoutError('local inference worker exceeded its execution limit')
                    raise TimeoutError('local inference is busy')
                result = self.connection.recv()
                previous, self.pending = self.pending, None
                if previous == request:
                    return self._result(result)
            self.connection.send(request)
            self.pending = request
            self.pending_started = time.monotonic()
            if not self.connection.poll(self.timeout):
                raise TimeoutError('local inference deadline exceeded')
            result = self.connection.recv()
            self.pending = None
            return self._result(result)
        except (EOFError, BrokenPipeError):
            self._stop()
            raise OSError('local inference worker exited') from None
        finally:
            self.lock.release()

    @staticmethod
    def _result(result):
        if result[0] != 'ok':
            raise ValueError('local inference failed')
        return result[1]

    def _stop(self):
        if self.connection is not None:
            self.connection.close()
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=1)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=1)
            self.process.close()
        self.connection = self.process = self.pending = None

    def close(self):
        with self.lock:
            self._stop()
