"""Durable outbound work, separate from the full search catalog."""

import json
import fcntl
import sqlite3
import time
import shutil
import os
import re
import stat
from dataclasses import asdict
from contextlib import contextmanager, ExitStack
from pathlib import Path

from session_search.core.records import SessionRevision, canonical_json
from session_search.interfaces.client import Client, RemoteError


class UploadQueue:
    def __init__(self, root: Path):
        self.root = root
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
            CREATE TABLE IF NOT EXISTS raw_captures (
              path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS raw_acknowledgements (
              path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, digest TEXT NOT NULL,
              size INTEGER NOT NULL, session_id TEXT NOT NULL, revision TEXT NOT NULL
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
               checkpoint: tuple[str, str] | None = None, raw: dict | None = None) -> dict:
        revision = session.revision
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
                                      "session": asdict(session), **({"raw": raw} if raw else {})})
            existing = self.db.execute("SELECT revision FROM pending WHERE request_id=?",
                                       (request_id,)).fetchone()
            if existing and existing[0] != revision:
                raise ValueError("idempotency key reused for different content")
            self.db.execute(
                "INSERT OR IGNORE INTO pending(request_id,session_id,revision,payload) VALUES (?,?,?,?)",
                (request_id, session.session_id, revision, payload),
            )
            if checkpoint:
                self.db.execute("INSERT INTO captured VALUES (?,?) ON CONFLICT(path) "
                                "DO UPDATE SET fingerprint=excluded.fingerprint", checkpoint)
            if raw:
                self.db.execute("INSERT INTO raw_captures VALUES (?,?,?) ON CONFLICT(path) "
                                "DO UPDATE SET fingerprint=excluded.fingerprint,digest=excluded.digest",
                                (raw["path"], raw["fingerprint"], raw["digest"]))
        return {"session_id": session.session_id, "revision": revision,
                "duplicate": bool(existing), "upload": "queued"}

    def status(self) -> dict:
        rows = self.db.execute("SELECT state,COUNT(*) total FROM pending GROUP BY state").fetchall()
        return {"pending": sum(row["total"] for row in rows),
                "states": {row["state"]: row["total"] for row in rows}}

    def has_raw(self, path: str, fingerprint: str) -> bool:
        return self.db.execute("SELECT 1 FROM raw_captures WHERE path=? AND fingerprint=?",
                               (path, fingerprint)).fetchone() is not None

    @contextmanager
    def capture_guard(self):
        with (self.root / ".capture.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def compact(self) -> dict:
        """Reclaim acknowledged payload pages without discarding any queue records."""
        with ExitStack() as locks:
            for name in (".flush.lock", ".capture.lock"):
                lock = locks.enter_context((self.root / name).open("a"))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return {"version": 1, "status": "deferred", "reason": "client_busy"}
            if self.status()["pending"]:
                return {"version": 1, "status": "deferred", "reason": "pending_work"}
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            unused = self.db.execute("PRAGMA freelist_count").fetchone()[0] * page_size
            if unused < 1024 * 1024:
                return {"version": 1, "status": "unchanged", "reclaimed_bytes": 0}
            allocated = self.db.execute("PRAGMA page_count").fetchone()[0] * page_size
            if shutil.disk_usage(self.root).free < allocated * 3 + 64 * 1024 * 1024:
                return {"version": 1, "status": "deferred", "reason": "insufficient_scratch_space"}
            if self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                return {"version": 1, "status": "deferred", "reason": "database_busy"}
            path = self.root / "upload-queue.sqlite3"
            before = path.stat().st_size
            self.db.execute("VACUUM")
            checkpoint = self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint[0]:
                return {"version": 1, "status": "deferred", "reason": "checkpoint_busy"}
            return {"version": 1, "status": "compacted", "before_bytes": before,
                    "after_bytes": path.stat().st_size,
                    "reclaimed_bytes": max(0, before - path.stat().st_size)}

    def flush(self, client: Client, *, limit: int = 100, now: float | None = None,
              bootstrap_imports: bool = False, reconcile_raw_prefixes: bool = False) -> dict:
        if not 1 <= limit <= 1000:
            raise ValueError("flush limit must be between 1 and 1000")
        with (self.root / ".flush.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"version": 1, "status": "coalesced", "sent": 0, "failed": 0,
                        "queue": self.status()}
            bootstrapped = self._bootstrap_imports(client, limit) if bootstrap_imports else 0
            reconciled = self._reconcile_raw_prefixes(client, limit) if reconcile_raw_prefixes else 0
            result = self._flush(client, limit=limit, now=now)
            with self.capture_guard():
                try:
                    result["chunk_cleanup"] = self._prune_chunk_staging()
                except (OSError, ValueError):
                    result["chunk_cleanup"] = {"status": "deferred"}
            result["bootstrapped"] = bootstrapped
            result["reconciled"] = reconciled
            return result

    def _prune_chunk_staging(self) -> dict:
        """Caller holds flush and capture locks; only outbound staging is disposable."""
        from session_search.storage.chunks import ChunkStore
        from session_search.storage.objects import ObjectStore, sync_directory
        if (self.root / "catalog.sqlite3").exists():
            raise ValueError("client cleanup cannot run beside a search catalog")
        chunks = ChunkStore(self.root)
        # Validate every managed path before any removal. Unexpected layouts or
        # corrupt recipes defer cleanup, preserving evidence for inspection.
        members = {}
        temporaries = []
        for kind in ("recipes", "chunks"):
            directory = chunks.path(kind, "0" * 64).parent.parent
            members[kind] = {}
            if not directory.exists():
                continue
            for bucket in directory.iterdir():
                if (bucket.is_symlink() or not bucket.is_dir()
                        or not re.fullmatch('[0-9a-f]{2}', bucket.name)):
                    raise ValueError("unexpected staging directory")
                for path in bucket.iterdir():
                    if re.fullmatch(r'\.chunk-[a-z0-9_]{8}', path.name):
                        info = path.lstat()
                        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                                or stat.S_IMODE(info.st_mode) != 0o600):
                            raise ValueError("unexpected staging temporary")
                        temporaries.append(path)
                        continue
                    key = bucket.name + path.name
                    if chunks.path(kind, key) != path or not path.is_file():
                        raise ValueError("unexpected staging member")
                    members[kind][key] = path
        recipes = {key: chunks.recipe(key) for key in members["recipes"]}
        pending = {row[0] for row in self.db.execute(
            "SELECT DISTINCT json_extract(payload,'$.raw.digest') FROM pending "
            "WHERE json_type(payload,'$.raw')='object'")}
        retained = set()
        for key in pending:
            if key in recipes:
                retained.update(chunk["digest"] for chunk in recipes[key]["chunks"])
            elif ObjectStore(self.root).layout(key) != "file":
                raise ValueError("pending raw staging is unavailable")
        if not retained <= members["chunks"].keys():
            raise ValueError("pending raw chunks are unavailable")
        removed = 0
        # Publish recipe removals before deleting their now-unreferenced chunks.
        # An interrupted cleanup can safely repeat from the durable queue roots.
        for kind, keep in (("recipes", pending), ("chunks", retained)):
            for key, path in members[kind].items():
                if key not in keep:
                    path.unlink()
                    sync_directory(path.parent)
                    removed += 1
        # mkstemp files can survive process death before or after hard-link
        # publication. Neither queue payloads nor recipes address these names.
        # Validate all durable roots above before reclaiming these aliases.
        for path in temporaries:
            path.unlink()
            sync_directory(path.parent)
        return {"status": "complete", "removed_members": removed,
                "removed_temporaries": len(temporaries)}

    def _reconcile_raw_prefixes(self, client: Client, limit: int) -> int:
        import hashlib
        from session_search.storage.objects import ObjectStore
        rows = self.db.execute(
            "SELECT p.seq,p.session_id,json_extract(p.payload,'$.raw') raw FROM pending p "
            "WHERE p.state IN ('pending','conflict') AND json_extract(p.payload,'$.session.source')='codex' "
            "AND json_type(p.payload,'$.raw')='object' "
            "AND NOT EXISTS(SELECT 1 FROM pending older WHERE older.session_id=p.session_id "
            "AND older.seq<p.seq) ORDER BY p.seq LIMIT ?", (limit,),
        ).fetchall()
        reconciled = 0
        for start in range(0, len(rows), 10):
            batch = rows[start:start + 10]
            heads = client.recovery_heads([row['session_id'] for row in batch])
            for row in batch:
                if row['session_id'] not in heads:
                    with self.db:
                        self.db.execute(
                            "UPDATE pending SET payload=json_set(payload,'$.expected_revision',NULL),"
                            "state='pending',next_attempt=0 WHERE seq=?", (row['seq'],),
                        )
                    reconciled += 1
                    continue  # Commit still compares against an absent head.
                head = heads.get(row['session_id'], {})
                previous = head.get('raw')
                raw = json.loads(row['raw'])
                if (head.get('source') != 'codex' or not previous
                        or raw['size'] < previous['size']):
                    continue
                hasher = hashlib.sha256()
                remaining = previous['size']
                try:
                    for chunk in ObjectStore(self.root).iter_bytes(raw['digest']):
                        prefix = chunk[:remaining]
                        hasher.update(prefix)
                        remaining -= len(prefix)
                except (OSError, ValueError):
                    continue  # Retain the conflict if staging cannot prove ancestry.
                if remaining or hasher.hexdigest() != previous['digest']:
                    continue
                with self.db:
                    self.db.execute(
                        "UPDATE pending SET payload=json_set(payload,'$.expected_revision',?),"
                        "state='pending',next_attempt=0 WHERE seq=?", (head['revision'], row['seq']),
                    )
                reconciled += 1
        return reconciled

    def _bootstrap_imports(self, client: Client, limit: int) -> int:
        rows = self.db.execute("SELECT p.seq,p.session_id FROM pending p "
            "WHERE p.state IN ('pending','conflict') "
            "AND json_extract(p.payload,'$.expected_revision') IS NULL "
            "AND json_extract(p.payload,'$.session.source')='codex' "
            "AND NOT EXISTS(SELECT 1 FROM pending older WHERE older.session_id=p.session_id "
            "AND older.seq<p.seq) ORDER BY p.seq LIMIT ?", (limit,)).fetchall()
        updated = 0
        for start in range(0, len(rows), 100):
            batch = rows[start:start + 100]
            heads = client.migration_heads([r['session_id'] for r in batch])
            with self.db:
                for row in batch:
                    head = heads.get(row['session_id'], {})
                    if head.get('source') != 'codex' or head.get('parser_version') != 'legacy-archive-v1':
                        continue
                    # The server still compares this exact revision at commit.
                    # Another client's intervening update therefore stays a conflict.
                    self.db.execute("UPDATE pending SET payload=json_set(payload,'$.expected_revision',?),"
                        "state='pending',next_attempt=0 WHERE seq=?",
                        (head['revision'], row['seq']))
                    updated += 1
        return updated

    def _flush(self, client: Client, *, limit: int, now: float | None) -> dict:
        now = time.time() if now is None else now
        sent = 0
        failed = 0
        # Only the oldest outstanding revision per session can be sent. A conflict
        # blocks that session; unrelated histories can continue to progress.
        rows = self.db.execute(
            "SELECT p.seq,p.revision,p.session_id,p.attempts FROM pending p "
            "WHERE p.state='pending' AND p.next_attempt<=? AND "
            "NOT EXISTS(SELECT 1 FROM pending older WHERE older.session_id=p.session_id "
            "AND older.seq<p.seq) ORDER BY p.seq LIMIT ?", (now, limit),
        ).fetchall()
        for row in rows:
            try:
                serialized = self.db.execute("SELECT payload FROM pending WHERE seq=?", (row['seq'],)).fetchone()
                payload = json.loads(serialized[0])
                if payload.get("raw"):
                    from session_search.storage.objects import ObjectStore
                    raw = payload["raw"]
                    objects = ObjectStore(self.root)
                    if objects.layout(raw["digest"]) == "chunks-v1":
                        client.upload_raw_store(objects, raw["digest"])
                    else:
                        client.upload_raw(objects.path(raw["digest"]), raw["digest"])
                receipt = client.upload(payload)
                if receipt.get("status") != "durable" or receipt.get("revision") != row["revision"]:
                    raise RemoteError(502)
                with self.db:
                    self.db.execute("INSERT INTO acknowledged VALUES (?,?) ON CONFLICT(session_id) "
                                    "DO UPDATE SET revision=excluded.revision",
                                    (row["session_id"], row["revision"]))
                    if payload.get('raw'):
                        raw = payload['raw']
                        # Retain exact recovery requirements in the same commit
                        # that retires the acknowledged outbound payload.
                        self.db.execute(
                            'INSERT INTO raw_acknowledgements VALUES (?,?,?,?,?,?) ON CONFLICT(path) '
                            'DO UPDATE SET fingerprint=excluded.fingerprint,digest=excluded.digest,'
                            'size=excluded.size,session_id=excluded.session_id,revision=excluded.revision',
                            (raw['path'], raw['fingerprint'], raw['digest'], raw['size'],
                             row['session_id'], row['revision']),
                        )
                    self.db.execute("DELETE FROM pending WHERE seq=?", (row["seq"],))
                if payload.get("raw"):
                    with self.capture_guard():
                        still_pending = self.db.execute(
                            "SELECT 1 FROM pending WHERE json_extract(payload,'$.raw.digest')=? LIMIT 1",
                            (payload["raw"]["digest"],),
                        ).fetchone()
                        if not still_pending:
                            # Only the transfer copy; native files remain untouched.
                            try:
                                ObjectStore(self.root).path(payload["raw"]["digest"]).unlink(missing_ok=True)
                            except OSError:
                                pass  # Retaining an extra cache copy is safe; retry GC later.
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
