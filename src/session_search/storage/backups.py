"""Restic snapshots are accepted only after a complete restore and verification."""

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from session_search.core.records import canonical_json
from session_search.storage.snapshots import verify_snapshot


@dataclass(frozen=True)
class ResticRepository:
    name: str
    repository: str
    password_file: Path

    def run(self, arguments: list[str], *, timeout: float = 3600) -> str:
        completed = subprocess.run(
            ["restic", "--repo", self.repository, "--password-file", str(self.password_file),
             *arguments], capture_output=True, text=True, timeout=timeout, check=False,
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
        with tempfile.TemporaryDirectory(prefix="session-search-restore-") as temporary:
            self.run(["restore", backup_id, "--target", temporary])
            restored = Path(temporary) / str(source_path.resolve()).lstrip("/")
            result = verify_snapshot(restored)
            if result["snapshot"] != expected:
                raise ValueError("restored snapshot differs from the required revision set")
            return result


def load_repositories(path: Path) -> list[ResticRepository]:
    rows = json.loads(path.read_text())["repositories"]
    repositories = [ResticRepository(row["name"], row["repository"],
                                      Path(row["password_file"]).expanduser()) for row in rows]
    if len({repo.name for repo in repositories}) != len(repositories):
        raise ValueError("backup repository names must be unique")
    return repositories


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
