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
    worker = IsolatedLocalEmbedder(LocalEmbedder(tmp_path / 'missing', EmbeddingIdentity('test')), timeout=0.01)
    with pytest.raises(FileNotFoundError):
        worker.embed('query')
    assert worker.process is None
    with worker.lock:
        with pytest.raises(TimeoutError, match='busy'):
            worker.embed('query')
    with pytest.raises(ValueError, match='IPC limit'):
        worker.embed('x' * 32769)
    worker.close()


def test_startup_and_warm_budgets_share_pending_wait(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('session_search.core.local_worker.time.monotonic', lambda: clock[0])
    worker = IsolatedLocalEmbedder(LocalEmbedder(tmp_path / 'model', EmbeddingIdentity('test')))
    worker.process, worker.connection = Process(), Connection()
    waits = []

    def poll(timeout):
        waits.append(timeout)
        clock[0] += 3
        return True

    worker.connection.poll = poll
    try:
        worker.embed('cold')
        assert waits == [30]
        worker.pending = ('previous', False)
        worker.pending_started = clock[0]
        worker.embed('warm')
        assert waits == [30, 10, 7]
        assert worker.warm
        worker._stop()
        assert not worker.warm
    finally:
        worker.close()


def test_busy_request_waits_for_lock_without_starting_another_worker(tmp_path):
    import threading
    worker = IsolatedLocalEmbedder(LocalEmbedder(tmp_path / 'model', EmbeddingIdentity('test')), timeout=1)
    worker.process, worker.connection = Process(), Connection()
    worker.connection.ready = True
    worker.lock.acquire()
    timer = threading.Timer(0.05, worker.lock.release)
    timer.start()
    try:
        assert worker.embed('waiting') == [0.6, 0.8]
        assert worker.connection.requests == [('waiting', False)]
    finally:
        timer.join()
        worker.close()


@pytest.mark.parametrize('warm,reason', [(False, 'local_startup_timeout'), (True, 'local_query_timeout')])
def test_timeout_reports_startup_or_query(tmp_path, warm, reason):
    worker = IsolatedLocalEmbedder(LocalEmbedder(tmp_path / 'model', EmbeddingIdentity('test')))
    worker.process, worker.connection = Process(), Connection()
    worker.warm = warm
    try:
        with pytest.raises(TimeoutError) as error:
            worker.embed('query')
        assert error.value.reason == reason
    finally:
        worker.close()


def test_failed_pending_request_does_not_fail_different_query(tmp_path):
    worker = IsolatedLocalEmbedder(LocalEmbedder(tmp_path / 'model', EmbeddingIdentity('test')))
    worker.process, worker.connection = Process(), Connection()
    worker.pending = ('old', False)
    worker.pending_started = time.monotonic()
    replies = iter([('error', None), ('ok', [0.6, 0.8])])
    worker.connection.poll = lambda timeout: True
    worker.connection.recv = lambda: next(replies)
    try:
        assert worker.embed('new') == [0.6, 0.8]
        assert worker.connection.requests == [('new', False)]
        assert worker.warm
    finally:
        worker.close()


def test_process_start_consumes_startup_budget(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('session_search.core.local_worker.time.monotonic', lambda: clock[0])
    model = tmp_path / 'model'
    model.touch()
    worker = IsolatedLocalEmbedder(LocalEmbedder(model, EmbeddingIdentity('test')))

    def start():
        worker.process, worker.connection = Process(), Connection()
        clock[0] += 31

    monkeypatch.setattr(worker, '_start', start)
    try:
        with pytest.raises(TimeoutError) as error:
            worker.embed('cold')
        assert error.value.reason == 'local_startup_timeout'
        assert worker.connection.requests == []
    finally:
        worker.close()
