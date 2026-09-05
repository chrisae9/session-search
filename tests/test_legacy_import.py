import gzip

import pytest

from session_search.capture.legacy import import_archive
from session_search.core.records import Event, SearchQuery, SessionRevision, canonical_json, digest
from session_search.storage.catalog import Catalog


def record(payload, *, revision=0, tombstone=False, producer="device"):
    identity = {"schema_version": 1, "scope": "personal", "source_id": payload["source_id"],
                "source_record_id": payload["record_id"]}
    result = {**identity, "record_id": digest(canonical_json(identity).encode()),
              "record_type": "tombstone" if tombstone else "event", "revision": revision,
              "content_hash": digest(canonical_json(payload).encode())}
    result["revision_hash"] = digest(canonical_json(result).encode())
    return {**result, "producer_id": producer, "payload": payload}


def archive(root, records, *, sequence=1):
    raw = b"".join(sorted(canonical_json(row).encode() + b"\n" for row in records))
    compressed = gzip.compress(raw, mtime=0)
    key = digest(compressed)
    relative = f"objects/events/v1/{key}.jsonl.gz"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(compressed)
    manifest = {"schema_version": 1, "manifest_type": "event-batch", "producer_id": "device",
                "sequence": sequence, "objects": [{"schema_version": 1, "object_type": "events",
                    "path": relative, "sha256": key, "size": len(compressed), "record_count": len(records)}]}
    content = canonical_json(manifest).encode() + b"\n"
    path = root / "manifests/device" / f"{sequence:020d}-{digest(content)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return target


def payloads(source="codex"):
    common = {"schema_version": 1, "source_id": source, "session_id": "legacy-session",
              "native_session_id": "native", "scope": "personal"}
    return [{**common, "record_id": f"session:{source}:native", "record_type": "session",
             "project_slug": "demo", "session_slug": "historical work"},
            {**common, "record_id": f"{source}:native:turn:7:event:0", "record_type": "event",
             "turn_id": f"{source}:native:turn:7", "turn_index": 7, "sequence_index": 0,
             "event_index": 0, "role": "user", "user_origin": "user",
             "timestamp": "2026-01-01", "text": "retained recovery insight"}]


def test_import_preserves_historical_sources_and_old_turn_citations(tmp_path):
    source, destination = tmp_path / "legacy", tmp_path / "new"
    archive(source, [record(p) for name in ("codex", "claude-code") for p in payloads(name)])
    before = {str(p): digest(p.read_bytes()) for p in source.rglob("*") if p.is_file()}
    result = import_archive(source, destination)
    assert result["sessions"] == 2
    with Catalog(destination) as catalog:
        assert len(catalog.search(SearchQuery("insight", producer="device"))["results"]) == 2
        context = catalog.context([{"legacy_locator": "codex:native:turn:7"}])
        assert context["results"][0]["events"][0]["text"] == "retained recovery insight"
        catalog.ingest(SessionRevision("native", (Event("new", "user", "new content"),)),
                       producer="device", request_id="new")
        assert catalog.context([{"legacy_locator": "codex:native:turn:7"}])["status"] == "ok"
    assert before == {str(p): digest(p.read_bytes()) for p in source.rglob("*") if p.is_file()}


def test_tombstoned_legacy_evidence_is_not_resurrected(tmp_path):
    source, destination = tmp_path / "legacy", tmp_path / "new"
    session, event = payloads()
    archive(source, [record(session), record(event), record(event, revision=1, tombstone=True)])
    import_archive(source, destination)
    with Catalog(destination, readonly=True) as catalog:
        assert catalog.status()["sessions"] == 1
        assert not catalog.search(SearchQuery("insight"))["results"]


def test_corrupt_source_never_publishes_partial_destination(tmp_path):
    source, destination = tmp_path / "legacy", tmp_path / "new"
    target = archive(source, [record(p) for p in payloads()])
    target.write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        import_archive(source, destination)
    assert not destination.exists()


def test_conflicting_revision_and_existing_destination_fail_closed(tmp_path):
    source, destination = tmp_path / "legacy", tmp_path / "new"
    session, event = payloads()
    archive(source, [record(session), record(event), record({**event, "text": "different"})])
    with pytest.raises(ValueError, match="conflicting"):
        import_archive(source, destination)
    destination.mkdir()
    with pytest.raises(FileExistsError):
        import_archive(source, destination)


def test_conflicting_older_revision_is_detected_after_newer_revision(tmp_path):
    source, destination = tmp_path / "legacy", tmp_path / "new"
    session, event = payloads()
    archive(source, [record(session), record(event, revision=2)], sequence=1)
    archive(source, [record(event, revision=1)], sequence=2)
    archive(source, [record({**event, "text": "conflict"}, revision=1)], sequence=3)
    with pytest.raises(ValueError, match="conflicting"):
        import_archive(source, destination)
    assert not destination.exists()
    result = import_archive(source, destination, allow_superseded_conflicts=True)
    assert result["superseded_conflicts"] == 1
    with Catalog(destination, readonly=True) as catalog:
        assert catalog.search(SearchQuery("insight"))["results"]
        assert not catalog.search(SearchQuery("conflict"))["results"]


def test_superseded_conflict_option_never_permits_ambiguous_current_head(tmp_path):
    source, destination = tmp_path / "legacy", tmp_path / "new"
    session, event = payloads()
    archive(source, [record(session), record(event)], sequence=1)
    archive(source, [record({**event, "text": "conflict"})], sequence=2)
    with pytest.raises(ValueError, match="conflicting current"):
        import_archive(source, destination, allow_superseded_conflicts=True)
    assert not destination.exists()
