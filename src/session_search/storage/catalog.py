"""Transactional canonical revisions and targeted lexical reads.

Historical evidence is immutable. Only the per-session search head moves forward;
source disappearance has no corresponding write operation.
"""

from __future__ import annotations

import gzip
import json
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path

from session_search.core.records import Citation, SearchQuery, SessionRevision, canonical_json, digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS publication_state (
 id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL, epoch INTEGER NOT NULL
);
INSERT OR IGNORE INTO publication_state VALUES (1,lower(hex(randomblob(16))),0);
CREATE TABLE IF NOT EXISTS revisions (
 session_id TEXT NOT NULL, revision TEXT NOT NULL, source TEXT NOT NULL,
 project TEXT NOT NULL, title TEXT NOT NULL, parent_session_id TEXT,
 is_subagent INTEGER NOT NULL, parser_version TEXT NOT NULL,
 event_count INTEGER NOT NULL, event_map BLOB NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(session_id, revision)
);
CREATE TABLE IF NOT EXISTS heads (
 session_id TEXT PRIMARY KEY, revision TEXT NOT NULL,
 FOREIGN KEY(session_id,revision) REFERENCES revisions(session_id,revision)
);
CREATE TABLE IF NOT EXISTS evidence (
 row_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, content_hash TEXT NOT NULL UNIQUE,
 event_id TEXT NOT NULL, role TEXT NOT NULL,
 text TEXT NOT NULL, timestamp TEXT NOT NULL, origin TEXT NOT NULL, kind TEXT NOT NULL,
 UNIQUE(session_id,event_id,content_hash)
);
CREATE INDEX IF NOT EXISTS evidence_identity ON evidence(session_id,event_id);
CREATE TABLE IF NOT EXISTS active_events (
 session_id TEXT NOT NULL, ordinal INTEGER NOT NULL, event_row INTEGER NOT NULL REFERENCES evidence(row_id),
 PRIMARY KEY(session_id,ordinal)
);
CREATE INDEX IF NOT EXISTS active_events_by_evidence ON active_events(event_row);
CREATE VIEW IF NOT EXISTS events AS SELECT b.*,h.revision,a.ordinal FROM evidence b
 JOIN active_events a ON a.event_row=b.row_id JOIN heads h ON h.session_id=a.session_id;
CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(text, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS producers (
 session_id TEXT NOT NULL, revision TEXT NOT NULL, producer TEXT NOT NULL,
 PRIMARY KEY(session_id,revision,producer),
 FOREIGN KEY(session_id,revision) REFERENCES revisions(session_id,revision)
);
CREATE TABLE IF NOT EXISTS receipts (
 producer TEXT NOT NULL, request_id TEXT NOT NULL, session_id TEXT NOT NULL,
 revision TEXT NOT NULL, PRIMARY KEY(producer,request_id)
);
CREATE TABLE IF NOT EXISTS checkpoints (
 path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, session_id TEXT NOT NULL,
 revision TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS raw_sources (
 session_id TEXT NOT NULL, revision TEXT NOT NULL, digest TEXT NOT NULL,
 size INTEGER NOT NULL, path TEXT NOT NULL, fingerprint TEXT NOT NULL,
 PRIMARY KEY(session_id,revision,digest,path),
 FOREIGN KEY(session_id,revision) REFERENCES revisions(session_id,revision)
);
CREATE TABLE IF NOT EXISTS legacy_citations (
 locator TEXT NOT NULL,session_id TEXT NOT NULL,revision TEXT NOT NULL,event_id TEXT NOT NULL,
 PRIMARY KEY(locator,session_id,revision,event_id),
 FOREIGN KEY(session_id,revision) REFERENCES revisions(session_id,revision)
);
CREATE TABLE IF NOT EXISTS project_aliases (
 session_id TEXT NOT NULL, project TEXT NOT NULL, alias TEXT NOT NULL,
 legacy_revision TEXT NOT NULL, native_revision TEXT NOT NULL,
 PRIMARY KEY(session_id,project,alias),
 FOREIGN KEY(session_id,legacy_revision) REFERENCES revisions(session_id,revision),
 FOREIGN KEY(session_id,native_revision) REFERENCES revisions(session_id,revision)
);
PRAGMA user_version = 2;
"""


def fts_expression(text: str) -> str:
    """Quote identifiers and preserve explicit phrases; never execute query syntax."""
    parts = re.findall(r'"([^"]+)"|(\S+)', text)
    values = [phrase or word for phrase, word in parts]
    return " OR ".join('"' + value.replace('"', '""') + '"' for value in values)


def match_offset(text: str, query: str) -> int:
    match = re.search(re.escape(query.strip('"')), text, re.IGNORECASE)
    if match:
        return match.start()
    candidates = []
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', query):
        value = phrase or word.rstrip("?!,;:")
        if not value:
            continue
        # A question's short words must not anchor snippets inside unrelated
        # words (for example, "we" inside "answer"). Prefer an explicit phrase,
        # then a longer query term; this affects presentation, never ranking.
        pattern = ((r"(?<!\w)" if value[0].isalnum() or value[0] == "_" else "")
                   + re.escape(value)
                   + (r"(?!\w)" if value[-1].isalnum() or value[-1] == "_" else ""))
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidates.append((bool(phrase), len(value), -match.start()))
    return -max(candidates)[2] if candidates else 0


def excerpt(text: str, query: str, limit: int = 800) -> str:
    position = match_offset(text, query)
    start = max(0, position - limit // 4)
    return ("…" if start else "") + text[start:start + limit] + (
        "…" if len(text) > start + limit else ""
    )


class Catalog:
    def __init__(self, root: Path, *, readonly: bool = False):
        self.root = Path(root)
        self.readonly = readonly
        self._generation_pin = None
        self._writer_pin = None
        current = self.root / "CURRENT"
        if current.exists():
            if not readonly:
                raise PermissionError("replica generations are read-only")
            from session_search.storage.generations import pin_current
            self.root, self._generation_pin = pin_current(self.root)
        try:
            self._open()
        except BaseException:
            self.close()
            raise

    def _open(self):
        readonly = self.readonly
        if not readonly and (self.root / "manifest.json").exists():
            raise PermissionError("verified snapshots are read-only; prepare a replacement primary explicitly")
        path = self.root / "catalog.sqlite3"
        if readonly:
            self.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
        else:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            from session_search.storage.fencing import acquire_writer
            self._writer_pin = acquire_writer(self.root)
            self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 2}:
            self.close()
            raise ValueError("unsupported catalog schema; upgrade Session Search")
        if not readonly:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript(SCHEMA)
            path.chmod(0o600)
        elif version != 2:
            self.close()
            raise ValueError("uninitialized catalog")
        self.has_project_aliases = bool(self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_aliases'"
        ).fetchone())

    def close(self):
        if hasattr(self, "db"):
            self.db.close()
        if self._writer_pin is not None:
            self._writer_pin.close()
            self._writer_pin = None
        if self._generation_pin is not None:
            self._generation_pin.close()
            self._generation_pin = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def ingest(self, session: SessionRevision, *, producer: str, request_id: str,
               checkpoint: tuple[str, str] | None = None, raw: dict | None = None) -> dict:
        if self.readonly:
            raise PermissionError("standby is read-only")
        if not producer or not request_id:
            raise ValueError("producer and idempotency key are required")
        revision = session.revision
        if raw:
            from session_search.storage.objects import ObjectStore
            objects = ObjectStore(self.root)
            if not objects.verify(raw["digest"]) or objects.size(raw["digest"]) != raw["size"]:
                raise ValueError("raw evidence must be durable and verified before acknowledgement")
        with self.db:
            prior = self.db.execute(
                "SELECT session_id,revision FROM receipts WHERE producer=? AND request_id=?",
                (producer, request_id),
            ).fetchone()
            if prior:
                if (prior["session_id"], prior["revision"]) != (session.session_id, revision):
                    raise ValueError("idempotency key reused for different content")
                return {"session_id": session.session_id, "revision": revision, "duplicate": True}
            exists = self.db.execute(
                "SELECT 1 FROM revisions WHERE session_id=? AND revision=?",
                (session.session_id, revision),
            ).fetchone()
            if not exists:
                previous_head = self.db.execute(
                    "SELECT r.revision,r.source,r.project,r.parser_version "
                    "FROM revisions r JOIN heads h USING(session_id,revision) "
                    "WHERE r.session_id=?", (session.session_id,),
                ).fetchone()
                previous_rows = {row[0] for row in self.db.execute(
                    "SELECT event_row FROM active_events WHERE session_id=?", (session.session_id,)
                )}
                self.db.execute("DELETE FROM active_events WHERE session_id=?", (session.session_id,))
                event_ids = []
                for index, event in enumerate(session.events):
                    content_hash = digest(canonical_json({"session_id": session.session_id,
                                                          **asdict(event)}).encode())
                    self.db.execute(
                        "INSERT OR IGNORE INTO evidence(session_id,content_hash,event_id,role,text,"
                        "timestamp,origin,kind) VALUES (?,?,?,?,?,?,?,?)",
                        (session.session_id, content_hash, event.event_id, event.role,
                         event.text, event.timestamp, event.origin, event.kind),
                    )
                    row_id = self.db.execute("SELECT row_id FROM evidence WHERE content_hash=?",
                                              (content_hash,)).fetchone()[0]
                    event_ids.append(row_id)
                    if row_id not in previous_rows:
                        self.db.execute("INSERT INTO evidence_fts(rowid,text) VALUES (?,?)",
                                        (row_id, event.text))
                    self.db.execute("INSERT INTO active_events VALUES (?,?,?)",
                                    (session.session_id, index, row_id))
                self.db.executemany("DELETE FROM evidence_fts WHERE rowid=?",
                                    ((row_id,) for row_id in previous_rows - set(event_ids)))
                self.db.execute(
                    "INSERT INTO revisions(session_id,revision,source,project,title,"
                    "parent_session_id,is_subagent,parser_version,event_count,event_map) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (session.session_id, revision, session.source, session.project, session.title,
                     session.parent_session_id, session.is_subagent, session.parser_version,
                     len(session.events), gzip.compress(canonical_json(event_ids).encode(), mtime=0)),
                )
                self.db.execute(
                    "INSERT INTO heads VALUES (?,?) ON CONFLICT(session_id) "
                    "DO UPDATE SET revision=excluded.revision", (session.session_id, revision),
                )
                # Legacy imports label projects with slugs; native capture uses
                # paths. Bind the migration label to this exact native project.
                if (previous_head and previous_head["source"] == session.source == "codex"
                        and previous_head["parser_version"] == "legacy-archive-v1"
                        and session.parser_version != "legacy-archive-v1"
                        and previous_head["project"] and session.project
                        and previous_head["project"] != session.project):
                    self.db.execute(
                        "INSERT OR IGNORE INTO project_aliases VALUES (?,?,?,?,?)",
                        (session.session_id, session.project, previous_head["project"],
                         previous_head["revision"], revision),
                    )
            self.db.execute("INSERT OR IGNORE INTO producers VALUES (?,?,?)",
                            (session.session_id, revision, producer))
            self.db.execute("INSERT INTO receipts VALUES (?,?,?,?)",
                            (producer, request_id, session.session_id, revision))
            if checkpoint:
                self.db.execute(
                    "INSERT INTO checkpoints VALUES (?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                    "fingerprint=excluded.fingerprint,session_id=excluded.session_id,"
                    "revision=excluded.revision",
                    (*checkpoint, session.session_id, revision),
                )
            if raw:
                self.db.execute("INSERT OR IGNORE INTO raw_sources VALUES (?,?,?,?,?,?)",
                                (session.session_id, revision, raw["digest"], raw["size"],
                                 raw["path"], raw["fingerprint"]))
            self.bump_publication()
        return {"session_id": session.session_id, "revision": revision, "duplicate": bool(exists)}

    def bump_publication(self):
        self.db.execute("UPDATE publication_state SET epoch=epoch+1 WHERE id=1")

    def publication(self) -> str | None:
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='publication_state'").fetchone():
            return None
        row = self.db.execute("SELECT identity,epoch FROM publication_state WHERE id=1").fetchone()
        return f"{row[0]}:{row[1]}" if row else None

    def fingerprint(self, path: str) -> str | None:
        row = self.db.execute("SELECT fingerprint FROM checkpoints WHERE path=?", (path,)).fetchone()
        return row[0] if row else None

    def has_raw(self, path: str, fingerprint: str) -> bool:
        return self.db.execute("SELECT 1 FROM raw_sources WHERE path=? AND fingerprint=?",
                               (path, fingerprint)).fetchone() is not None

    def scope(self, query: SearchQuery):
        conditions = []
        args: list = []
        if query.role:
            conditions.append("e.role=?")
            args.append(query.role)
            if query.role == "user":
                conditions.append("e.origin='user'")
        if query.project:
            project_condition = "instr(lower(r.project),lower(?))>0"
            args.append(query.project)
            if self.has_project_aliases:
                project_condition += (
                    " OR EXISTS(SELECT 1 FROM project_aliases pa WHERE "
                    "pa.session_id=r.session_id AND pa.project=r.project "
                    "AND instr(lower(pa.alias),lower(?))>0)"
                )
                args.append(query.project)
            conditions.append("(" + project_condition + ")")
        for value, condition in [
            (query.after, "e.timestamp>=?"), (query.before, "e.timestamp<?"),
            (query.session_id, "e.session_id=?"),
        ]:
            if value:
                conditions.append(condition)
                args.append(value)
        if not query.include_subagents:
            conditions.append("r.is_subagent=0")
        if query.producer:
            conditions.append("EXISTS(SELECT 1 FROM producers p WHERE p.session_id=e.session_id "
                              "AND p.revision=e.revision AND p.producer=?)")
            args.append(query.producer)
        cte = ""
        if query.exclude_sessions:
            placeholders = ",".join("(?)" for _ in query.exclude_sessions)
            cte = (f"WITH RECURSIVE excluded(id) AS (VALUES {placeholders} UNION "
                   "SELECT r.session_id FROM revisions r JOIN heads h "
                   "ON h.session_id=r.session_id AND h.revision=r.revision "
                   "JOIN excluded x ON r.parent_session_id=x.id) ")
            args = [*query.exclude_sessions, *args]
            conditions.append("e.session_id NOT IN (SELECT id FROM excluded)")
        return cte, conditions, args

    def search(self, query: SearchQuery) -> dict:
        cte, conditions, args = self.scope(query)
        if query.literal:
            conditions.append("instr(lower(e.text),lower(?))>0")
            args.append(query.text)
            ranking = "0.0"
            join_fts = ""
            from session_search.storage.literal import ready, terms
            candidate = terms(query.text)
            if candidate and ready(self.db):
                conditions.append("e.row_id IN (SELECT rowid FROM literal_fts WHERE literal_fts MATCH ? "
                                  "UNION SELECT rowid FROM literal_nul)")
                args.append(candidate)
        else:
            conditions.append("evidence_fts MATCH ?")
            args.append(fts_expression(query.text))
            ranking = "bm25(evidence_fts)"
            join_fts = "JOIN evidence_fts ON evidence_fts.rowid=e.row_id"
        rows = self.db.execute(
            cte + f"SELECT e.*,r.project,r.title,{ranking} AS rank FROM events e "
            "JOIN heads h ON h.session_id=e.session_id AND h.revision=e.revision "
            "JOIN revisions r ON r.session_id=e.session_id AND r.revision=e.revision "
            + join_fts + " WHERE " + " AND ".join(conditions)
            + " ORDER BY rank,e.timestamp DESC,e.session_id,e.ordinal LIMIT ?",
            (*args, query.limit + 1),
        ).fetchall()
        results = []
        for row in rows[:query.limit]:
            results.append({
                "citation": Citation(row["session_id"], row["revision"], row["event_id"],
                                     match_offset(row["text"], query.text)).to_dict(),
                "role": row["role"], "timestamp": row["timestamp"], "origin": row["origin"],
                "excerpt": excerpt(row["text"], query.text), "project": row["project"],
                "title": row["title"], "score": -row["rank"],
            })
        return {"version": 1, "status": "ok" if results else "no_matches",
                "mode": "keyword", "semantic_available": False, "results": results,
                "more_matches": len(rows) > query.limit, "coverage": self.status()}

    def context(self, citations: list[Citation | dict], *, neighbors: int = 2) -> dict:
        if len(citations) > 100 or not 0 <= neighbors <= 10:
            raise ValueError("context accepts at most 100 citations and 0–10 neighbors")
        results = []
        resolved = []
        for value in citations:
            if isinstance(value, Citation):
                resolved.append(value)
            elif set(value) == {"legacy_locator"}:
                matches = self.legacy_citations(value["legacy_locator"])
                if not matches:
                    results.append({"legacy_locator": value["legacy_locator"], "status": "unavailable"})
                resolved.extend(matches)
            else:
                resolved.append(Citation(**value))
        if len(resolved) > 100:
            raise ValueError("expanded legacy context exceeds 100 events; narrow the request")
        for cite in resolved:
            event_ids = self.revision_event_ids(cite.session_id, cite.revision)
            candidates = {row[0] for row in self.db.execute(
                "SELECT row_id FROM evidence WHERE session_id=? AND event_id=?",
                (cite.session_id, cite.event_id),
            )}
            position = next((i for i, row_id in enumerate(event_ids) if row_id in candidates), None)
            if position is None:
                results.append({"citation": cite.to_dict(), "status": "unavailable"})
                continue
            events = self.read_events(event_ids[max(0, position - neighbors):position + neighbors + 1])
            for event in events:
                if event["event_id"] == cite.event_id:
                    if cite.offset > len(event["text"]):
                        raise ValueError("citation offset is outside the cited event")
                    if len(event["text"]) > 4096:
                        start = max(0, cite.offset - 256)
                        event["text"] = event["text"][start:start + 4096]
                        event["text_offset"] = start
                        event["text_truncated"] = True
            results.append({"citation": cite.to_dict(), "status": "ok", "events": events})
        missing = sum(result["status"] != "ok" for result in results)
        return {"version": 1, "status": "partial" if missing else "ok", "results": results}

    def legacy_citations(self, locator: str) -> list[Citation]:
        rows = self.db.execute("SELECT session_id,revision,event_id FROM legacy_citations "
                               "WHERE locator=? ORDER BY event_id LIMIT 101", (locator,)).fetchall()
        if len(rows) > 100 or len({row["session_id"] for row in rows}) > 1:
            raise ValueError("legacy locator is ambiguous or too broad; use a specific event citation")
        return [Citation(**dict(row)) for row in rows]

    def revision_event_ids(self, session_id: str, revision: str) -> list[int]:
        row = self.db.execute("SELECT event_map FROM revisions WHERE session_id=? AND revision=?",
                               (session_id, revision)).fetchone()
        return json.loads(gzip.decompress(row[0])) if row else []

    def read_events(self, event_ids: list[int]) -> list[dict]:
        events = {}
        for start in range(0, len(event_ids), 500):
            batch = event_ids[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = self.db.execute("SELECT row_id,event_id,role,text,timestamp,origin,kind "
                                   f"FROM evidence WHERE row_id IN ({placeholders})", batch)
            for row in rows:
                value = dict(row)
                key = value.pop("row_id")
                events[key] = value
        if len(events) != len(event_ids):
            raise ValueError("revision references missing or duplicate evidence")
        return [events[row_id] for row_id in event_ids]

    def status(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) sessions,COALESCE(SUM(r.event_count),0) events FROM heads h "
            "JOIN revisions r ON h.session_id=r.session_id AND h.revision=r.revision"
        ).fetchone()
        result = {"sessions": row["sessions"], "events": row["events"],
                  "publication": self.publication(),
                  "write_fenced": (self.root / "FENCED.json").exists(),
                  "semantic_indexed": None,
                  "replication": {"status": "not_configured"}, "backup": "not_configured"}
        receipts = self.root / "replication-receipts"
        if receipts.exists():
            acknowledgements = []
            paths = sorted(receipts.glob("*.json"))
            errors = 0
            for path in paths[:16]:
                try:
                    value = json.loads(path.read_text())
                    acknowledgements.append({"host": value["destination"]["host"],
                        "publication": value["publication"], "verified_at": value.get("verified_at"),
                        "matches_current": value["publication"] == result["publication"]})
                except (OSError, ValueError, KeyError, TypeError):
                    errors += 1
            if acknowledgements:
                result["replication"] = {"status": "acknowledged" if all(
                    item["matches_current"] for item in acknowledgements) else "behind",
                    "acknowledgements": acknowledgements}
            if errors or len(paths) > 16:
                result["replication"].update(status="partial", unreadable_receipts=errors,
                                             more_destinations=len(paths) > 16)
        from session_search.storage.backup_cycle import cycle_status
        result["backup"] = cycle_status(self.root, result["publication"])
        manifest = self.root / "manifest.json"
        if manifest.exists():
            from session_search.core.records import digest
            value = json.loads(manifest.read_text())
            result["snapshot"] = digest(canonical_json(value).encode())
            result["snapshot_created_at"] = value["created_at"]
            result["snapshot_purpose"] = value.get("purpose", "recovery")
            result["replication"] = {"status": "replica" if self._generation_pin else "snapshot",
                                     "publication": result["publication"]}
        return result

    def export_revision(self, session_id: str, revision: str) -> dict:
        row = self.db.execute("SELECT * FROM revisions WHERE session_id=? AND revision=?",
                              (session_id, revision)).fetchone()
        if row is None:
            raise KeyError("revision unavailable")
        result = {key: row[key] for key in (
            "session_id", "source", "project", "title", "parent_session_id", "parser_version"
        )}
        result["is_subagent"] = bool(row["is_subagent"])
        result["events"] = self.read_events(self.revision_event_ids(session_id, revision))
        return json.loads(canonical_json(result))
