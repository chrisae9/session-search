import json
import os
import shutil
import time

import pytest

from session_search.capture.local import capture_file
from session_search.storage import offload
from session_search.storage.backups import ResticRepository, backup_all, restore_backup
from session_search.storage.catalog import Catalog
from session_search.storage.snapshots import create_snapshot
from test_capture import rollout


@pytest.mark.skipif(shutil.which("restic") is None, reason="restic integration dependency missing")
def test_two_real_backups_restore_exact_evidence_before_manual_offload(tmp_path, monkeypatch, capsys):
    password = tmp_path / "password"
    password.write_text("synthetic-test-only-password")
    password.chmod(0o600)
    restore_directory = tmp_path / "restore-work"
    restore_directory.mkdir()
    repositories = [ResticRepository(name, str(tmp_path / name), password, restore_directory)
                    for name in ("one", "two")]
    for repository in repositories:
        repository.initialize()
    source = tmp_path / "example.jsonl"
    source.write_text(rollout("recoverable evidence"))
    old = time.time() - 40 * 86400
    os.utime(source, (old, old))
    with Catalog(tmp_path / "catalog") as catalog:
        capture_file(catalog, source, "test", archive_raw=True)
        create_snapshot(catalog, tmp_path / "snapshot")
        receipt_path = tmp_path / "receipt.json"
        result = backup_all(tmp_path / "snapshot", repositories, receipt_path)
        assert len(result["receipts"]) == 2
        plan = offload.plan_offload(catalog, receipt_path)
        assert len(plan["candidates"]) == 1
        # The server-side primitive verifies exact raw requirements without
        # needing access to a client's native paths or permission to remove them.
        requirements = [{key: row[key] for key in ("session_id", "revision", "digest", "size")}
                        for row in plan["candidates"]]
        proof = offload.verify_offload_backups(result, requirements, repositories)
        assert proof["candidates_verified"] == 1
        assert proof["status"] == "restore_verified"
        assert "source_path" not in json.dumps(proof)
        assert source.exists()
        from session_search.storage.verification_jobs import VerificationJobs
        job_config = tmp_path / 'job-repositories.json'
        job_config.write_text(json.dumps({'repositories': [
            {'name': r.name, 'repository': r.repository, 'password_file': str(r.password_file),
             'restore_directory': str(restore_directory)} for r in repositories]}))
        jobs = VerificationJobs(tmp_path / 'jobs', job_config, receipt_path)
        job = jobs.submit('test', 'a' * 64, requirements)
        assert job['status'] == 'queued'
        assert jobs.submit('test', 'b' * 64, requirements)['status'] == 'busy'
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            polled = jobs.poll('test', job['job_id'])
            if polled['status'] not in {'queued', 'running'}:
                break
            time.sleep(0.05)
        assert polled['status'] == 'restore_verified'
        assert polled['proof']['requirements_digest'] == proof['requirements_digest']
        # A replacement HTTP manager can poll the durable result, but another
        # device receives no indication that this job belongs to somebody else.
        replacement_jobs = VerificationJobs(tmp_path / 'jobs', job_config, receipt_path)
        assert replacement_jobs.poll('test', job['job_id']) == polled
        assert jobs.poll('other', job['job_id'])['status'] == 'unavailable'
        assert source.exists()
        wrong_size = [{**requirements[0], "size": requirements[0]["size"] + 1}]
        with pytest.raises(ValueError, match="absent from a backup"):
            offload.verify_offload_backups(result, wrong_size, repositories)
        assert source.exists()
        monkeypatch.setattr(offload, "writers_running", lambda: True)
        with pytest.raises(RuntimeError, match="writers"):
            offload.apply_offload(plan, repositories, catalog=catalog, expected_plan_id=plan["plan_id"])
        assert source.exists()
        monkeypatch.setattr(offload, "writers_running", lambda: False)
        corrupted = json.loads(json.dumps(plan))
        corrupted["candidates"][0]["path"] = "unexpected"
        with pytest.raises(ValueError, match="plan changed"):
            offload.apply_offload(corrupted, repositories, catalog=catalog, expected_plan_id=plan["plan_id"])
        from dataclasses import replace
        from pathlib import Path
        unavailable_scratch = [replace(r, restore_directory=tmp_path / "missing-scratch")
                               for r in repositories]
        with pytest.raises(FileNotFoundError):
            offload.apply_offload(plan, unavailable_scratch, catalog=catalog,
                                  expected_plan_id=plan["plan_id"])
        assert source.exists()
        restore_targets = []
        run = ResticRepository.run

        def checked_run(repository, arguments, **kwargs):
            if arguments[0] == "restore":
                target = Path(arguments[arguments.index("--target") + 1])
                assert target.parent == restore_directory
                restore_targets.append(repository.name)
            return run(repository, arguments, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(ResticRepository, "run", checked_run)
            applied = offload.apply_offload(plan, repositories, catalog=catalog,
                                            expected_plan_id=plan["plan_id"])
        assert restore_targets == ["one", "two"]
        assert applied["removed"] == 1
        assert not source.exists()
        assert catalog.status()["sessions"] == 1
        assert not list(restore_directory.iterdir())
    from session_search.core.records import SearchQuery
    from session_search.storage.objects import ObjectStore
    configuration = tmp_path / "repositories.json"
    configuration.write_text(json.dumps({"repositories": [
        {"name": r.name, "repository": r.repository, "password_file": str(r.password_file)}
        for r in repositories]}))
    # The native file is now absent. Each independent repository must produce a
    # persistent, usable recovery snapshot with the exact raw bytes and citation.
    for repository, receipt in zip(repositories, result["receipts"]):
        destination = tmp_path / (repository.name + "-recovered")
        if repository.name == "two":
            from session_search.interfaces.cli import main
            assert main(["restore-backup", str(destination), "--repositories", str(configuration),
                         "--repository", repository.name, "--receipt", str(receipt_path)]) == 0
            restored = json.loads(capsys.readouterr().out)
        else:
            restored = restore_backup(repository, receipt, destination)
        assert restored["snapshot"] == receipt["snapshot"]
        assert restored["writable"] is False
        with Catalog(destination, readonly=True) as recovered:
            hit = recovered.search(SearchQuery("recoverable"))["results"][0]
            assert recovered.context([hit["citation"]])["results"]
            key = recovered.db.execute("SELECT digest FROM raw_sources").fetchone()[0]
            assert ObjectStore(destination).path(key).read_text() == rollout("recoverable evidence")
        with pytest.raises(PermissionError, match="read-only"):
            Catalog(destination)
        with pytest.raises(FileExistsError):
            restore_backup(repository, receipt, destination)
        # Both independent Restic restores must support continued writing while
        # preserving the exact citation and raw evidence from recovery.
        from session_search.storage.recovery import prepare_primary
        from session_search.core.records import Event, SessionRevision
        replacement = tmp_path / (repository.name + "-primary")
        prepared = prepare_primary(destination, replacement, expected_snapshot=receipt["snapshot"],
                                   old_primary_isolated=True)
        assert prepared["status"] == "prepared"
        with Catalog(replacement) as primary:
            assert primary.context([hit["citation"]])["results"]
            assert ObjectStore(replacement).path(key).read_text() == rollout("recoverable evidence")
            primary.ingest(SessionRevision("new-session", (Event("new", "user", "continued work"),)),
                           producer="new-device", request_id="new")
            assert primary.status()["sessions"] == 2
        wrong_repository = {**receipt, "repository_id": "0" * 64}
        with pytest.raises(ValueError, match="identity changed"):
            restore_backup(repository, wrong_repository, tmp_path / "wrong-repository")
        wrong_snapshot = {**receipt, "snapshot": "0" * 64}
        with pytest.raises(ValueError, match="differs"):
            restore_backup(repository, wrong_snapshot, tmp_path / "wrong-snapshot")
        assert not (tmp_path / "wrong-snapshot").exists()
        assert not list(tmp_path.glob(".restore-*"))


def test_offload_verification_rejects_incomplete_policy_and_invalid_receipts(tmp_path, monkeypatch):
    repositories = [ResticRepository(name, str(tmp_path / name), tmp_path / 'password')
                    for name in ('one', 'two', 'three')]
    def forbidden(*args, **kwargs):
        raise AssertionError('invalid request reached Restic')
    monkeypatch.setattr(ResticRepository, 'run', forbidden)
    rows = [{'version': 1, 'status': 'restore_verified', 'repository': r.name,
             'repository_id': str(i) * 64, 'backup_id': 'a' * 64,
             'snapshot': 'b' * 64, 'source_path': '/snapshot'}
            for i, r in enumerate(repositories)]
    receipt = {'version': 1, 'status': 'restore_verified', 'receipts': rows}
    with pytest.raises(ValueError, match='every configured'):
        offload.verify_offload_backups({**receipt, 'receipts': rows[:2]}, [], repositories)
    with pytest.raises(ValueError, match='same snapshot'):
        offload.verify_offload_backups({**receipt, 'receipts': [*rows[:2],
            {**rows[2], 'snapshot': 'c' * 64}]}, [], repositories)
    with pytest.raises(ValueError, match='source path'):
        offload.verify_offload_backups({**receipt, 'receipts': [*rows[:2],
            {**rows[2], 'source_path': '/snapshot/../../outside'}]}, [], repositories)
    with pytest.raises(ValueError, match='distinct backup'):
        offload.verify_offload_backups({**receipt, 'receipts': [*rows[:2],
            {**rows[2], 'repository_id': rows[0]['repository_id']}]}, [], repositories)
