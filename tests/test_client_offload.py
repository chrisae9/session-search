import json
import os
import shutil
import time

import pytest

from session_search.capture import offload
from session_search.capture.local import capture_file
from session_search.capture.queue import UploadQueue
from session_search.core.records import digest, canonical_json
from test_capture import rollout


class ProofClient:
    primary = 'https://primary.example'
    def request_offload_verification(self, nonce, requirements):
        self.nonce, self.requirements = nonce, requirements
        return {'version': 1, 'status': 'queued', 'job_id': 'a' * 64, 'nonce': nonce}
    def poll_offload_verification(self, job_id):
        return {'version': 1, 'status': 'restore_verified', 'job_id': job_id, 'nonce': self.nonce,
                'expires_at': time.time() + 899,
                'proof': {'version': 1, 'status': 'restore_verified', 'verified_at': time.time(),
                          'requirements_digest': digest(canonical_json(self.requirements).encode()),
                          'candidates_verified': len(self.requirements), 'snapshot': 'b' * 64,
                          'repositories': [{'repository': str(i), 'repository_id': str(i) * 64,
                                            'backup_id': 'c' * 64} for i in range(2)]}}


def prepared(tmp_path):
    home = tmp_path / 'codex'
    home.mkdir()
    source = home / 'example.jsonl'
    source.write_text(rollout('synthetic recoverable evidence'))
    old = time.time() - 40 * 86400
    os.utime(source, (old, old))
    root = tmp_path / 'client'
    with UploadQueue(root) as queue:
        capture_file(queue, source, 'device', archive_raw=True)
        pending = queue.db.execute('SELECT revision,payload FROM pending').fetchone()
        raw = json.loads(pending['payload'])['raw']
        class Ack:
            def upload_raw(self, *args):
                pass
            def upload(self, payload):
                return {'status': 'durable', 'revision': pending['revision']}
        queue.flush(Ack())
    return home, source, root, raw


@pytest.mark.parametrize('fault', ['nonce', 'digest', 'expired', 'repositories', 'changed', 'writers'])
def test_invalid_fresh_proof_or_changed_source_never_removes_file(tmp_path, monkeypatch, fault):
    home, source, root, raw = prepared(tmp_path)
    client = ProofClient()
    monkeypatch.setattr(offload, 'writers_running', lambda: False)
    with UploadQueue(root) as queue:
        plan = offload.plan_client_offload(queue, client, home)
        assert len(plan['candidates']) == 1
        original_poll = client.poll_offload_verification
        def bad_poll(job_id):
            result = original_poll(job_id)
            if fault == 'nonce':
                result['nonce'] = 'f' * 64
            elif fault == 'digest':
                result['proof']['requirements_digest'] = 'f' * 64
            elif fault == 'expired':
                result['expires_at'] = 0
            elif fault == 'repositories':
                result['proof']['repositories'][1] = result['proof']['repositories'][0]
            elif fault == 'changed':
                source.write_text(rollout('changed while backup was restoring'))
            elif fault == 'writers':
                monkeypatch.setattr(offload, 'writers_running', lambda: True)
            return result
        monkeypatch.setattr(client, 'poll_offload_verification', bad_poll)
        if fault == 'changed':
            result = offload.apply_client_offload(queue, client, plan, expected_plan_id=plan['plan_id'])
            assert result['removed'] == 0 and result['skipped'] == 1
        else:
            with pytest.raises((ValueError, RuntimeError)):
                offload.apply_client_offload(queue, client, plan, expected_plan_id=plan['plan_id'])
        assert source.exists()


def test_plan_retention_acknowledgement_locks_and_review_binding(tmp_path, monkeypatch):
    home, source, root, raw = prepared(tmp_path)
    client = ProofClient()
    monkeypatch.setattr(offload, 'writers_running', lambda: False)
    with UploadQueue(root) as queue:
        plan = offload.plan_client_offload(queue, client, home)
        assert len(plan['candidates']) == 1
        with queue.capture_guard(), pytest.raises(BlockingIOError):
            offload.plan_client_offload(queue, client, home)
        with pytest.raises(ValueError, match='changed after review'):
            offload.apply_client_offload(queue, client, {**plan, 'retention_days': 0},
                                        expected_plan_id=plan['plan_id'])
        os.utime(source, None)
        assert not offload.plan_client_offload(queue, client, home)['candidates']
        queue.db.execute('DELETE FROM raw_acknowledgements')
        queue.db.commit()
        assert offload.plan_client_offload(queue, client, home)['missing_acknowledgements'] == 1
        assert source.exists()


@pytest.mark.skipif(shutil.which('restic') is None, reason='restic integration dependency missing')
def test_http_client_offload_requires_two_fresh_real_restores(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from session_search.interfaces.client import Client, RemoteError
    from session_search.interfaces.credentials import update_device
    from session_search.interfaces.server import create_app
    from session_search.storage.backups import ResticRepository, backup_all
    from session_search.storage.catalog import Catalog
    from session_search.storage.snapshots import create_snapshot
    home = tmp_path / 'codex'
    home.mkdir()
    source = home / 'example.jsonl'
    source.write_text(rollout('recoverable client evidence'))
    original = source.read_bytes()
    old = time.time() - 40 * 86400
    os.utime(source, (old, old))
    password = tmp_path / 'password'
    password.write_text('synthetic-only')
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    repositories = [ResticRepository(name, str(tmp_path / name), password, scratch) for name in ('one', 'two')]
    for repository in repositories:
        repository.initialize()
    config = tmp_path / 'repositories.json'
    config.write_text(json.dumps({'repositories': [
        {'name': r.name, 'repository': r.repository, 'password_file': str(password),
         'restore_directory': str(scratch)} for r in repositories]}))
    receipt = tmp_path / 'receipt.json'
    credentials = tmp_path / 'credentials.json'
    token = tmp_path / 'token'
    update_device(credentials, 'device', token)
    server_root = tmp_path / 'server'
    with Catalog(server_root):
        pass
    http = TestClient(create_app(server_root, credentials, offload_repositories=config, offload_receipt=receipt))
    class HTTPClient(Client):
        def _request(self, endpoint, route, payload, *, method=None):
            assert endpoint == self.primary
            headers = {'Authorization': 'Bearer ' + token.read_text().strip()}
            kwargs = {'content': payload} if isinstance(payload, bytes) else {'json': payload} if payload is not None else {}
            response = http.request(method or ('POST' if payload is not None else 'GET'), route, headers=headers, **kwargs)
            if response.status_code >= 400:
                raise RemoteError(response.status_code)
            return response.json()
    client = HTTPClient('https://primary.example', token, standby='https://standby.example')
    monkeypatch.setattr(offload, 'writers_running', lambda: False)
    with UploadQueue(tmp_path / 'client') as queue:
        capture_file(queue, source, 'device', archive_raw=True)
        assert queue.flush(client)['sent'] == 1
        with Catalog(server_root) as catalog:
            from session_search.core.records import SearchQuery
            assert catalog.search(SearchQuery('recoverable'))['results']
            create_snapshot(catalog, tmp_path / 'snapshot')
        backup_all(tmp_path / 'snapshot', repositories, receipt)
        plan = offload.plan_client_offload(queue, client, home)
        assert len(plan['candidates']) == 1
        result = offload.apply_client_offload(queue, client, plan, expected_plan_id=plan['plan_id'], timeout=60)
        assert result['removed'] == 1
        assert not source.exists()
        assert queue.db.execute('SELECT COUNT(*) FROM raw_acknowledgements').fetchone()[0] == 1
        from session_search.core.records import SearchQuery
        from session_search.storage.objects import ObjectStore
        with Catalog(server_root, readonly=True) as catalog:
            hit = catalog.search(SearchQuery('recoverable'))['results'][0]
            assert catalog.context([hit['citation']])['status'] == 'ok'
            assert b''.join(ObjectStore(server_root).iter_bytes(plan['candidates'][0]['digest'])) == original
    assert not list(scratch.iterdir())


def test_cli_writes_private_plan_without_overwriting_reviewed_file(tmp_path, capsys):
    from session_search.interfaces.cli import main
    home, source, root, raw = prepared(tmp_path)
    token = tmp_path / 'token'
    token.write_text('synthetic-only')
    output = tmp_path / 'plan.json'
    args = ['--data-dir', str(root), '--primary', 'https://primary.example', '--token-file', str(token),
            'plan-client-offload', '--codex-home', str(home), '--output', str(output)]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'planned' and report['candidates'] == 1
    assert output.stat().st_mode & 0o777 == 0o600
    original = output.read_bytes()
    assert main(args) == 2
    capsys.readouterr()
    assert output.read_bytes() == original
    assert source.exists()
