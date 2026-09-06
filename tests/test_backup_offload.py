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
        monkeypatch.setattr(offload, "writers_running", lambda: True)
        with pytest.raises(RuntimeError, match="writers"):
            offload.apply_offload(plan, repositories, catalog=catalog, expected_plan_id=plan["plan_id"])
        assert source.exists()
        monkeypatch.setattr(offload, "writers_running", lambda: False)
        corrupted = json.loads(json.dumps(plan))
        corrupted["candidates"][0]["path"] = "unexpected"
        with pytest.raises(ValueError, match="plan changed"):
            offload.apply_offload(corrupted, repositories, catalog=catalog, expected_plan_id=plan["plan_id"])
        applied = offload.apply_offload(plan, repositories, catalog=catalog, expected_plan_id=plan["plan_id"])
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
