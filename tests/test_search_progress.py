"""Real HTTP transport checks: elapsed time is not a search failure."""

import json
import socket
import threading
import time

import pytest
import uvicorn

from session_search.interfaces.client import Client
from session_search.interfaces.server import create_app
from session_search.storage.catalog import Catalog


@pytest.fixture
def progress_server(tmp_path, monkeypatch):
    import hashlib
    from session_search.storage import semantic
    root = tmp_path / 'data'
    with Catalog(root):
        pass
    token = tmp_path / 'token'
    token.write_text('test-only')
    credentials = tmp_path / 'credentials'
    credentials.write_text(json.dumps({'test': hashlib.sha256(b'test-only').hexdigest()}))
    entered, release, aborted = threading.Event(), threading.Event(), threading.Event()

    def worker(catalog, query, provider):
        entered.set()
        try:
            if query.text == 'progressing':
                for _ in range(24):
                    catalog.search_progress()
                    time.sleep(0.1)
            elif query.text == 'stalled':
                release.wait(5)
                catalog.search_progress()
            elif query.text == 'sql-progress':
                # The SQLite callback also observes cancellation/progress without
                # query contents crossing the transport boundary.
                catalog.db.execute('WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x WHERE n<200000) SELECT sum(n) FROM x').fetchone()
            else:
                raise ValueError('synthetic failure')
            result = catalog.search(query)
            result['mode'] = 'hybrid'
            result['semantic_available'] = True
            return result
        except Exception:
            aborted.set()
            raise

    monkeypatch.setattr(semantic, 'hybrid_search', worker)
    app = create_app(root, credentials, search_workers=1)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    config = uvicorn.Config(app, log_level='error', access_log=False, lifespan='off')
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    client = Client(f'http://127.0.0.1:{listener.getsockname()[1]}', token, timeout=1)
    try:
        yield client, entered, release, aborted
    finally:
        release.set()
        server.should_exit = True
        thread.join(6)
        listener.close()
        assert not thread.is_alive()


def test_progress_keeps_search_alive_beyond_socket_timeout(progress_server):
    client, _, _, _ = progress_server
    start = time.monotonic()
    result = client.read('search', {'text': 'progressing'})
    assert time.monotonic() - start > 2
    assert result['mode'] == 'hybrid' and result['semantic_available']
    assert result['status'] == 'no_matches' and not result.get('degraded')


def test_no_progress_times_out_without_keyword_substitution_and_retains_admission(progress_server):
    client, _, release, aborted = progress_server
    result = client.read('search', {'text': 'stalled'})
    assert result['status'] == 'unavailable'
    assert result['reason'] == 'search_progress_timeout'
    busy = client.read('search', {'text': 'progressing'})
    assert busy['reason'] == 'search_busy'
    release.set()
    assert aborted.wait(3)


def test_streaming_failure_cannot_be_mistaken_for_empty_success(progress_server):
    client, _, _, _ = progress_server
    result = client.read('search', {'text': 'failure'})
    assert result['status'] == 'unavailable' and result['reason'] == 'search_failed'


def test_sql_progress_response(progress_server):
    client, _, _, _ = progress_server
    assert client.read('search', {'text': 'sql-progress'})['semantic_available']


def test_truncated_chunked_stream_is_sanitized_and_can_fail_over(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Truncated(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-ndjson')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            # Announce a large chunk, then drop the connection before completing
            # even the first frame. urllib raises IncompleteRead, not URLError.
            self.wfile.write(b'100\r\n{"version":1,')
            self.wfile.flush()
            self.close_connection = True

    token = tmp_path / 'token'
    token.write_text('test-only')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Truncated)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Client(f'http://127.0.0.1:{server.server_port}', token, timeout=1)
        assert client.read('search', {'text': 'private-query'})['reason'] == 'search_interrupted'
        client.standby = 'http://127.0.0.1:1'
        request = client._request
        def fallback(endpoint, route, payload):
            if endpoint == client.standby:
                return {'version': 1, 'status': 'ok', 'mode': 'hybrid'}
            return request(endpoint, route, payload)
        client._request = fallback
        assert client.read('search', {'text': 'private-query'})['served_by'] == 'standby'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
