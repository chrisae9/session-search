from dataclasses import replace

from session_search.capture.checkpoints import CheckpointCache
from session_search.capture.incremental import parse_incremental
from test_incremental_capture import full, message, write


def checkpoint(tmp_path):
    path = tmp_path / "example.jsonl"
    write(path, [message("user", "first"), message("assistant", "answer"),
                 message("user", "last")])
    return path, parse_incremental(path)[1]


def test_disk_roundtrip_resume_and_corruption_fallback(tmp_path):
    path, cp = checkpoint(tmp_path)
    root = tmp_path / "cache"
    assert CheckpointCache(root).save(path, cp)
    cached = CheckpointCache(root).load(path)
    assert cached == cp
    write(path, [message("assistant", "new answer")], "a")
    actual, _, work = parse_incremental(path, cached)
    assert actual == full(path) and work["mode"] == "incremental"
    entry = next(root.glob("*.cache"))
    assert entry.stat().st_mode & 0o777 == 0o600
    data = entry.read_bytes()
    entry.write_bytes(data[:40])
    assert CheckpointCache(root).load(path) is None
    assert parse_incremental(path, CheckpointCache(root).load(path))[0] == full(path)


def test_parser_change_and_key_mismatch_are_misses(tmp_path, monkeypatch):
    path, cp = checkpoint(tmp_path)
    cache = CheckpointCache(tmp_path / "cache")
    assert cache.save(path, cp)
    assert cache.load(tmp_path / "elsewhere.jsonl") is None
    monkeypatch.setattr("session_search.capture.checkpoints.parser_identity", lambda: "different")
    assert cache.load(path) is None


def test_bounded_replacement_eviction_and_unexpected_files(tmp_path):
    path, cp = checkpoint(tmp_path)
    root = tmp_path / "cache"
    cache = CheckpointCache(root)
    assert cache.save(path, cp)
    entry = next(root.glob("*.cache"))
    size = entry.stat().st_size
    cache = CheckpointCache(root, budget_bytes=size + 20)
    # Atomic replacement needs room for the old and new entry together.
    assert not cache.save(path, cp)
    assert cache.load(path) == cp
    other = tmp_path / "another.jsonl"
    assert cache.save(other, cp)
    assert cache.load(path) is None
    assert sum(p.stat().st_size for p in root.iterdir()) <= cache.budget
    unknown = root / "unmanaged"
    unknown.write_text("keep")
    assert not cache.save(path, cp)
    assert cache.load(other) is None
    assert unknown.read_text() == "keep"


def test_interrupted_publication_preserves_old_entry(tmp_path, monkeypatch):
    path, cp = checkpoint(tmp_path)
    cache = CheckpointCache(tmp_path / "cache")
    assert cache.save(path, cp)

    def fail(*args):
        raise OSError("simulated cache publication failure")

    monkeypatch.setattr("session_search.capture.checkpoints.os.replace", fail)
    assert not cache.save(path, replace(cp, completed_events=()))
    assert cache.load(path) == cp
    assert not list(cache.root.glob(".checkpoint-*"))


def test_oversized_decode_and_invalid_positions_are_rejected(tmp_path, monkeypatch):
    path, cp = checkpoint(tmp_path)
    cache = CheckpointCache(tmp_path / "cache")
    assert cache.save(path, cp)
    monkeypatch.setattr("session_search.capture.checkpoints.MAX_DECODED", 16)
    assert cache.load(path) is None
    assert not cache.save(path, cp)
    monkeypatch.setattr("session_search.capture.checkpoints.MAX_DECODED", 128 * 1024 ** 2)
    assert cache.save(path, replace(cp, turn_offset=cp.size + 1))
    assert cache.load(path) is None


def test_capture_reopen_raw_preservation_and_cache_loss(tmp_path):
    from session_search.capture.local import capture_file
    from session_search.storage.catalog import Catalog
    from session_search.storage.objects import ObjectStore
    path, _ = checkpoint(tmp_path)
    root = tmp_path / "catalog"
    with Catalog(root) as c:
        first = capture_file(c, path, "device", archive_raw=True, chunk_raw=True, incremental=True)
        assert first["parser"]["mode"] == "full"
        assert first["parser"]["checkpoint_saved"]
    write(path, [message("assistant", "appended reply")], "a")
    with Catalog(root) as c:
        second = capture_file(c, path, "device", archive_raw=True, chunk_raw=True, incremental=True)
        assert second["parser"]["mode"] == "incremental"
        expected = full(path)
        assert second["revision"] == expected.revision
        raw = c.db.execute("SELECT digest FROM raw_sources WHERE revision=?", (expected.revision,)).fetchone()[0]
        assert b"".join(ObjectStore(root).iter_bytes(raw)) == path.read_bytes()
    next((root / "parser-checkpoints").glob("*.cache")).write_bytes(b"broken")
    write(path, [message("user", "new task")], "a")
    with Catalog(root) as c:
        third = capture_file(c, path, "device", incremental=True)
        assert third["parser"]["mode"] == "full" and third["revision"] == full(path).revision
        assert path.exists()
        forced = capture_file(c, path, "device", incremental=True, force=True)
        assert forced["parser"]["mode"] == "full"


def test_cli_capture_uses_persistent_checkpoint_and_reports_work(tmp_path, capsys):
    import json
    from session_search.interfaces.cli import main
    home = tmp_path / "codex"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    path = sessions / "example.jsonl"
    write(path, [message("user", "first"), message("user", "last")])
    args = ["--data-dir", str(tmp_path / "data"), "capture", "--codex-home", str(home),
            "--producer", "test", "--incremental"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["parser"]["checkpoints_saved"] == 1
    write(path, [message("assistant", "appended")], "a")
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["parser"]["incremental"] == 1
    assert result["parser"]["reused_events"] == 1
