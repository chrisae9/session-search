import shutil

from session_search.capture.local import capture_file, capture_home
from session_search.storage.catalog import Catalog
from test_capture import rollout


def test_low_space_defers_before_staging_or_checkpoint(tmp_path, monkeypatch):
    source = tmp_path / 'example.jsonl'
    source.write_text(rollout('retained native'))
    root = tmp_path / 'catalog'
    with Catalog(root) as catalog:
        monkeypatch.setattr('session_search.capture.local.shutil.disk_usage',
                            lambda path: shutil._ntuple_diskusage(100, 100, 0))
        result = capture_file(catalog, source, 'd', archive_raw=True, chunk_raw=True)
        assert result['reason'] == 'insufficient_staging_space'
        assert catalog.fingerprint(str(source)) is None
        assert not list(root.glob('.capture-*'))
        assert source.exists()


def test_budget_resumes_uncaptured_files_and_skips_unchanged_for_free(tmp_path):
    home = tmp_path / 'codex'
    sessions = home / 'sessions'
    sessions.mkdir(parents=True)
    paths = [sessions / (sid + '.jsonl') for sid in ('a', 'b')]
    for path in paths:
        path.write_text(rollout('budget evidence', sid=path.stem))
    budget = paths[0].stat().st_size
    with Catalog(tmp_path / 'catalog') as catalog:
        first = capture_home(catalog, home, 'd', max_bytes=budget)
        assert first['status'] == 'partial'
        assert first['counts'] == {'captured': 1, 'deferred': 1}
        assert first['admitted_source_bytes'] == budget
        second = capture_home(catalog, home, 'd', max_bytes=budget)
        assert second['counts'] == {'unchanged': 1, 'captured': 1}
        assert second['status'] == 'complete'
        assert capture_home(catalog, home, 'd', max_bytes=0)['counts'] == {'unchanged': 2}


def test_failed_parse_consumes_run_budget(tmp_path):
    home = tmp_path / 'codex'
    sessions = home / 'sessions'
    sessions.mkdir(parents=True)
    bad = sessions / 'a.jsonl'
    good = sessions / 'b.jsonl'
    bad.write_text('{invalid}\n')
    good.write_text(rollout('later', sid='b'))
    with Catalog(tmp_path / 'catalog') as catalog:
        result = capture_home(catalog, home, 'd', max_bytes=good.stat().st_size)
        assert len(result['errors']) == 1
        assert result['counts'] == {'deferred': 1}
        assert catalog.fingerprint(str(good)) is None
        assert result['admitted_source_bytes'] == bad.stat().st_size


def test_staging_uses_catalog_filesystem(tmp_path, monkeypatch):
    source = tmp_path / 'example.jsonl'
    source.write_text(rollout('staged locally'))
    import session_search.capture.local as capture
    original = capture.copy_complete_records
    root = tmp_path / 'catalog'

    def observe(path, destination):
        assert destination.parent.parent == root
        return original(path, destination)

    monkeypatch.setattr(capture, 'copy_complete_records', observe)
    with Catalog(root) as catalog:
        assert capture_file(catalog, source, 'd')['status'] == 'captured'
    assert not list(root.glob('.capture-*'))
