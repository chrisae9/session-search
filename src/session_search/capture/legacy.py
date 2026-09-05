"""One-way import of the original immutable archive into a separate new store.

The source is never opened for writing. An on-disk merge table bounds memory;
only a complete, verified import is published as the destination directory.
"""

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path, PurePosixPath

from session_search.capture.parsers.base import redact_sensitive_text
from session_search.core.records import Event, SessionRevision, canonical_json, digest
from session_search.storage.catalog import Catalog
from session_search.storage.objects import sync_directory

MANIFEST_NAME = re.compile(r"([0-9]{20})-([0-9a-f]{64})\.json")
RECORD_KEYS = {"schema_version", "record_type", "record_id", "revision", "revision_hash",
               "content_hash", "producer_id", "source_id", "scope", "source_record_id", "payload"}


def manifests(root: Path) -> list[Path]:
    directory = root / "manifests"
    if not directory.is_dir():
        raise ValueError("legacy archive has no manifest directory")
    result = []
    for producer in sorted(directory.iterdir()):
        if producer.name.startswith("."):
            continue
        if producer.is_symlink() or not producer.is_dir():
            raise ValueError("invalid legacy producer directory")
        for path in sorted(producer.iterdir()):
            if path.name.startswith("."):
                continue
            if path.is_symlink() or not path.is_file() or not MANIFEST_NAME.fullmatch(path.name):
                raise ValueError("invalid legacy manifest entry")
            result.append(path)
    return result


def validate_record(record: dict):
    if set(record) != RECORD_KEYS:
        raise ValueError("legacy record fields do not match schema version 1")
    if record.get("schema_version") != 1 or record.get("record_type") not in {"event", "tombstone"}:
        raise ValueError("unsupported legacy record")
    if not isinstance(record.get("revision"), int) or isinstance(record["revision"], bool) or record["revision"] < 0:
        raise ValueError("invalid legacy revision")
    identity = {key: record[key] for key in ("schema_version", "scope", "source_id", "source_record_id")}
    if digest(canonical_json(identity).encode()) != record["record_id"]:
        raise ValueError("legacy record identity mismatch")
    if digest(canonical_json(record["payload"]).encode()) != record["content_hash"]:
        raise ValueError("legacy payload checksum mismatch")
    identity.update({key: record[key] for key in ("record_type", "record_id", "revision", "content_hash")})
    if digest(canonical_json(identity).encode()) != record["revision_hash"]:
        raise ValueError("legacy revision checksum mismatch")


def iter_records(root: Path, paths: list[Path]):
    seen_objects, seen_sequences = {}, set()
    for path in paths:
        match = MANIFEST_NAME.fullmatch(path.name)
        content = path.read_bytes()
        if digest(content) != match[2]:
            raise ValueError("legacy manifest checksum mismatch")
        manifest = json.loads(content)
        if set(manifest) != {"schema_version", "manifest_type", "producer_id", "sequence", "objects"}:
            raise ValueError("legacy manifest fields do not match schema version 1")
        if canonical_json(manifest).encode() + b"\n" != content:
            raise ValueError("legacy manifest is not canonical JSON")
        if (manifest["schema_version"] != 1 or manifest["manifest_type"] != "event-batch"
                or manifest["producer_id"] != path.parent.name or manifest["sequence"] != int(match[1])):
            raise ValueError("legacy manifest identity mismatch")
        sequence = (manifest["producer_id"], manifest["sequence"])
        if sequence in seen_sequences:
            raise ValueError("conflicting legacy producer sequence")
        seen_sequences.add(sequence)
        if not isinstance(manifest["objects"], list) or not manifest["objects"]:
            raise ValueError("legacy manifest contains no object references")
        for reference in manifest["objects"]:
            if set(reference) != {"schema_version", "object_type", "path", "sha256", "size", "record_count"}:
                raise ValueError("legacy object fields do not match schema version 1")
            relative = PurePosixPath(reference["path"])
            expected = f"objects/events/v1/{reference['sha256']}.jsonl.gz"
            if str(relative) != expected or not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]):
                raise ValueError("invalid legacy object path")
            if reference["schema_version"] != 1 or reference["object_type"] != "events":
                raise ValueError("unsupported legacy object")
            if expected in seen_objects:
                if seen_objects[expected] != reference:
                    raise ValueError("legacy manifests disagree about an object")
                continue
            seen_objects[expected] = reference
            source = root / relative
            if source.is_symlink() or source.stat().st_size != reference["size"]:
                raise ValueError("legacy object size mismatch")
            with source.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != reference["sha256"]:
                    raise ValueError("legacy object checksum mismatch")
            count, previous = 0, None
            with gzip.open(source, "rb") as stream:
                for line in stream:
                    if not line.endswith(b"\n") or (previous is not None and line <= previous):
                        raise ValueError("legacy object is not ordered canonical JSONL")
                    record = json.loads(line)
                    if canonical_json(record).encode() + b"\n" != line:
                        raise ValueError("legacy record is not canonical JSON")
                    validate_record(record)
                    count += 1
                    previous = line
                    yield record
            if count != reference["record_count"]:
                raise ValueError("legacy object record count mismatch")


def import_archive(source: Path, destination: Path) -> dict:
    source, destination = source.resolve(), destination.resolve()
    if destination.exists():
        raise FileExistsError("legacy import requires a new destination; existing stores are never replaced")
    if source == destination or source in destination.parents:
        raise ValueError("import destination must be outside the legacy archive")
    paths = manifests(source)
    frontier = [(str(path.relative_to(source)), digest(path.read_bytes())) for path in paths]
    destination.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix=".legacy-import-", dir=destination.parent))
    spool = sqlite3.connect(workspace / "merge.sqlite3")
    imported = records = 0
    try:
        spool.executescript("""
            CREATE TABLE latest(id TEXT PRIMARY KEY,revision INTEGER,hash TEXT,kind TEXT,
              source TEXT,native TEXT,payload TEXT);
            CREATE TABLE producers(id TEXT,revision INTEGER,producer TEXT,PRIMARY KEY(id,revision,producer));
            CREATE TABLE revision_hashes(id TEXT,revision INTEGER,hash TEXT,PRIMARY KEY(id,revision));
            CREATE INDEX sessions ON latest(source,native);
        """)
        for record in iter_records(source, paths):
            prior = spool.execute("SELECT hash FROM revision_hashes WHERE id=? AND revision=?",
                                  (record["record_id"], record["revision"])).fetchone()
            if prior and prior[0] != record["revision_hash"]:
                raise ValueError("conflicting legacy record revision")
            spool.execute("INSERT OR IGNORE INTO revision_hashes VALUES (?,?,?)",
                          (record["record_id"], record["revision"], record["revision_hash"]))
            payload = record["payload"]
            spool.execute("INSERT INTO latest VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "revision=excluded.revision,hash=excluded.hash,kind=excluded.kind,source=excluded.source,"
                "native=excluded.native,payload=excluded.payload WHERE excluded.revision>latest.revision",
                (record["record_id"], record["revision"], record["revision_hash"], record["record_type"],
                 record["source_id"], payload.get("native_session_id") or payload.get("session_id"),
                 canonical_json(payload)))
            spool.execute("INSERT OR IGNORE INTO producers VALUES (?,?,?)",
                          (record["record_id"], record["revision"], record["producer_id"]))
            records += 1
            if records % 1000 == 0:
                spool.commit()
        spool.commit()
        with Catalog(workspace / "catalog") as catalog:
            sessions = spool.execute("SELECT source,native,payload FROM latest WHERE kind='event' "
                                      "AND json_extract(payload,'$.record_type')='session' ORDER BY source,native")
            for source_id, native, serialized in sessions:
                metadata = json.loads(serialized)
                sid = native if source_id == "codex" else f"{source_id}:{native}"
                rows = spool.execute("SELECT id,revision,payload FROM latest WHERE source=? AND native=? "
                    "AND kind='event' AND json_extract(payload,'$.record_type')='event' ORDER BY "
                    "COALESCE(json_extract(payload,'$.sequence_index'),json_extract(payload,'$.turn_index')),"
                    "json_extract(payload,'$.event_index'),id", (source_id, native)).fetchall()
                events, aliases, producers = [], [], set()
                for record_id, record_revision, serialized_event in rows:
                    value = json.loads(serialized_event)
                    role = value.get("role", "context")
                    if role not in {"user", "assistant", "context"}:
                        role = "tool"
                    event = Event(value["record_id"], role, redact_sensitive_text(value.get("text", "")),
                                  value.get("timestamp") or "", value.get("user_origin") or role,
                                  value.get("event_type", "message"))
                    events.append(event)
                    for locator in (value["record_id"], value.get("turn_id")):
                        if locator:
                            aliases.append((locator, event.event_id))
                    for row in spool.execute("SELECT producer FROM producers WHERE id=? AND revision=?",
                                              (record_id, record_revision)):
                        producers.add(row[0])
                parent = metadata.get("parent_session_id")
                if parent and source_id != "codex":
                    parent = f"{source_id}:{parent}"
                session = SessionRevision(sid, tuple(events), source_id,
                    metadata.get("project_slug") or "", metadata.get("session_slug") or "",
                    parent, bool(metadata.get("is_subagent")), "legacy-archive-v1")
                revision = session.revision
                for producer in sorted(producers or {"legacy-import"}):
                    catalog.ingest(session, producer=producer, request_id=f"legacy:{sid}:{revision}")
                with catalog.db:
                    catalog.db.executemany("INSERT OR IGNORE INTO legacy_citations VALUES (?,?,?,?)",
                        ((locator, sid, revision, event_id) for locator, event_id in aliases))
                imported += 1
            catalog.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # Reject a moving source frontier. This import can be retried safely;
        # no partial destination has become visible and no source was mutated.
        if [(str(path.relative_to(source)), digest(path.read_bytes())) for path in manifests(source)] != frontier:
            raise ValueError("legacy archive changed during import; retry from a stable snapshot")
        with (workspace / "catalog/catalog.sqlite3").open("rb") as stream:
            os.fsync(stream.fileno())
        sync_directory(workspace / "catalog")
        os.rename(workspace / "catalog", destination)
        sync_directory(destination.parent)
        return {"version": 1, "status": "imported", "sessions": imported, "records_read": records,
                "legacy_frontier": digest(canonical_json(frontier).encode()), "raw_archived": False}
    finally:
        spool.close()
        shutil.rmtree(workspace)
