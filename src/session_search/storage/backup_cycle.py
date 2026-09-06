"""Retry one recovery snapshot until every configured destination verifies it."""

import fcntl
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from session_search.core.records import canonical_json, digest
from session_search.storage.catalog import Catalog
from session_search.storage.objects import sync_directory
from session_search.storage.replication import capacity, save_receipt
from session_search.storage.snapshots import create_snapshot, verify_snapshot


def backup_cycle(source: Path, outbox: Path, repositories, *, reserve_bytes=2 * 1024 ** 3):
    if len(repositories) < 2 or len({r.name for r in repositories}) != len(repositories):
        raise ValueError("two distinct named backup destinations are required")
    if type(reserve_bytes) is not int or reserve_bytes < 0:
        raise ValueError("backup reserve must be nonnegative")
    source = source.resolve()
    if (source / "CURRENT").exists() or (source / "manifest.json").exists():
        raise ValueError("backup cycles require the authoritative catalog, not a snapshot or replica")
    if outbox.is_symlink():
        raise ValueError("backup outbox must not be a symlink")
    outbox = outbox.resolve()
    if source == outbox or outbox in source.parents:
        raise ValueError("backup outbox must be separate from source")
    outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    binding = digest(canonical_json({
        "source": str(source),
        "repositories": [(r.name, r.repository, str(r.password_file), str(r.restore_directory))
                         for r in repositories],
    }).encode())
    with (outbox / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"version": 1, "status": "coalesced"}
        state_path = outbox / "state.json"
        if state_path.is_symlink():
            raise ValueError("invalid backup state")
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state.get("version") != 1 or state.get("binding") != binding:
                raise ValueError("backup outbox belongs to another configuration")
        else:
            if any(p.name != ".lock" for p in outbox.iterdir()):
                raise ValueError("new backup outbox must be empty")
            state = {"version": 1, "binding": binding, "receipts": {}}
            save_receipt(state_path, state)
        def record(result, publication=None):
            completed_at = datetime.now(timezone.utc).isoformat()
            summary = {"version": 1, "status": result["status"], "completed_at": completed_at,
                       "required_destinations": len(repositories),
                       "verified_destinations": len(state.get("receipts", {}))}
            if publication:
                summary["publication"] = publication
            if result.get("snapshot"):
                summary["snapshot"] = result["snapshot"]
            elif result.get("receipts"):
                summary["snapshot"] = result["receipts"][0]["snapshot"]
            save_receipt(outbox / "status.json", {**result, "completed_at": completed_at})
            save_receipt(source / "backup-cycle-status.json", summary)
            return result

        pending = outbox / "pending"
        if pending.is_symlink():
            raise ValueError("invalid pending snapshot")
        # A killed snapshot builder may leave unpublished staging. Do not create
        # another generation until that interrupted work has been inspected.
        if any(outbox.glob(".snapshot-*")):
            return record({"version": 1, "status": "deferred", "reason": "interrupted_snapshot_staging"})
        if not pending.exists():
            with Catalog(source, readonly=True) as catalog:
                raw_bytes = catalog.db.execute(
                    "SELECT COALESCE(SUM(size),0) FROM (SELECT DISTINCT digest,size FROM raw_sources)"
                ).fetchone()[0]
                catalog_bytes = sum(p.stat().st_size for p in source.glob("catalog.sqlite3*")
                                    if p.is_file())
                check = capacity(outbox, 2 * (raw_bytes + catalog_bytes), reserve_bytes)
                if check["status"] != "ready":
                    return record({**check, "stage": "recovery_snapshot"})
                create_snapshot(catalog, pending)
            state["receipts"] = {}
        verified = verify_snapshot(pending)
        if verified["purpose"] != "recovery":
            raise ValueError("backup cycle requires a recovery snapshot")
        if state.get("snapshot") != verified["snapshot"]:
            state.update(snapshot=verified["snapshot"], receipts={})
        save_receipt(state_path, state)
        failures = []
        known_ids = set()
        # Restore materializes the full logical archive even when staging uses
        # shared chunks or hardlinks. Check each configured scratch filesystem.
        manifest = json.loads((pending / "manifest.json").read_text())
        restore_bytes = (pending / "catalog.sqlite3").stat().st_size + sum(
            item["size"] for item in manifest["raw_objects"])
        for repository in repositories:
            try:
                identity = json.loads(repository.run(["cat", "config"]))["id"]
                if identity in known_ids:
                    raise ValueError("backup repositories are not independent")
                known_ids.add(identity)
                receipt = state["receipts"].get(repository.name)
                if receipt and receipt["repository_id"] != identity:
                    raise ValueError("backup repository identity changed")
                if receipt and (receipt.get("snapshot") != verified["snapshot"]
                                or receipt.get("status") != "restore_verified"
                                or receipt.get("repository") != repository.name
                                or receipt.get("source_path") != str(pending)):
                    raise ValueError("cached backup receipt does not match pending work")
                if receipt:
                    continue  # Exact immutable snapshot already restore-verified.
                check = capacity(repository.restore_directory or Path(tempfile.gettempdir()),
                                 restore_bytes, reserve_bytes)
                if check["status"] != "ready":
                    failures.append({"repository": repository.name, "reason": "restore_capacity"})
                    continue
                receipt = repository.backup_and_verify(pending)
                if (receipt.get("snapshot") != verified["snapshot"]
                        or receipt.get("repository_id") != identity
                        or receipt.get("status") != "restore_verified"):
                    raise ValueError("backup acknowledgement changed identity")
                state["receipts"][repository.name] = receipt
                save_receipt(state_path, state)
            except (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
                # Never expose Restic exception text or repository URLs.
                failures.append({"repository": repository.name, "reason": type(exc).__name__})
        if failures:
            result = {"version": 1, "status": "partial", "snapshot": verified["snapshot"],
                      "verified_destinations": sorted(state["receipts"]), "failures": failures}
        else:
            receipts = [state["receipts"][r.name] for r in repositories]
            result = {"version": 1, "status": "restore_verified", "receipts": receipts}
            directory = outbox / "receipts"
            directory.mkdir(mode=0o700, exist_ok=True)
            receipt_path = directory / (verified["snapshot"] + ".json")
            if receipt_path.exists():
                if json.loads(receipt_path.read_text()) != result:
                    raise ValueError("completed backup receipt differs")
            else:
                save_receipt(receipt_path, result)
            # Publish the complete receipt before reclaiming the one staging
            # generation. A crash before cleanup safely retries the same receipt.
            save_receipt(outbox / "latest.json", result)
            shutil.rmtree(pending)
            sync_directory(outbox)
            result = {**result, "receipt": str(receipt_path)}
        return record(result, verified["publication"])


def cycle_status(root: Path, publication: str | None):
    path = root / "backup-cycle-status.json"
    if not path.exists():
        return "not_configured"
    try:
        if path.is_symlink():
            raise ValueError("invalid status")
        with path.open() as stream:
            text = stream.read(8193)
        if len(text) > 8192:
            raise ValueError("oversized status")
        value = json.loads(text)
        if value["version"] != 1 or value["status"] not in {"partial", "deferred", "restore_verified"}:
            raise ValueError("invalid status")
        timestamp = datetime.fromisoformat(value["completed_at"])
        if timestamp.tzinfo is None:
            raise ValueError("timestamp must include timezone")
        result = {"status": value["status"], "completed_at": value["completed_at"]}
        for name in ("required_destinations", "verified_destinations"):
            if type(value[name]) is not int or value[name] < 0:
                raise ValueError("invalid destination count")
            result[name] = value[name]
        result["matches_current"] = bool(publication and value.get("publication") == publication)
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "unavailable"}
