import json
import os
import shutil
import time

import pytest

from session_search.capture.local import capture_file
from session_search.storage import offload
from session_search.storage.backups import ResticRepository, backup_all
from session_search.storage.catalog import Catalog
from session_search.storage.snapshots import create_snapshot
from test_capture import rollout


@pytest.mark.skipif(shutil.which("restic") is None, reason="restic integration dependency missing")
def test_two_real_backups_restore_exact_evidence_before_manual_offload(tmp_path, monkeypatch):
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
