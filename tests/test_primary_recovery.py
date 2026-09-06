import json

import pytest

from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.storage.catalog import Catalog
from session_search.storage.recovery import prepare_primary
from session_search.storage.snapshots import activate_replica, create_snapshot, verify_snapshot


def fixture_snapshot(tmp_path, *, search_only=False):
    root, snapshot = tmp_path / 'original', tmp_path / 'snapshot'
    with Catalog(root) as catalog:
        revision = SessionRevision('s1', (Event('e1', 'user', 'immutable evidence'),))
        catalog.ingest(revision, producer='device', request_id='one')
        result = create_snapshot(catalog, snapshot, search_only=search_only)
    return snapshot, result['snapshot'], revision


def test_replacement_preserves_citations_and_changes_publication_identity(tmp_path):
    source, snapshot_id, revision = fixture_snapshot(tmp_path)
    original = verify_snapshot(source)
    with Catalog(source, readonly=True) as catalog:
        citation = catalog.search(SearchQuery('immutable'))['results'][0]['citation']
    (source / 'FENCED.json').write_text('{}')
    (source / 'credentials.json').write_text('synthetic-unused')
    destination = tmp_path / 'replacement'
    receipt = prepare_primary(source, destination, expected_snapshot=snapshot_id,
                              old_primary_isolated=True)
    assert receipt['source_publication'] == original['publication']
    assert receipt['publication'].split(':')[0] != original['publication'].split(':')[0]
    assert receipt['publication'].endswith(':0')
    assert not (destination / 'credentials.json').exists()
    assert not (destination / 'FENCED.json').exists()
    assert json.loads((destination / 'recovery.json').read_text()) == receipt
    with Catalog(destination) as catalog:
        assert catalog.context([citation])['results'][0]['events'][0]['text'] == 'immutable evidence'
        retry = catalog.ingest(revision, producer='device', request_id='one')
        assert retry['duplicate']
        catalog.ingest(SessionRevision('s1', (Event('e2', 'user', 'continued work'),)),
                       producer='device', request_id='two')
        assert catalog.search(SearchQuery('continued'))['results']
        assert catalog.context([citation])['results']
        create_snapshot(catalog, tmp_path / 'replacement-snapshot', search_only=True)
    assert verify_snapshot(source)["snapshot"] == original["snapshot"]
    standby = tmp_path / 'standby'
    activate_replica(source, standby)
    with pytest.raises(ValueError, match='identity'):
        activate_replica(tmp_path / 'replacement-snapshot', standby)
    with pytest.raises(FileExistsError):
        prepare_primary(source, destination, expected_snapshot=snapshot_id, old_primary_isolated=True)


def test_preparation_requires_isolation_and_exact_recovery_input(tmp_path):
    source, snapshot_id, _ = fixture_snapshot(tmp_path, search_only=True)
    destination = tmp_path / 'replacement'
    with pytest.raises(ValueError, match='isolate'):
        prepare_primary(source, destination, expected_snapshot=snapshot_id)
    with pytest.raises(ValueError, match='recovery'):
        prepare_primary(source, destination, expected_snapshot=snapshot_id, old_primary_isolated=True)
    assert not destination.exists()


def test_failed_copy_never_publishes_primary(tmp_path, monkeypatch):
    from session_search.storage import recovery
    source, snapshot_id, _ = fixture_snapshot(tmp_path)
    destination = tmp_path / 'replacement'
    with pytest.raises(ValueError, match='exact'):
        prepare_primary(source, destination, expected_snapshot='0' * 64, old_primary_isolated=True)
    copy = recovery.shutil.copyfile

    def corrupt_copy(src, dst):
        result = copy(src, dst)
        if dst.name == 'catalog.sqlite3':
            with dst.open('ab') as stream:
                stream.write(b'corruption')
        return result

    monkeypatch.setattr(recovery.shutil, 'copyfile', corrupt_copy)
    with pytest.raises(ValueError, match='checksum'):
        prepare_primary(source, destination, expected_snapshot=snapshot_id, old_primary_isolated=True)
    assert not destination.exists()
    assert not list(tmp_path.glob('.primary-*'))
    assert verify_snapshot(source)['snapshot'] == snapshot_id


def test_prepare_primary_cli(tmp_path, capsys):
    from session_search.interfaces.cli import main
    source, snapshot_id, _ = fixture_snapshot(tmp_path)
    destination = tmp_path / 'replacement'
    assert main(['prepare-primary', str(source), str(destination), '--snapshot-id', snapshot_id,
                 '--old-primary-isolated']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'prepared'
    with Catalog(destination) as catalog:
        assert catalog.status()['sessions'] == 1


def test_destination_cannot_modify_snapshot_or_follow_symlink(tmp_path):
    source, snapshot_id, _ = fixture_snapshot(tmp_path)
    with pytest.raises(ValueError, match='outside'):
        prepare_primary(source, source / 'child', expected_snapshot=snapshot_id,
                        old_primary_isolated=True)
    destination = tmp_path / 'link'
    target = tmp_path / 'missing'
    destination.symlink_to(target, target_is_directory=True)
    with pytest.raises(FileExistsError, match='symlink'):
        prepare_primary(source, destination, expected_snapshot=snapshot_id, old_primary_isolated=True)
    assert not target.exists()
    assert verify_snapshot(source)['snapshot'] == snapshot_id
