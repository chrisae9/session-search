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


def test_staging_uses_bounded_memory_for_large_incomplete_record(tmp_path):
    import tracemalloc
    from session_search.capture.local import copy_complete_records
    path, staged = tmp_path / 'large.jsonl', tmp_path / 'staged.jsonl'
    prefix = rollout('complete evidence').encode()
    with path.open('wb') as stream:
        stream.write(prefix)
        for _ in range(24):
            stream.write(b'x' * (1024 * 1024))
    tracemalloc.start()
    try:
        complete, partial = copy_complete_records(path, staged)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert partial and complete == len(prefix)
    assert staged.read_bytes() == prefix
    assert peak < 4 * 1024 * 1024


def test_complete_prefix_boundaries_and_strict_envelopes(tmp_path):
    import pytest
    from session_search.capture.local import copy_complete_records
    path, staged = tmp_path / 'example.jsonl', tmp_path / 'stage.jsonl'
    for data, expected in [(b'', b''), (b'no newline', b''), (b'{}\n', b'{}\n'),
                           (b'{}\r\n\nunfinished', b'{}\r\n\n')]:
        path.write_bytes(data)
        complete, partial = copy_complete_records(path, staged)
        assert staged.read_bytes() == expected
        assert complete == len(expected) and partial == (len(expected) < len(data))
    with Catalog(tmp_path / 'catalog') as catalog:
        for invalid in ('[]\n', 'null\n', '\n', '{private malformed text}\n'):
            path.write_text(rollout('uncommitted') + invalid)
            with pytest.raises(ValueError) as exc:
                capture_file(catalog, path, 'device')
            assert 'private malformed text' not in str(exc.value)
            assert catalog.fingerprint(str(path)) is None
            assert catalog.status()['sessions'] == 0


def test_staging_source_change_does_not_advance_checkpoint(tmp_path, monkeypatch):
    from session_search.capture import local
    path = tmp_path / 'example.jsonl'
    path.write_text(rollout('initial evidence'))
    copy = local.copy_complete_records

    def append_after_copy(source, destination):
        result = copy(source, destination)
        with source.open('a') as stream:
            stream.write('{}\n')
        return result

    monkeypatch.setattr(local, 'copy_complete_records', append_after_copy)
    with Catalog(tmp_path / 'catalog') as catalog:
        assert capture_file(catalog, path, 'device')['status'] == 'changed_during_read'
        assert catalog.fingerprint(str(path)) is None
        assert catalog.status()['sessions'] == 0
