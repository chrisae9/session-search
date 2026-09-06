"""Restic snapshots are accepted only after a complete restore and verification."""

import json
import subprocess
import tempfile
import os
import re
import fcntl
from dataclasses import dataclass
from pathlib import Path

from session_search.core.records import canonical_json
from session_search.storage.snapshots import verify_snapshot
from session_search.storage.objects import sync_directory


@dataclass(frozen=True)
class ResticRepository:
    name: str
    repository: str
    password_file: Path
    restore_directory: Path | None = None
    inherited_lock_fd: int | None = None

    def run(self, arguments: list[str], *, timeout: float = 3600) -> str:
        completed = subprocess.run(
            ["restic", "--repo", self.repository, "--password-file", str(self.password_file),
             *arguments], capture_output=True, text=True, timeout=timeout, check=False,
            pass_fds=(() if self.inherited_lock_fd is None else (self.inherited_lock_fd,)),
        )
        if completed.returncode:
            # Restic diagnostics may contain credentials embedded in repository URLs.
            raise RuntimeError(f"backup repository operation failed with exit {completed.returncode}")
        return completed.stdout

    def initialize(self):
        # Explicit administration only; ordinary backup never initializes or prunes.
        self.run(["init"])

    def backup_and_verify(self, snapshot: Path) -> dict:
        snapshot = snapshot.resolve()
        expected = verify_snapshot(snapshot)
        if expected["purpose"] != "recovery":
            raise ValueError("search-only replicas are not recovery backup inputs")
        output = self.run(["backup", str(snapshot), "--json", "--tag", "session-search",
                           "--tag", expected["snapshot"]])
        summaries = [json.loads(line) for line in output.splitlines() if line.strip()]
        backup_id = next((row.get("snapshot_id") for row in reversed(summaries)
                          if row.get("message_type") == "summary"), None)
        if not backup_id:
            raise RuntimeError("backup did not return a completed snapshot")
        self.verify_restore(backup_id, snapshot, expected["snapshot"])
        config = json.loads(self.run(["cat", "config"]))
        return {"version": 1, "repository": self.name, "repository_id": config["id"],
                "backup_id": backup_id, "snapshot": expected["snapshot"],
                "source_path": str(snapshot), "status": "restore_verified"}

    def verify_restore(self, backup_id: str, source_path: Path, expected: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="session-search-restore-", dir=self.restore_directory) as temporary:
            self.run(["restore", backup_id, "--target", temporary])
            restored = Path(temporary) / str(source_path.resolve()).lstrip("/")
            result = verify_snapshot(restored)
            if result["purpose"] != "recovery":
                raise ValueError("restored snapshot is not a recovery backup")
            if result["snapshot"] != expected:
                raise ValueError("restored snapshot differs from the required revision set")
            return result


def load_repositories(path: Path) -> list[ResticRepository]:
    rows = json.loads(path.read_text())["repositories"]
    repositories = [ResticRepository(row["name"], row["repository"],
                                      Path(row["password_file"]).expanduser(),
                                      Path(row["restore_directory"]).expanduser()
                                      if row.get("restore_directory") else None) for row in rows]
    if len({repo.name for repo in repositories}) != len(repositories):
        raise ValueError("backup repository names must be unique")
    return repositories


def restore_backup(repository: ResticRepository, receipt: dict, destination: Path) -> dict:
    """Retain one freshly verified recovery snapshot without enabling a writer."""
    if receipt.get("version") != 1 or receipt.get("status") != "restore_verified":
        raise ValueError("a verified version 1 backup receipt is required")
    if receipt.get("repository") != repository.name:
        raise ValueError("receipt belongs to another repository")
    for field in ("repository_id", "backup_id", "snapshot"):
        if not isinstance(receipt.get(field), str) or not re.fullmatch("[0-9a-f]{64}", receipt[field]):
            raise ValueError("invalid backup receipt identity")
    source = Path(receipt.get("source_path", ""))
    if not source.is_absolute() or ".." in source.parts or source == Path("/"):
        raise ValueError("invalid backup source path")
    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (destination.parent / ".restore.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("restore destination already exists")
        config = json.loads(repository.run(["cat", "config"]))
        if config.get("id") != receipt["repository_id"]:
            raise ValueError("backup repository identity changed")
        # Stage beside the destination so publication is a rename, not another
        # full copy. Failure leaves the destination absent and the backup intact.
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=destination.parent) as temporary:
            repository.run(["restore", receipt["backup_id"], "--target", temporary])
            restored = Path(temporary) / str(source).lstrip("/")
            result = verify_snapshot(restored)
            if result["purpose"] != "recovery" or result["snapshot"] != receipt["snapshot"]:
                raise ValueError("restored snapshot differs from the required recovery evidence")
            for path in restored.rglob("*"):
                if path.is_file():
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
            for path in sorted(restored.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if path.is_dir():
                    sync_directory(path)
            sync_directory(restored)
            if destination.exists() or destination.is_symlink():
                raise FileExistsError("restore destination already exists")
            os.rename(restored, destination)
            sync_directory(destination.parent)
    return {**result, "repository": repository.name, "writable": False}


def backup_all(snapshot: Path, repositories: list[ResticRepository], receipt_path: Path) -> dict:
    if len(repositories) < 2:
        raise ValueError("this durability policy requires two backup repositories")
    receipts = [repository.backup_and_verify(snapshot) for repository in repositories]
    if len({receipt["repository_id"] for receipt in receipts}) != len(receipts):
        raise ValueError("backup destinations must be distinct repositories")
    result = {"version": 1, "status": "restore_verified", "receipts": receipts}
    # Never overwrite an earlier recovery receipt with a partial attempt.
    with receipt_path.open("x") as output:
        output.write(canonical_json(result))
        output.flush()
        import os
        os.fsync(output.fileno())
    return result
