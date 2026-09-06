import time

import pytest

from session_search.core.embeddings import EmbeddingIdentity, LocalEmbedder
from session_search.core.local_worker import IsolatedLocalEmbedder


class Process:
    def __init__(self):
        self.alive = True
        self.closed = False

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.alive = False

    def join(self, timeout):
        pass

    def close(self):
        self.closed = True


class Connection:
    def __init__(self):
        self.ready = False
        self.requests = []
        self.closed = False

    def send(self, request):
        self.requests.append(request)

    def poll(self, timeout):
        return self.ready

    def recv(self):
        self.ready = False
        return ('ok', [0.6, 0.8])

    def close(self):
        self.closed = True


def test_timed_out_work_remains_single_and_can_be_reused(tmp_path):
    worker = IsolatedLocalEmbedder(LocalEmbedder(tmp_path / 'model', EmbeddingIdentity('test', dimensions=2)))
    worker.process, worker.connection = Process(), Connection()
    connection = worker.connection
    try:
        with pytest.raises(TimeoutError):
            worker.embed('first')
        with pytest.raises(TimeoutError, match='busy'):
            worker.embed('second')
        assert connection.requests == [('first', False)]
        connection.ready = True
        assert worker.embed('first') == [0.6, 0.8]
        assert len(connection.requests) == 1
        with pytest.raises(TimeoutError):
            worker.embed('second')
        worker.pending_started = time.monotonic() - 61
        with pytest.raises(TimeoutError, match='execution limit'):
            worker.embed('third')
        assert worker.process is None and connection.closed
    finally:
        worker.close()


def test_missing_model_and_busy_worker_do_not_start_process(tmp_path):
    worker = IsolatedLocalEmbedder(LocalEmbedder(tmp_path / 'missing', EmbeddingIdentity('test')))
    with pytest.raises(FileNotFoundError):
        worker.embed('query')
    assert worker.process is None
    with worker.lock:
        with pytest.raises(TimeoutError, match='busy'):
            worker.embed('query')
    with pytest.raises(ValueError, match='IPC limit'):
        worker.embed('x' * 32769)
    worker.close()
