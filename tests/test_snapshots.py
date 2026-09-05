import json

import pytest

from session_search.capture.local import capture_file
from session_search.core.records import Citation, SearchQuery
from session_search.storage.catalog import Catalog
from session_search.storage.objects import ObjectStore
from session_search.storage.snapshots import activate_replica, create_snapshot, verify_snapshot
from test_capture import rollout


def test_raw_optin_snapshot_is_self_contained_after_source_disappears(tmp_path):
    path = tmp_path / "example.jsonl"
    raw = rollout("recover this evidence").encode()
    path.write_bytes(raw)
    root = tmp_path / "catalog"
    snapshot = tmp_path / "snapshot"
    with Catalog(root) as catalog:
        capture_file(catalog, path, "d", archive_raw=True)
        cite = Citation(**catalog.search(SearchQuery("recover"))["results"][0]["citation"])
        receipt = create_snapshot(catalog, snapshot)
    path.unlink()
    assert receipt["raw_objects"] == 1
    assert verify_snapshot(snapshot)["status"] == "verified"
    manifest = json.loads((snapshot / "manifest.json").read_text())
    assert ObjectStore(snapshot).path(manifest["raw_objects"][0]["digest"]).read_bytes() == raw
    with Catalog(snapshot, readonly=True) as catalog:
        assert catalog.context([cite])["results"][0]["status"] == "ok"


def test_corrupt_new_snapshot_cannot_replace_serving_replica(tmp_path):
    root = tmp_path / "catalog"
    path = tmp_path / "example.jsonl"
    path.write_text(rollout("old evidence"))
    replica = tmp_path / "replica"
    with Catalog(root) as catalog:
        capture_file(catalog, path, "d")
        create_snapshot(catalog, tmp_path / "first")
        activate_replica(tmp_path / "first", replica)
        current = (replica / "CURRENT").read_text()
        path.write_text(rollout("new evidence"))
        capture_file(catalog, path, "d")
        create_snapshot(catalog, tmp_path / "second")
    with (tmp_path / "second/catalog.sqlite3").open("ab") as f:
        f.write(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        activate_replica(tmp_path / "second", replica)
    assert (replica / "CURRENT").read_text() == current
    with Catalog(replica, readonly=True) as catalog:
        assert catalog.search(SearchQuery("old"))["results"]
    with pytest.raises(PermissionError):
        Catalog(replica)


def test_inflight_reader_keeps_old_generation_after_activation(tmp_path):
    root = tmp_path / "catalog"
    path = tmp_path / "example.jsonl"
    replica = tmp_path / "replica"
    path.write_text(rollout("old evidence"))
    with Catalog(root) as catalog:
        capture_file(catalog, path, "d")
        create_snapshot(catalog, tmp_path / "first")
        activate_replica(tmp_path / "first", replica)
        with Catalog(replica, readonly=True) as reader:
            old_cite = Citation(**reader.search(SearchQuery("old"))["results"][0]["citation"])
            path.write_text(rollout("new evidence"))
            capture_file(catalog, path, "d")
            create_snapshot(catalog, tmp_path / "second")
            activate_replica(tmp_path / "second", replica)
            assert reader.search(SearchQuery("old"))["results"]
        with Catalog(replica, readonly=True) as updated:
            assert updated.search(SearchQuery("new"))["results"]
            assert updated.context([old_cite])["results"][0]["events"][0]["text"] == "old evidence"
