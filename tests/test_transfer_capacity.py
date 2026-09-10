import hashlib
import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from session_search.interfaces.server import create_app
from session_search.storage.catalog import Catalog
from session_search.storage.transfer_capacity import staging_admission
from session_search.storage.transfers import RawTransfers


def test_namespaces_share_budget_and_completed_upload_releases_it(tmp_path):
    raw = RawTransfers(tmp_path, max_staging_bytes=8)
    normalized = RawTransfers(tmp_path, namespace='revision-upload', max_staging_bytes=8)
    key = hashlib.sha256(b'abcdefgh').hexdigest()
    raw.append('device', key, 0, 8, b'abcd')
    normalized.append('device', key, 0, 8, b'abcd')
    with pytest.raises(OSError, match='staging byte limit'):
        raw.append('device', key, 4, 8, b'efgh')
    assert raw.status('device', key)['offset'] == 4
    # A full staging budget needs expiry or a larger reviewed budget; no pending
    # bytes are discarded to force progress. Raising the limit permits completion.
    raw.max_staging_bytes = 12
    assert raw.append('device', key, 4, 8, b'efgh')['status'] == 'complete'
    assert normalized.append('device', key, 4, 8, b'efgh')['status'] == 'complete'


def test_complete_partial_can_finalize_above_new_limit(tmp_path):
    transfer = RawTransfers(tmp_path, max_staging_bytes=1)
    data = b'complete synthetic data'
    key = hashlib.sha256(data).hexdigest()
    partial, _ = transfer.paths('d', key)
    partial.write_bytes(data)
    assert transfer.append('d', key, len(data), len(data), b'')['status'] == 'complete'


def test_scan_limits_and_special_files_fail_without_append(tmp_path):
    transfer = RawTransfers(tmp_path, max_staging_bytes=8, staging_scan_limit=1)
    key = hashlib.sha256(b'abcdef').hexdigest()
    partial, _ = transfer.paths('d', key)
    (partial.parent / 'other.lock').touch()
    with pytest.raises(OSError, match='scan limit'):
        transfer.append('d', key, 0, 6, b'abc')
    assert not partial.exists()
    transfer.staging_scan_limit = 100
    target = tmp_path / 'untouched'
    target.write_bytes(b'keep')
    (partial.parent / 'other.part').symlink_to(target)
    with pytest.raises(OSError, match='partial type'):
        transfer.append('d', key, 0, 6, b'abc')
    assert not partial.exists() and target.read_bytes() == b'keep'


def test_process_lock_defers_other_writer_and_releases_on_crash(tmp_path):
    # Child dies while owning the shared admission lock, after a durable append.
    script = '''
import os, sys
from pathlib import Path
from session_search.storage.transfer_capacity import staging_admission
root = Path(sys.argv[1])
with staging_admission(root, 6, 8):
    p = root / 'transfers' / 'synthetic.part'
    with p.open('wb') as f:
        f.write(b'123456'); f.flush(); os.fsync(f.fileno())
    print('locked', flush=True)
    sys.stdin.readline()
    os._exit(73)
'''
    (tmp_path / 'transfers').mkdir()
    child = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        with pytest.raises(BlockingIOError):
            with staging_admission(tmp_path, 1, 8):
                pytest.fail('second writer admitted')
        child.communicate('\n', timeout=5)
        assert child.returncode == 73
        with pytest.raises(OSError, match='staging byte limit'):
            with staging_admission(tmp_path, 3, 8):
                pytest.fail('durable bytes omitted after crash')
        with staging_admission(tmp_path, 2, 8):
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_http_capacity_failure_is_retryable_in_both_namespaces(tmp_path):
    root = tmp_path / 'server'
    with Catalog(root):
        pass
    credentials = tmp_path / 'credentials.json'
    credentials.write_text(json.dumps({'d': hashlib.sha256(b'synthetic-token').hexdigest()}))
    with TestClient(create_app(root, credentials, max_staging_bytes=5)) as client:
        headers = {'Authorization': 'Bearer synthetic-token'}
        key = hashlib.sha256(b'123456').hexdigest()
        for route in ('objects', 'revision-objects'):
            url = f'/v1/{route}/{key}?offset=0&total=6'
            response = client.put(url, headers=headers, content=b'123456')
            assert response.status_code == 503
            assert client.get(url.split('?')[0], headers=headers).json()['offset'] == 0


@pytest.mark.parametrize('value', [0, -1, True, 1.5])
def test_invalid_budget(value, tmp_path):
    with pytest.raises(ValueError):
        RawTransfers(tmp_path, max_staging_bytes=value)


def test_unconfigured_upload_does_not_scan_or_create_capacity_lock(tmp_path):
    transfer = RawTransfers(tmp_path)
    data = b'abc'
    key = hashlib.sha256(data).hexdigest()
    transfer.append('d', key, 0, 3, data)
    assert not (tmp_path / '.transfer-capacity.lock').exists()


def test_concurrent_processes_cannot_overcommit_shared_namespaces(tmp_path):
    with Catalog(tmp_path):
        pass
    script = '''
import hashlib, sys
from pathlib import Path
from session_search.storage.transfers import RawTransfers
transfer = RawTransfers(Path(sys.argv[1]), namespace=sys.argv[2], max_staging_bytes=8)
print('ready', flush=True)
sys.stdin.readline()
try:
    transfer.append('d', hashlib.sha256(b'123456789').hexdigest(), 0, 9, b'123456')
    print('admitted')
except OSError:
    print('deferred')
'''
    children = [subprocess.Popen([sys.executable, '-c', script, str(tmp_path), namespace],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                for namespace in ('raw', 'revision-upload')]
    try:
        for child in children:
            assert child.stdout.readline().strip() == 'ready'
        for child in children:
            child.stdin.write('\n')
            child.stdin.flush()
        outcomes = [child.communicate(timeout=5)[0].strip() for child in children]
        assert sorted(outcomes) == ['admitted', 'deferred']
        assert sum(p.stat().st_size for p in tmp_path.glob('*transfers/*.part')) == 6
        assert all(child.returncode == 0 for child in children)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


@pytest.mark.parametrize('value', [0, -1, True, 1000001])
def test_invalid_scan_limit(value, tmp_path):
    with pytest.raises(ValueError):
        RawTransfers(tmp_path, staging_scan_limit=value)


def test_capacity_recovery_preserves_client_queue_and_exact_raw(tmp_path):
    from session_search.capture.local import capture_file
    from session_search.capture.queue import UploadQueue
    from session_search.core.records import SearchQuery
    from session_search.interfaces.client import Client, RemoteError
    from session_search.storage.objects import ObjectStore
    from test_capture import rollout

    root = tmp_path / 'server'
    with Catalog(root):
        pass
    credentials = tmp_path / 'credentials.json'
    credentials.write_text(json.dumps({'device': hashlib.sha256(b'synthetic-token').hexdigest()}))
    source = tmp_path / 'synthetic.jsonl'
    source.write_text(rollout('capacity retry preserves history', sid='synthetic'))
    original = source.read_bytes()
    key = hashlib.sha256(original).hexdigest()
    client = Client('http://127.0.0.1:1234', tmp_path / 'unused-token')

    def request(endpoint, route, payload, *, method=None):
        kwargs = {'headers': {'Authorization': 'Bearer synthetic-token'}}
        if isinstance(payload, bytes):
            kwargs['content'] = payload
        elif payload is not None:
            kwargs['json'] = payload
        response = http.request(method or ('POST' if payload is not None else 'GET'), route, **kwargs)
        if response.status_code != 200:
            raise RemoteError(response.status_code)
        return response.json()

    client._request = request
    with UploadQueue(tmp_path / 'client') as queue:
        capture_file(queue, source, 'device', archive_raw=True)
        with TestClient(create_app(root, credentials, max_staging_bytes=1)) as http:
            assert queue.flush(client, now=0)['failed'] == 1
        assert queue.db.execute('SELECT state FROM pending').fetchone()[0] == 'pending'
        assert queue.db.execute('SELECT COUNT(*) FROM raw_acknowledgements').fetchone()[0] == 0
        assert b''.join(ObjectStore(queue.root).iter_bytes(key)) == original
        with TestClient(create_app(root, credentials, max_staging_bytes=len(original))) as http:
            assert queue.flush(client, now=10)['sent'] == 1
        assert queue.status()['pending'] == 0
        assert queue.db.execute('SELECT COUNT(*) FROM raw_acknowledgements').fetchone()[0] == 1
    assert source.read_bytes() == original
    assert b''.join(ObjectStore(root).iter_bytes(key)) == original
    with Catalog(root, readonly=True) as catalog:
        result = catalog.search(SearchQuery('capacity retry preserves history', literal=True))
        assert result['results']
        assert catalog.context([result['results'][0]['citation']])['status'] == 'ok'


@pytest.mark.parametrize('namespace', ['raw', 'revision-upload'])
def test_absent_status_and_completed_churn_do_not_exhaust_admission(tmp_path, namespace):
    transfer = RawTransfers(tmp_path, namespace=namespace, max_staging_bytes=32, staging_scan_limit=2)
    for i in range(12):
        data = f'synthetic-{i}'.encode()
        key = hashlib.sha256(data).hexdigest()
        assert transfer.status('device', key)['offset'] == 0
    assert not transfer.staging.exists()
    for i in range(12):
        data = f'synthetic-{i}'.encode()
        key = hashlib.sha256(data).hexdigest()
        assert transfer.append('device', key, 0, len(data), data)['status'] == 'complete'
    assert list(transfer.staging.iterdir()) == []


def test_waiter_retries_retired_lock_inode(tmp_path):
    import select
    from session_search.storage.transfers import transfer_lock

    transfer = RawTransfers(tmp_path)
    key = hashlib.sha256(b'x').hexdigest()
    partial, lock = transfer.paths('device', key)
    script = '''
import fcntl, sys
from pathlib import Path
from session_search.storage.transfers import RawTransfers
original = fcntl.flock
first = True
def controlled(fd, operation):
    global first
    if operation == fcntl.LOCK_EX and first:
        first = False
        print('opened', flush=True)
        original(fd, operation)
        print('retired-acquired', flush=True)
        sys.stdin.readline()
    else:
        original(fd, operation)
fcntl.flock = controlled
result = RawTransfers(Path(sys.argv[1])).append('device', sys.argv[2], 0, 1, b'x')
print(result['status'], flush=True)
'''
    child = None
    try:
        with transfer_lock(partial, lock):
            child = subprocess.Popen([sys.executable, '-c', script, str(tmp_path), key],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            assert child.stdout.readline().strip() == 'opened'
        assert child.stdout.readline().strip() == 'retired-acquired'
        with transfer_lock(partial, lock):
            child.stdin.write('\n')
            child.stdin.flush()
            assert not select.select([child.stdout], [], [], 0.2)[0]
            assert not partial.exists()
        stdout, _ = child.communicate(timeout=5)
        assert child.returncode == 0 and stdout.strip() == 'complete'
        assert transfer.objects.verify(key) and not lock.exists()
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()
