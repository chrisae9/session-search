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
    lookup = {'sources': [{k: row[k] for k in ('path', 'fingerprint', 'digest')}]}
    assert client.post('/v1/raw-acknowledgements', json=lookup, headers=other_headers).json()['matches'] == []
    assert client.post('/v1/raw-acknowledgements', json=lookup, headers=headers).json()['matches'] == [
        {'index': 0, **{k: row[k] for k in ('session_id', 'revision', 'size')}}]
    with Catalog(root) as catalog:
        from session_search.core.records import Event, SessionRevision
        catalog.ingest(SessionRevision('another', (Event('e', 'user', 'synthetic'),)),
                       producer='one', request_id='ambiguous',
                       raw={k: row[k] for k in ('path', 'fingerprint', 'digest', 'size')})
    assert client.post('/v1/raw-acknowledgements', json=lookup, headers=headers).json()['matches'] == []
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
    with pytest.raises(RemoteError):
        client.raw_acknowledgements([{'path': 'synthetic', 'fingerprint': 'stable', 'digest': 'c' * 64}])
    assert calls == ['https://primary.example'] * 3


def test_restore_keeps_admission_lock_after_worker_is_killed(tmp_path, monkeypatch):
    import json
    import os
    import signal
    import sys
    binary = tmp_path / 'bin'
    binary.mkdir()
    ready = tmp_path / 'ready.json'
    release = tmp_path / 'release'
    restic = binary / 'restic'
    restic.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys, time
from pathlib import Path
if 'restore' in sys.argv:
    Path(os.environ['TEST_RESTORE_READY']).write_text(json.dumps({'worker': os.getppid(), 'restore': os.getpid()}))
    deadline = time.monotonic() + 20
    while not Path(os.environ['TEST_RESTORE_RELEASE']).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    sys.exit(1)
if 'stats' in sys.argv:
    print(json.dumps({'total_size': 1}))
else:
    print(json.dumps({'id': '0' * 64}))
''')
    restic.chmod(0o700)
    monkeypatch.setenv('PATH', str(binary) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('TEST_RESTORE_READY', str(ready))
    monkeypatch.setenv('TEST_RESTORE_RELEASE', str(release))
    config = tmp_path / 'repositories.json'
    config.write_text(json.dumps({'repositories': [
        {'name': name, 'repository': str(tmp_path / name), 'password_file': str(tmp_path / 'password'),
         'restore_directory': str(tmp_path)} for name in ('one', 'two')]}))
    receipt = tmp_path / 'receipt.json'
    receipt.write_text(json.dumps({'version': 1, 'status': 'restore_verified', 'receipts': [
        {'version': 1, 'status': 'restore_verified', 'repository': name, 'repository_id': str(i) * 64,
         'backup_id': 'a' * 64, 'snapshot': 'b' * 64, 'source_path': '/snapshot'}
        for i, name in enumerate(('one', 'two'))]}))
    root = tmp_path / 'jobs'
    jobs = VerificationJobs(root, config, receipt)
    requirements = [{'session_id': 's', 'revision': 'c' * 64, 'digest': 'd' * 64, 'size': 1}]
    job = jobs.submit('one', 'e' * 64, requirements)
    pids = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), 'synthetic restore did not start'
        pids = json.loads(ready.read_text())
        os.kill(pids['worker'], signal.SIGKILL)
        # The child is confirmed alive after its worker is killed. Its inherited
        # file description must continue excluding a second restoration.
        os.kill(pids['restore'], 0)
        with (root / '.lock').open('a') as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert jobs.submit('one', 'f' * 64, requirements)['status'] == 'busy'
        assert 'proof' not in jobs.poll('one', job['job_id'])
        release.touch()
        deadline = time.monotonic() + 10
        while jobs.poll('one', job['job_id'])['status'] != 'interrupted' and time.monotonic() < deadline:
            time.sleep(0.02)
        assert jobs.poll('one', job['job_id'])['status'] == 'interrupted'
        assert 'proof' not in jobs.poll('one', job['job_id'])
        assert list(tmp_path.glob('.offload-*')), 'crash did not leave scratch to exercise cleanup'
        import subprocess
        def cannot_spawn(*args, **kwargs):
            raise OSError('synthetic spawn failure after cleanup')
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, 'Popen', cannot_spawn)
            with pytest.raises(OSError, match='after cleanup'):
                jobs.submit('one', 'f' * 64, requirements)
        assert not list(tmp_path.glob('.offload-*'))
    finally:
        release.touch()
        if pids:
            for pid in pids.values():
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_job_retention_bound_and_expired_cleanup(tmp_path, monkeypatch):
    import subprocess
    from session_search.storage.verification_jobs import MAX_JOBS
    root = tmp_path / 'jobs'
    root.mkdir()
    jobs = VerificationJobs(root, tmp_path / 'repos', tmp_path / 'receipt')
    for i in range(MAX_JOBS):
        write_record(root / (f'{i:064x}' + '.json'), {
            'owner': 'one', 'nonce': 'a' * 64, 'status': 'failed', 'expires_at': time.time() + 100})
    requirement = [{'session_id': 's', 'revision': 'b' * 64, 'digest': 'c' * 64, 'size': 1}]
    assert jobs.submit('one', 'd' * 64, requirement)['status'] == 'busy'
    for path in root.glob('*.json'):
        write_record(path, {'status': 'failed', 'expires_at': 0})
    staged = root / '.verification-abandoned'
    staged.write_text('interrupted atomic write')
    def cannot_spawn(*args, **kwargs):
        raise OSError('synthetic spawn failure')
    monkeypatch.setattr(subprocess, 'Popen', cannot_spawn)
    with pytest.raises(OSError, match='spawn failure'):
        jobs.submit('one', 'e' * 64, requirement)
    assert not staged.exists()
    paths = list(root.glob('*.json'))
    assert len(paths) == 1
    assert jobs.poll('one', paths[0].stem)['status'] == 'failed'


def test_interrupted_scratch_cleanup_preserves_ownership_until_finished(tmp_path, monkeypatch):
    import shutil
    from session_search.storage.verification_jobs import cleanup_scratch
    job_id = 'a' * 64
    scratch = tmp_path / f'.offload-{job_id}-0'
    scratch.mkdir()
    write_record(scratch / 'owner.json', {'version': 1, 'job_id': job_id})
    restored = scratch / 'restored'
    restored.mkdir()
    (restored / 'one').write_text('synthetic')
    (restored / 'two').write_text('synthetic')
    record = {'job_id': job_id, 'scratch': [str(scratch)]}
    def interrupted(path):
        (path / 'one').unlink()
        raise OSError('interrupted cleanup')
    with monkeypatch.context() as patch:
        patch.setattr(shutil, 'rmtree', interrupted)
        with pytest.raises(OSError):
            cleanup_scratch(record)
    assert (scratch / 'owner.json').exists()
    assert (restored / 'two').exists()
    cleanup_scratch(record)
    assert not scratch.exists()
    assert 'scratch' not in record
    scratch.mkdir()
    write_record(scratch / 'owner.json', {'version': 1, 'job_id': 'b' * 64})
    (scratch / 'preserve').write_text('unowned')
    with pytest.raises(ValueError, match='ownership changed'):
        cleanup_scratch({'job_id': job_id, 'scratch': [str(scratch)]})
    assert (scratch / 'preserve').read_text() == 'unowned'
