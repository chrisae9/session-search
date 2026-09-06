"""Reviewable manual offload. Application re-verifies exact recovery first."""

import hashlib
import json
import subprocess
import time
from pathlib import Path

from session_search.capture.local import fingerprint
from session_search.core.records import canonical_json, digest
from session_search.storage.backups import ResticRepository
from session_search.storage.catalog import Catalog
from session_search.storage.objects import ObjectStore


def writers_running() -> bool:
    result = subprocess.run(["pgrep", "-if", r"(^|/|\s)codex(\s|$)|Codex.app/Contents/"],
                            capture_output=True, text=True, check=False)
    if result.returncode not in {0, 1}:
        raise RuntimeError("could not verify that Codex writers are stopped")
    return result.returncode == 0


def plan_offload(catalog: Catalog, receipt_file: Path, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    receipt = json.loads(receipt_file.read_text())
    if receipt.get("status") != "restore_verified" or len(receipt.get("receipts", [])) < 2:
        raise ValueError("offload requires verified recovery receipts from both destinations")
    snapshots = {row["snapshot"] for row in receipt["receipts"]}
    if len(snapshots) != 1:
        raise ValueError("backup receipts do not cover the same snapshot")
    candidates, skipped = [], 0
    objects = ObjectStore(catalog.root)
    for row in catalog.db.execute(
        "SELECT DISTINCT s.* FROM raw_sources s JOIN heads h "
        "ON h.session_id=s.session_id AND h.revision=s.revision"
    ):
        path = Path(row["path"])
        try:
            if path.is_symlink() or fingerprint(path) != row["fingerprint"]:
                skipped += 1
                continue
            stat = path.stat()
            if stat.st_mtime > now - 30 * 86400 or stat.st_size != row["size"]:
                skipped += 1
                continue
            if not objects.verify(row["digest"]):
                skipped += 1
                continue
            with path.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != row["digest"]:
                    skipped += 1
                    continue
                source.seek(-1, 2)
                if source.read(1) != b"\n":
                    skipped += 1
                    continue
            candidates.append(dict(row))
        except OSError:
            skipped += 1
    result = {"version": 1, "created_at": now, "retention_days": 30,
              "receipt": receipt, "candidates": candidates, "skipped": skipped}
    result["plan_id"] = digest(canonical_json(result).encode())
    return result


def verify_offload_backups(receipt: dict, candidates: list[dict],
                           repositories: list[ResticRepository]) -> dict:
    """Freshly restore all required backups; never read or remove native files.

    The returned report is verification evidence, not an offline deletion permit.
    Remote callers must bind it to an authenticated request and recheck local files.
    """
    import re
    if (not isinstance(receipt, dict) or receipt.get("version") != 1
            or receipt.get("status") != "restore_verified"):
        raise ValueError("verified version 1 backup receipts are required")
    receipts = receipt.get("receipts", [])
    if not isinstance(receipts, list) or not all(isinstance(row, dict) for row in receipts):
        raise ValueError("backup receipts must be a list of records")
    if not isinstance(candidates, list) or not all(isinstance(row, dict) for row in candidates):
        raise ValueError("raw requirements must be a list of records")
    names = [repository.name for repository in repositories]
    if len(names) < 2 or len(set(names)) != len(names):
        raise ValueError("two distinct configured backup repositories are required")
    if (len(receipts) != len(names)
            or {row.get("repository") for row in receipts} != set(names)):
        raise ValueError("receipts must cover every configured backup repository")
    for row in receipts:
        if row.get("version") != 1 or row.get("status") != "restore_verified":
            raise ValueError("verified version 1 backup receipts are required")
        for field in ("repository_id", "backup_id", "snapshot"):
            if not isinstance(row.get(field), str) or not re.fullmatch("[0-9a-f]{64}", row[field]):
                raise ValueError("invalid backup receipt identity")
        source = Path(row.get("source_path", ""))
        if not source.is_absolute() or ".." in source.parts or source == Path("/"):
            raise ValueError("invalid backup source path")
    if len({row["snapshot"] for row in receipts}) != 1:
        raise ValueError("backup receipts do not cover the same snapshot")
    requirements = []
    for candidate in candidates:
        if not isinstance(candidate.get("session_id"), str) or not candidate["session_id"]:
            raise ValueError("invalid required session identity")
        for field in ("revision", "digest"):
            if not isinstance(candidate.get(field), str) or not re.fullmatch("[0-9a-f]{64}", candidate[field]):
                raise ValueError("invalid required raw identity")
        if type(candidate.get("size")) is not int or candidate["size"] <= 0:
            raise ValueError("invalid required raw size")
        requirements.append({key: candidate[key] for key in ("session_id", "revision", "digest", "size")})
    configured = {repository.name: repository for repository in repositories}
    if len(receipts) < 2 or len({row["repository_id"] for row in receipts}) != len(receipts):
        raise ValueError("two distinct backup repositories are required")
    # Restore each required destination now. Inspect its restored catalog to prove
    # every candidate's exact normalized revision and raw digest are present.
    import tempfile
    for receipt in receipts:
        repository = configured[receipt["repository"]]
        actual = json.loads(repository.run(["cat", "config"]))["id"]
        if actual != receipt["repository_id"]:
            raise ValueError("backup repository identity changed")
        with tempfile.TemporaryDirectory(prefix="session-search-offload-verify-",
                                         dir=repository.restore_directory) as root:
            repository.run(["restore", receipt["backup_id"], "--target", root])
            restored = Path(root) / receipt["source_path"].lstrip("/")
            from session_search.storage.snapshots import verify_snapshot
            verified = verify_snapshot(restored)
            if verified["purpose"] != "recovery" or verified["snapshot"] != receipt["snapshot"]:
                raise ValueError("restored snapshot does not match receipt")
            with Catalog(restored, readonly=True) as recovered:
                for candidate in candidates:
                    row = recovered.db.execute(
                        "SELECT 1 FROM raw_sources WHERE session_id=? AND revision=? AND digest=? AND size=?",
                        (candidate["session_id"], candidate["revision"], candidate["digest"], candidate["size"]),
                    ).fetchone()
                    if not row:
                        raise ValueError("required raw revision is absent from a backup")
    return {"version": 1, "status": "restore_verified", "verified_at": time.time(),
            "requirements_digest": digest(canonical_json(requirements).encode()),
            "snapshot": receipts[0]["snapshot"], "candidates_verified": len(requirements),
            "repositories": [{key: row[key] for key in ("repository", "repository_id", "backup_id")}
                             for row in receipts]}


def apply_offload(plan: dict, repositories: list[ResticRepository], *, catalog: Catalog,
                  expected_plan_id: str) -> dict:
    original = dict(plan)
    plan_id = original.pop("plan_id", None)
    if plan_id != expected_plan_id or digest(canonical_json(original).encode()) != plan_id:
        raise ValueError("offload plan changed after review")
    if writers_running():
        raise RuntimeError("stop Codex writers before applying an offload plan")
    if catalog.readonly:
        raise ValueError("offload requires the primary catalog")
    verify_offload_backups(plan["receipt"], plan["candidates"], repositories)
    removed, skipped = 0, 0
    for candidate in plan["candidates"]:
        if writers_running():
            raise RuntimeError("Codex restarted; remaining offloads stopped")
        path = Path(candidate["path"])
        try:
            # No source path is accepted merely because it appeared in a plan.
            row = catalog.db.execute("SELECT 1 FROM raw_sources s JOIN heads h "
                "ON h.session_id=s.session_id AND h.revision=s.revision "
                "WHERE s.path=? AND s.digest=? AND s.revision=?",
                (str(path), candidate["digest"], candidate["revision"])).fetchone()
            if not row or path.is_symlink() or fingerprint(path) != candidate["fingerprint"]:
                skipped += 1
                continue
            if path.stat().st_mtime > time.time() - 30 * 86400:
                skipped += 1
                continue
            with path.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != candidate["digest"]:
                    skipped += 1
                    continue
            if fingerprint(path) != candidate["fingerprint"]:
                skipped += 1
                continue
            path.unlink()
            removed += 1
        except OSError:
            skipped += 1
    return {"version": 1, "status": "partial" if skipped else "ok",
            "removed": removed, "skipped": skipped}
