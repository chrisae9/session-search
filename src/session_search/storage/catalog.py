"""Transactional canonical revisions and targeted lexical reads.

Historical evidence is immutable. Only the per-session search head moves forward;
source disappearance has no corresponding write operation.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from session_search.core.records import Citation, SearchQuery, SessionRevision, canonical_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
 session_id TEXT NOT NULL, revision TEXT NOT NULL, source TEXT NOT NULL,
 project TEXT NOT NULL, title TEXT NOT NULL, parent_session_id TEXT,
 is_subagent INTEGER NOT NULL, parser_version TEXT NOT NULL,
 event_count INTEGER NOT NULL,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(session_id, revision)
);
CREATE TABLE IF NOT EXISTS heads (
 session_id TEXT PRIMARY KEY, revision TEXT NOT NULL,
 FOREIGN KEY(session_id,revision) REFERENCES revisions(session_id,revision)
);
CREATE TABLE IF NOT EXISTS events (
 row_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, revision TEXT NOT NULL,
 event_id TEXT NOT NULL, ordinal INTEGER NOT NULL, role TEXT NOT NULL,
 text TEXT NOT NULL, timestamp TEXT NOT NULL, origin TEXT NOT NULL, kind TEXT NOT NULL,
 UNIQUE(session_id,revision,event_id),
 FOREIGN KEY(session_id,revision) REFERENCES revisions(session_id,revision)
);
CREATE INDEX IF NOT EXISTS events_revision ON events(session_id,revision,ordinal);
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
PRAGMA user_version = 1;
"""


def fts_expression(text: str) -> str:
    """Quote identifiers and preserve explicit phrases; never execute query syntax."""
    parts = re.findall(r'"([^"]+)"|(\S+)', text)
    values = [phrase or word for phrase, word in parts]
    return " OR ".join('"' + value.replace('"', '""') + '"' for value in values)


def excerpt(text: str, query: str, limit: int = 800) -> str:
    needle = query.strip('"').casefold()
    position = text.casefold().find(needle)
    if position < 0:
        positions = [text.casefold().find(term.strip('"').casefold()) for term in query.split()]
        position = min((p for p in positions if p >= 0), default=0)
    start = max(0, position - limit // 4)
    return ("…" if start else "") + text[start:start + limit] + (
        "…" if len(text) > start + limit else ""
    )


class Catalog:
    def __init__(self, root: Path, *, readonly: bool = False):
        self.root = Path(root)
        self.readonly = readonly
        current = self.root / "CURRENT"
        if current.exists():
            if not readonly:
                raise PermissionError("replica generations are read-only")
            generation = current.read_text().strip()
            if not re.fullmatch("[0-9a-f]{64}", generation):
                raise ValueError("invalid replica generation pointer")
            self.root = self.root / "generations" / generation
        path = self.root / "catalog.sqlite3"
        if readonly:
            self.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
        else:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            self.close()
            raise ValueError("unsupported catalog schema; upgrade Session Search")
        if not readonly:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript(SCHEMA)
            path.chmod(0o600)
        elif version != 1:
            self.close()
            raise ValueError("uninitialized catalog")

    def close(self):
        self.db.close()

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
            if not objects.verify(raw["digest"]) or objects.path(raw["digest"]).stat().st_size != raw["size"]:
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
                self.db.execute(
                    "INSERT INTO revisions(session_id,revision,source,project,title,"
                    "parent_session_id,is_subagent,parser_version,event_count) VALUES (?,?,?,?,?,?,?,?,?)",
                    (session.session_id, revision, session.source, session.project, session.title,
                     session.parent_session_id, session.is_subagent, session.parser_version,
                     len(session.events)),
                )
                self.db.execute(
                    "DELETE FROM evidence_fts WHERE rowid IN (SELECT e.row_id FROM events e "
                    "JOIN heads h ON h.session_id=e.session_id AND h.revision=e.revision "
                    "WHERE e.session_id=?)", (session.session_id,),
                )
                for index, event in enumerate(session.events):
                    row = self.db.execute(
                        "INSERT INTO events(session_id,revision,event_id,ordinal,role,text,"
                        "timestamp,origin,kind) VALUES (?,?,?,?,?,?,?,?,?)",
                        (session.session_id, revision, event.event_id, index, event.role,
                         event.text, event.timestamp, event.origin, event.kind),
                    )
                    self.db.execute("INSERT INTO evidence_fts(rowid,text) VALUES (?,?)",
                                    (row.lastrowid, event.text))
                self.db.execute(
                    "INSERT INTO heads VALUES (?,?) ON CONFLICT(session_id) "
                    "DO UPDATE SET revision=excluded.revision", (session.session_id, revision),
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
        return {"session_id": session.session_id, "revision": revision, "duplicate": bool(exists)}

    def fingerprint(self, path: str) -> str | None:
        row = self.db.execute("SELECT fingerprint FROM checkpoints WHERE path=?", (path,)).fetchone()
        return row[0] if row else None

    def scope(self, query: SearchQuery):
        conditions = []
        args: list = []
        if query.role:
            conditions.append("e.role=?")
            args.append(query.role)
            if query.role == "user":
                conditions.append("e.origin='user'")
        for value, condition in [
            (query.after, "e.timestamp>=?"), (query.before, "e.timestamp<?"),
            (query.project, "instr(lower(r.project),lower(?))>0"),
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
                "citation": Citation(row["session_id"], row["revision"], row["event_id"]).to_dict(),
                "role": row["role"], "timestamp": row["timestamp"], "origin": row["origin"],
                "excerpt": excerpt(row["text"], query.text), "project": row["project"],
                "title": row["title"], "score": -row["rank"],
            })
        return {"version": 1, "status": "ok" if results else "no_matches",
                "mode": "keyword", "semantic_available": False, "results": results,
                "more_matches": len(rows) > query.limit, "coverage": self.status()}

    def context(self, citations: list[Citation], *, neighbors: int = 2) -> dict:
        if len(citations) > 100 or not 0 <= neighbors <= 10:
            raise ValueError("context accepts at most 100 citations and 0–10 neighbors")
        results = []
        for cite in citations:
            hit = self.db.execute(
                "SELECT ordinal FROM events WHERE session_id=? AND revision=? AND event_id=?",
                (cite.session_id, cite.revision, cite.event_id),
            ).fetchone()
            if not hit:
                results.append({"citation": cite.to_dict(), "status": "unavailable"})
                continue
            rows = self.db.execute(
                "SELECT event_id,role,text,timestamp,origin,kind FROM events "
                "WHERE session_id=? AND revision=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal",
                (cite.session_id, cite.revision, hit[0] - neighbors, hit[0] + neighbors),
            ).fetchall()
            events = [dict(row) for row in rows]
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

    def status(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) sessions,COALESCE(SUM(r.event_count),0) events FROM heads h "
            "JOIN revisions r ON h.session_id=r.session_id AND h.revision=r.revision"
        ).fetchone()
        result = {"sessions": row["sessions"], "events": row["events"],
                  "semantic_indexed": None,
                  "replication": "not_configured", "backup": "not_configured"}
        manifest = self.root / "manifest.json"
        if manifest.exists():
            from session_search.core.records import digest
            value = json.loads(manifest.read_text())
            result["snapshot"] = digest(canonical_json(value).encode())
            result["snapshot_created_at"] = value["created_at"]
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
        result["events"] = [dict(event) for event in self.db.execute(
            "SELECT event_id,role,text,timestamp,origin,kind FROM events "
            "WHERE session_id=? AND revision=? ORDER BY ordinal", (session_id, revision))]
        return json.loads(canonical_json(result))
