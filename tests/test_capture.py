import json

from session_search.capture.local import capture_file, capture_home
from session_search.core.records import SearchQuery
from session_search.storage.catalog import Catalog


def rollout(text, sid="example"):
    return "\n".join(json.dumps(row) for row in [
        {"type": "session_meta", "payload": {"id": sid, "cwd": "/project"}},
        {"type": "response_item", "timestamp": "2026-01-01T12:00:00Z", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}},
    ]) + "\n"


def test_partial_tail_retried_and_local_disappearance_never_deletes(tmp_path):
    home = tmp_path / "codex"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    path = sessions / "example.jsonl"
    path.write_text(rollout("backup discovery") + '{"type":')
    with Catalog(tmp_path / "catalog") as catalog:
        result = capture_file(catalog, path, "device")
        assert result["partial_tail"]
        assert catalog.search(SearchQuery("discovery"))["results"]
        assert capture_file(catalog, path, "device")["status"] == "unchanged"
        path.write_text(rollout("new evidence"))
        capture_file(catalog, path, "device")
        assert catalog.search(SearchQuery("evidence"))["results"]
        path.unlink()
        assert capture_home(catalog, home, "device")["status"] == "empty"
        assert catalog.search(SearchQuery("evidence"))["results"]


def test_malformed_complete_line_preserves_last_good_state(tmp_path):
    path = tmp_path / "example.jsonl"
    path.write_text(rollout("retained evidence"))
    with Catalog(tmp_path / "catalog") as catalog:
        capture_file(catalog, path, "device")
        previous = catalog.fingerprint(str(path))
        path.write_text(rollout("new evidence") + "{broken}\n")
        import pytest
        with pytest.raises(ValueError):
            capture_file(catalog, path, "device")
        assert catalog.fingerprint(str(path)) == previous
        assert catalog.search(SearchQuery("retained"))["results"]


def test_normalized_credentials_are_redacted(tmp_path):
    path = tmp_path / "example.jsonl"
    path.write_text(rollout("Authorization: Bearer " + "x" * 30))
    with Catalog(tmp_path / "catalog") as catalog:
        capture_file(catalog, path, "device")
        assert not catalog.search(SearchQuery("x" * 30, literal=True))["results"]
        assert catalog.search(SearchQuery("REDACTED"))["results"]
