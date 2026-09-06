"""Self-contained, verified snapshots for replication and backup input."""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from session_search.core.records import Event, SessionRevision, canonical_json, digest
from session_search.storage.catalog import Catalog
from session_search.storage.objects import ObjectStore, sync_directory


def file_digest(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def create_snapshot(catalog: Catalog, destination: Path, *, search_only: bool = False) -> dict:
    """SQLite's backup API takes a consistent view while ordinary writes continue."""
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError("snapshot destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=destination.parent))
    try:
        target = sqlite3.connect(stage / "catalog.sqlite3")
        try:
            catalog.db.backup(target)
            if search_only:
                target.execute("DELETE FROM raw_sources")
                target.commit()
            target.execute("PRAGMA journal_mode=DELETE")
            target.commit()
            objects = target.execute("SELECT DISTINCT digest,size FROM raw_sources ORDER BY digest").fetchall()
            publication = None
            if target.execute("SELECT 1 FROM sqlite_master WHERE name='publication_state'").fetchone():
                row = target.execute("SELECT identity,epoch FROM publication_state WHERE id=1").fetchone()
                publication = f"{row[0]}:{row[1]}" if row else None
        finally:
            target.close()
        (stage / "catalog.sqlite3").chmod(0o600)
        with (stage / "catalog.sqlite3").open("rb") as f:
            os.fsync(f.fileno())
        source_objects = ObjectStore(catalog.root)
        target_objects = ObjectStore(stage)
        contains_chunks = False
        for key, size in objects:
            contains_chunks = contains_chunks or source_objects.layout(key) == "chunks-v1"
            source_objects.copy_to(target_objects, key)
            if target_objects.size(key) != size:
                raise ValueError("raw object size mismatch")
        manifest = {"version": 2 if contains_chunks else 1, "created_at": datetime.now(timezone.utc).isoformat(),
                    "purpose": "search-replica" if search_only else "recovery",
                    "publication": publication,
                    "catalog_sha256": file_digest(stage / "catalog.sqlite3"),
                    "raw_objects": [{"digest": key, "size": size} for key, size in objects]}
        if contains_chunks:
            manifest["raw_storage"] = "files-and-chunks-v1"
        content = canonical_json(manifest)
        with (stage / "manifest.json").open("w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        sync_directory(stage)
        verify_snapshot(stage)
        os.rename(stage, destination)
        sync_directory(destination.parent)
        return {"version": 1, "status": "verified", "snapshot": digest(content.encode()),
                "raw_objects": len(objects), "purpose": manifest["purpose"]}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def verify_snapshot(path: Path) -> dict:
    path = path.resolve()
    manifest_path = path / "manifest.json"
    if manifest_path.is_symlink() or (path / "catalog.sqlite3").is_symlink():
        raise ValueError("snapshot files must not be symlinks")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("version") not in {1, 2}:
        raise ValueError("unsupported snapshot version")
    if manifest["version"] == 2 and manifest.get("raw_storage") != "files-and-chunks-v1":
        raise ValueError("unsupported snapshot raw storage")
    purpose = manifest.get("purpose", "recovery")
    if purpose not in {"recovery", "search-replica"}:
        raise ValueError("unsupported snapshot purpose")
    if purpose == "search-replica" and manifest["raw_objects"]:
        raise ValueError("search replica must not claim raw recovery coverage")
    if file_digest(path / "catalog.sqlite3") != manifest["catalog_sha256"]:
        raise ValueError("snapshot catalog checksum mismatch")
    with Catalog(path, readonly=True) as catalog:
        if catalog.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("snapshot database integrity check failed")
        if catalog.db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("snapshot database reference check failed")
        if catalog.has_project_aliases and catalog.db.execute(
            "SELECT 1 FROM project_aliases a "
            "JOIN revisions l ON l.session_id=a.session_id AND l.revision=a.legacy_revision "
            "JOIN revisions n ON n.session_id=a.session_id AND n.revision=a.native_revision "
            "WHERE l.source!='codex' OR n.source!='codex' "
            "OR l.parser_version!='legacy-archive-v1' OR n.parser_version='legacy-archive-v1' "
            "OR a.alias='' OR a.project='' OR a.alias!=l.project OR a.project!=n.project LIMIT 1"
        ).fetchone():
            raise ValueError("snapshot project alias provenance mismatch")
        for row in catalog.db.execute("SELECT session_id,revision FROM revisions"):
            value = catalog.export_revision(row["session_id"], row["revision"])
            value["events"] = tuple(Event(**event) for event in value["events"])
            if SessionRevision(**value).revision != row["revision"]:
                raise ValueError("snapshot canonical revision digest mismatch")
        expected = [{"digest": row[0], "size": row[1]} for row in catalog.db.execute(
            "SELECT DISTINCT digest,size FROM raw_sources ORDER BY digest")]
        if expected != manifest["raw_objects"]:
            raise ValueError("snapshot object manifest is incomplete")
        objects = ObjectStore(path)
        for item in expected:
            if manifest["version"] == 1 and objects.layout(item["digest"]) != "file":
                raise ValueError("chunked raw evidence requires snapshot version 2")
            if not objects.verify(item["digest"]) or objects.size(item["digest"]) != item["size"]:
                raise ValueError("snapshot raw evidence is missing or corrupt")
        coverage = catalog.status()
        if manifest.get("publication") != catalog.publication():
            raise ValueError("snapshot publication does not match its catalog")
    return {"version": 1, "status": "verified", "coverage": coverage,
            "purpose": purpose,
            "publication": manifest.get("publication"),
            "snapshot": digest(canonical_json(manifest).encode()),
            "created_at": manifest["created_at"]}


def activate_replica(snapshot: Path, replica: Path, *, consume: bool = False) -> dict:
    """Verify before atomic publication; managed incoming transfers can be moved."""
    verified = verify_snapshot(snapshot)
    replica = replica.resolve()
    if (replica / "catalog.sqlite3").exists():
        raise ValueError("refusing to activate a replica over a writable catalog")
    generations = replica / "generations"
    generations.mkdir(parents=True, exist_ok=True, mode=0o700)
    from session_search.storage.generations import pointer, publication_lock, publish_locked, prune_replica
    generation = generations / verified["snapshot"]
    stage = Path(tempfile.mkdtemp(prefix=".transfer-", dir=generations))
    try:
        if consume:
            # The sender retains its pending snapshot until acknowledgement.
            # Moving a completed local transfer avoids another full catalog copy.
            # A retry recreates incoming independently of published generations.
            os.rename(snapshot, stage)
            sync_directory(snapshot.parent)
        else:
            shutil.copytree(snapshot, stage, dirs_exist_ok=True, symlinks=False)
        copied = verify_snapshot(stage)
        if copied["snapshot"] != verified["snapshot"]:
            raise ValueError("snapshot changed during transfer")
        (stage / ".readers.lock").touch(mode=0o600)
        for path in stage.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        # Flush nested directories before making their parent visible.
        for path in sorted(stage.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir():
                sync_directory(path)
        sync_directory(stage)
        with publication_lock(replica, exclusive=True):
            current = pointer(replica, "CURRENT")
            if current:
                previous_manifest = json.loads((generations / current / "manifest.json").read_text())
                previous_publication = previous_manifest.get("publication")
                incoming_publication = verified.get("publication")
                if previous_publication:
                    if not incoming_publication:
                        raise ValueError("replica publication identity is missing")
                    old_identity, old_epoch = previous_publication.split(":")
                    new_identity, new_epoch = incoming_publication.split(":")
                    if new_identity != old_identity or int(new_epoch) < int(old_epoch):
                        raise ValueError("replica publication would change primary identity or move backward")
            if generation.exists():
                verify_snapshot(generation)
                if not (generation / ".readers.lock").is_file():
                    raise ValueError("generation predates reader pinning; initialize a new replica directory")
            else:
                os.rename(stage, generation)
                sync_directory(generations)
            publish_locked(replica, verified["snapshot"])
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    verified["retention"] = prune_replica(replica)
    return verified
