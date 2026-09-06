import fcntl
import hashlib
import time

import pytest
from fastapi.testclient import TestClient

from session_search.capture.local import capture_file
from session_search.interfaces.credentials import update_device
from session_search.interfaces.server import create_app
from session_search.storage.catalog import Catalog
from session_search.storage.verification_jobs import VerificationJobs, write_record
from test_capture import rollout


def test_job_ownership_expiry_interruption_and_admission(tmp_path):
    root = tmp_path / 'jobs'
    root.mkdir()
    jobs = VerificationJobs(root, tmp_path / 'repos', tmp_path / 'receipt')
    job_id = 'a' * 64
    record = {'owner': hashlib.sha256(b'one').hexdigest(), 'nonce': 'b' * 64,
              'status': 'restore_verified', 'proof': {'safe': True}, 'expires_at': time.time() + 100}
    write_record(root / (job_id + '.json'), record)
    assert jobs.poll('two', job_id)['status'] == 'unavailable'
    assert jobs.poll('one', job_id)['proof'] == {'safe': True}
    record['expires_at'] = 0
    write_record(root / (job_id + '.json'), record)
    assert jobs.poll('one', job_id)['status'] == 'expired'
    assert 'proof' not in jobs.poll('one', job_id)
    record['status'] = 'running'
    write_record(root / (job_id + '.json'), record)
    assert jobs.poll('one', job_id)['status'] == 'interrupted'
    with (root / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert jobs.poll('one', job_id)['status'] == 'running'
        assert jobs.submit('one', 'c' * 64, [{'session_id': 's', 'revision': 'd' * 64,
                                           'digest': 'e' * 64, 'size': 1}])['status'] == 'busy'
    with pytest.raises(ValueError):
        jobs.submit('one', 'short', [])


def test_http_verification_is_explicit_primary_only_and_device_scoped(tmp_path, monkeypatch):
    credentials = tmp_path / 'credentials.json'
    token = tmp_path / 'token'
    update_device(credentials, 'one', token)
    other = tmp_path / 'other'
    update_device(credentials, 'two', other)
    root = tmp_path / 'catalog'
    source = tmp_path / 'session.jsonl'
    source.write_text(rollout('synthetic archived evidence'))
    with Catalog(root) as catalog:
        capture_file(catalog, source, 'one', archive_raw=True)
        row = dict(catalog.db.execute('SELECT * FROM raw_sources').fetchone())
    requirement = {k: row[k] for k in ('session_id', 'revision', 'digest', 'size')}
    payload = {'nonce': 'a' * 64, 'requirements': [requirement]}
    headers = {'Authorization': 'Bearer ' + token.read_text().strip()}
    other_headers = {'Authorization': 'Bearer ' + other.read_text().strip()}
    assert TestClient(create_app(root, credentials)).post(
        '/v1/offload-verifications', json=payload, headers=headers).status_code == 503
    assert TestClient(create_app(root, credentials, readonly=True)).post(
        '/v1/offload-verifications', json=payload, headers=headers).status_code == 409
    with pytest.raises(ValueError, match='standbys'):
        create_app(root, credentials, readonly=True, offload_repositories=tmp_path / 'repos',
                   offload_receipt=tmp_path / 'receipt')
    calls = []
    def submit(self, producer, nonce, requirements):
        calls.append((producer, nonce, requirements))
        return {'version': 1, 'status': 'queued'}
    monkeypatch.setattr(VerificationJobs, 'submit', submit)
    client = TestClient(create_app(root, credentials, offload_repositories=tmp_path / 'repos',
                                   offload_receipt=tmp_path / 'receipt'))
    assert client.post('/v1/offload-verifications', json=payload).status_code == 401
    assert client.post('/v1/offload-verifications', json=payload, headers=other_headers).status_code == 403
    assert not calls
    assert client.post('/v1/offload-verifications', json=payload, headers=headers).json()['status'] == 'queued'
    assert calls == [('one', payload['nonce'], [requirement])]
    assert source.exists()
    assert not (root / 'offload-verifications').exists()


def test_verification_client_never_fails_over(tmp_path, monkeypatch):
    from session_search.interfaces.client import Client, RemoteError
    client = Client('https://primary.example', tmp_path / 'token', standby='https://standby.example')
    calls = []
    def unavailable(endpoint, route, payload):
        calls.append(endpoint)
        raise RemoteError(503)
    monkeypatch.setattr(client, '_request', unavailable)
    with pytest.raises(RemoteError):
        client.request_offload_verification('a' * 64, [])
    with pytest.raises(RemoteError):
        client.poll_offload_verification('b' * 64)
    assert calls == ['https://primary.example', 'https://primary.example']
