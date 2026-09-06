import json

from session_search.interfaces.capture_status import capture_status


def test_sync_summary_is_bounded_and_excludes_error_details(tmp_path):
    receipt = {'version': 1, 'status': 'partial', 'completed_at': '2026-09-06T00:00:00+00:00',
               'capture': {'counts': {'captured': 2, 'deferred': 3, 'unexpected': 'private'},
                           'errors': [{'error': 'private details'}]}}
    path = tmp_path / 'sync-status.json'
    path.write_text(json.dumps(receipt))
    result = capture_status(tmp_path)
    assert result['status'] == 'partial'
    assert result['counts'] == {'captured': 2, 'deferred': 3}
    assert result['capture_error_count'] == 1
    assert 'private' not in json.dumps(result)
    assert json.loads(path.read_text()) == receipt


def test_missing_invalid_and_symlink_receipts_do_not_bootstrap(tmp_path):
    assert capture_status(tmp_path) == {'status': 'not_observed'}
    assert list(tmp_path.iterdir()) == []
    path = tmp_path / 'sync-status.json'
    for text in ('{invalid', '{}', '[]', 'x' * (1024 * 1024 + 1)):
        path.write_text(text)
        assert capture_status(tmp_path) == {'status': 'unavailable'}
    path.unlink()
    target = tmp_path / 'target'
    target.write_text('private')
    path.symlink_to(target)
    assert capture_status(tmp_path) == {'status': 'unavailable'}
    assert target.read_text() == 'private'


def test_parser_work_summary_excludes_details_and_rejects_invalid_counts(tmp_path):
    receipt = {'version': 1, 'status': 'complete', 'completed_at': '2026-09-06T00:00:00+00:00',
               'capture': {'parser': {'full': 1, 'incremental': 2, 'reused_events': 30,
                                      'checkpoints_saved': 3, 'path': 'private'}}}
    path = tmp_path / 'sync-status.json'
    path.write_text(json.dumps(receipt))
    result = capture_status(tmp_path)
    assert result['parser'] == {'full': 1, 'incremental': 2, 'reused_events': 30,
                                'checkpoints_saved': 3}
    assert 'private' not in json.dumps(result)
    for invalid in (-1, True, 'private'):
        receipt['capture']['parser']['reused_events'] = invalid
        path.write_text(json.dumps(receipt))
        assert capture_status(tmp_path) == {'status': 'unavailable'}
