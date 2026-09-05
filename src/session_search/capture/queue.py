"""Durable outbound work, separate from the full search catalog."""

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from session_search.core.records import SessionRevision, canonical_json
from session_search.interfaces.client import Client, RemoteError


class UploadQueue:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "upload-queue.sqlite3"
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS pending (
              seq INTEGER PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
              session_id TEXT NOT NULL, revision TEXT NOT NULL, payload TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
              state TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS captured (
              path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS acknowledged (
              session_id TEXT PRIMARY KEY, revision TEXT NOT NULL
            );
        """)
        path.chmod(0o600)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def fingerprint(self, path):
        row = self.db.execute("SELECT fingerprint FROM captured WHERE path=?", (path,)).fetchone()
        return row[0] if row else None

    def ingest(self, session: SessionRevision, *, producer: str, request_id: str,
               checkpoint: tuple[str, str] | None = None) -> dict:
        with self.db:
            previous = self.db.execute(
                "SELECT revision FROM pending WHERE session_id=? ORDER BY seq DESC LIMIT 1",
                (session.session_id,),
            ).fetchone()
            if previous is None:
                previous = self.db.execute("SELECT revision FROM acknowledged WHERE session_id=?",
                                           (session.session_id,)).fetchone()
            payload = canonical_json({"request_id": request_id,
                                      "expected_revision": previous[0] if previous else None,
                                      "session": asdict(session)})
            existing = self.db.execute("SELECT revision FROM pending WHERE request_id=?",
                                       (request_id,)).fetchone()
            if existing and existing[0] != session.revision:
                raise ValueError("idempotency key reused for different content")
            self.db.execute(
                "INSERT OR IGNORE INTO pending(request_id,session_id,revision,payload) VALUES (?,?,?,?)",
                (request_id, session.session_id, session.revision, payload),
            )
            if checkpoint:
                self.db.execute("INSERT INTO captured VALUES (?,?) ON CONFLICT(path) "
                                "DO UPDATE SET fingerprint=excluded.fingerprint", checkpoint)
        return {"session_id": session.session_id, "revision": session.revision,
                "duplicate": bool(existing), "upload": "queued"}

    def status(self) -> dict:
        rows = self.db.execute("SELECT state,COUNT(*) total FROM pending GROUP BY state").fetchall()
        return {"pending": sum(row["total"] for row in rows),
                "states": {row["state"]: row["total"] for row in rows}}

    def flush(self, client: Client, *, limit: int = 100, now: float | None = None) -> dict:
        if not 1 <= limit <= 1000:
            raise ValueError("flush limit must be between 1 and 1000")
        now = time.time() if now is None else now
        sent = 0
        failed = 0
        # Only the oldest outstanding revision per session can be sent. A conflict
        # blocks that session; unrelated histories can continue to progress.
        rows = self.db.execute(
            "SELECT p.* FROM pending p WHERE p.state='pending' AND p.next_attempt<=? AND "
            "NOT EXISTS(SELECT 1 FROM pending older WHERE older.session_id=p.session_id "
            "AND older.seq<p.seq) ORDER BY p.seq LIMIT ?", (now, limit),
        ).fetchall()
        for row in rows:
            try:
                receipt = client.upload(json.loads(row["payload"]))
                if receipt.get("status") != "durable" or receipt.get("revision") != row["revision"]:
                    raise RemoteError(502)
                with self.db:
                    self.db.execute("INSERT INTO acknowledged VALUES (?,?) ON CONFLICT(session_id) "
                                    "DO UPDATE SET revision=excluded.revision",
                                    (row["session_id"], row["revision"]))
                    self.db.execute("DELETE FROM pending WHERE seq=?", (row["seq"],))
                sent += 1
            except RemoteError as exc:
                state = "conflict" if exc.status == 409 else "pending"
                if exc.status in {400, 413, 422}:
                    state = "rejected"
                with self.db:
                    self.db.execute(
                        "UPDATE pending SET attempts=attempts+1,next_attempt=?,state=? WHERE seq=?",
                        (now + min(300, 2 ** min(row["attempts"] + 1, 9)), state, row["seq"]),
                    )
                failed += 1
                if exc.status in {None, 401, 403, 429, 500, 502, 503, 504}:
                    break  # A shared outage should not hammer every queued session.
        return {"version": 1, "status": "partial" if failed else "ok",
                "sent": sent, "failed": failed, "queue": self.status()}
