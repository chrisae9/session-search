import json
import urllib.error

import pytest

from session_search.interfaces.client import Client, RemoteError


@pytest.mark.parametrize('reason,expected', [('read_timeout', 'search_timeout'),
                                            ('search_busy', 'search_busy'),
                                            ('connection_error', 'search_servers_unreachable')])
def test_client_preserves_failure_kind_without_exposing_endpoint(tmp_path, reason, expected):
    token = tmp_path / 'token'
    token.write_text('private-token-value')
    c = Client('https://private.example', token)
    def fail(*args, **kwargs):
        raise RemoteError(503 if reason == 'search_busy' else None, reason=reason)
    c._request = fail
    result = c.read('search', {'text': 'private query'})
    assert result['reason'] == expected
    assert result['failures'][0]['reason'] == reason
    assert all(value not in json.dumps(result) for value in ['private.example', 'private-token-value', 'private query'])


def test_transport_timeout_reason_is_retained(tmp_path):
    token = tmp_path / 'token'
    token.write_text('private-token-value')
    c = Client('https://private.example', token)
    def fail(*args, **kwargs):
        raise urllib.error.URLError(TimeoutError())
    c.opener.open = fail
    assert c.read('search', {'text': 'query'})['reason'] == 'search_timeout'


def test_search_logs_contain_timings_but_no_query_evidence_or_credentials(tmp_path, caplog):
    from test_raw_reconciliation import setup
    server, client, _, _ = setup(tmp_path)
    caplog.set_level('INFO', logger='uvicorn.error')
    result = client.read('search', {'text': 'sensitive-query-text', 'literal': True})
    assert result['status'] == 'no_matches'
    record = next(json.loads(r.message) for r in caplog.records if 'request_id' in r.message)
    assert record['operation'] == 'search' and record['elapsed_ms'] >= 0
    assert 'sensitive-query-text' not in caplog.text
    assert 'Bearer' not in caplog.text


@pytest.mark.parametrize('failure', ['incomplete', 'tls'])
def test_incomplete_http_response_does_not_escape_client(tmp_path, failure):
    import http.client
    token = tmp_path / 'token'
    token.write_text('test-only')
    client = Client('http://127.0.0.1:1', token)
    class Response:
        headers = {'Content-Type': 'application/x-ndjson'}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def readline(self, *args):
            if failure == 'tls':
                import ssl
                raise ssl.SSLEOFError('synthetic TLS disconnect')
            raise http.client.IncompleteRead(b'partial')
    client.opener.open = lambda *a, **k: Response()
    result = client.read('search', {'text': 'query'})
    assert result['reason'] == 'search_interrupted'
