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
        finally:
            target.close()
        (stage / "catalog.sqlite3").chmod(0o600)
        with (stage / "catalog.sqlite3").open("rb") as f:
            os.fsync(f.fileno())
        source_objects = ObjectStore(catalog.root)
        target_objects = ObjectStore(stage)
        for key, size in objects:
            if not source_objects.verify(key):
                raise ValueError("snapshot references missing or corrupt raw evidence")
            source = source_objects.path(key)
            dest = target_objects.path(key)
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Immutable objects may be linked locally. Cross-device copies are
            # streamed by copyfile and verified before the snapshot is published.
            try:
                os.link(source, dest)
            except OSError:
                shutil.copyfile(source, dest)
                dest.chmod(0o600)
            with dest.open("rb") as f:
                os.fsync(f.fileno())
            sync_directory(dest.parent)
            if dest.stat().st_size != size:
                raise ValueError("raw object size mismatch")
        manifest = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                    "purpose": "search-replica" if search_only else "recovery",
                    "catalog_sha256": file_digest(stage / "catalog.sqlite3"),
                    "raw_objects": [{"digest": key, "size": size} for key, size in objects]}
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
    if manifest.get("version") != 1:
        raise ValueError("unsupported snapshot version")
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
            if not objects.verify(item["digest"]) or objects.path(item["digest"]).stat().st_size != item["size"]:
                raise ValueError("snapshot raw evidence is missing or corrupt")
        coverage = catalog.status()
    return {"version": 1, "status": "verified", "coverage": coverage,
            "purpose": purpose,
            "snapshot": digest(canonical_json(manifest).encode()),
            "created_at": manifest["created_at"]}


def activate_replica(snapshot: Path, replica: Path) -> dict:
    """Copy and verify before switching the small CURRENT pointer atomically."""
    verified = verify_snapshot(snapshot)
    replica = replica.resolve()
    if (replica / "catalog.sqlite3").exists():
        raise ValueError("refusing to activate a replica over a writable catalog")
    generations = replica / "generations"
    generations.mkdir(parents=True, exist_ok=True, mode=0o700)
    from session_search.storage.generations import publication_lock, publish_locked, prune_replica
    generation = generations / verified["snapshot"]
    stage = Path(tempfile.mkdtemp(prefix=".transfer-", dir=generations))
    try:
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
