import json
import shlex
import shutil
from pathlib import Path

import pytest

from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.storage.catalog import Catalog
from session_search.storage.replication import capacity, receive_replica, replicate
from session_search.storage.snapshots import activate_replica, create_snapshot


def test_replication_resumes_failed_transfer_and_skips_unchanged_publication(tmp_path):
    primary, replica, outbox = (tmp_path / name for name in ('primary', 'replica with spaces', 'outbox'))
    with Catalog(primary) as catalog:
        catalog.ingest(SessionRevision('s', (Event('e', 'user', 'old evidence'),)),
                       producer='d', request_id='first')
    calls = []
    fail_transfer = True
    lose_ack = True

    def runner(arguments):
        nonlocal fail_transfer, lose_ack
        calls.append(arguments)
        if arguments[0] == 'rsync':
            if fail_transfer:
                fail_transfer = False
                raise RuntimeError('synthetic interrupted transfer')
            target = shlex.split(arguments[-1].split(':', 1)[1])[0]
            shutil.copytree(arguments[-2], target, dirs_exist_ok=True)
            return ''
        command = arguments[-1]
        if command.startswith('umask'):
            return ''
        parts = shlex.split(command)
        if 'replica-capacity' in parts:
            index = parts.index('replica-capacity')
            return json.dumps(capacity(replica, int(parts[index + 1]), int(parts[-1])))
        target = parts[-1]
        result = receive_replica(Path(target), replica)
        if lose_ack:
            lose_ack = False
            raise RuntimeError('synthetic lost acknowledgement')
        return json.dumps(result)

    def publish():
        return replicate(primary, outbox, 'standby', str(replica), '/example path/session-search', runner=runner)

    with pytest.raises(RuntimeError):
        publish()
    pending = (outbox / 'pending/manifest.json').read_bytes()
    with pytest.raises(RuntimeError):
        publish()
    assert (outbox / 'pending/manifest.json').read_bytes() == pending
    assert publish()['status'] == 'replicated'
    assert not (outbox / 'pending').exists()
    assert not list((replica / 'incoming').iterdir())
    before = len(calls)
    assert publish()['status'] == 'up_to_date'
    assert len(calls) == before
    with Catalog(primary) as catalog:
        assert catalog.status()['replication']['status'] == 'acknowledged'
        catalog.ingest(SessionRevision('s', (Event('e', 'user', 'new evidence'),)),
                       producer='d', request_id='second')
        assert catalog.status()['replication']['status'] == 'behind'
    assert publish()['status'] == 'replicated'
    with Catalog(replica, readonly=True) as catalog:
        assert catalog.search(SearchQuery('new'))['results']


def test_replica_rejects_older_publication_and_different_primary(tmp_path):
    primary, replica = tmp_path / 'primary', tmp_path / 'replica'
    with Catalog(primary) as catalog:
        create_snapshot(catalog, tmp_path / 'old', search_only=True)
        catalog.ingest(SessionRevision('s', (Event('e', 'user', 'new evidence'),)),
                       producer='d', request_id='new')
        create_snapshot(catalog, tmp_path / 'new', search_only=True)
    activate_replica(tmp_path / 'new', replica)
    current = (replica / 'CURRENT').read_text()
    with pytest.raises(ValueError, match='move backward'):
        activate_replica(tmp_path / 'old', replica)
    with Catalog(tmp_path / 'other') as catalog:
        create_snapshot(catalog, tmp_path / 'other-snapshot', search_only=True)
    with pytest.raises(ValueError, match='primary identity'):
        activate_replica(tmp_path / 'other-snapshot', replica)
    assert (replica / 'CURRENT').read_text() == current


def test_unreadable_replication_receipt_does_not_disable_search(tmp_path):
    with Catalog(tmp_path / 'primary') as catalog:
        catalog.ingest(SessionRevision('s', (Event('e', 'user', 'retained evidence'),)),
                       producer='d', request_id='new')
        receipts = catalog.root / 'replication-receipts'
        receipts.mkdir()
        (receipts / 'damaged.json').write_text('{')
        result = catalog.search(SearchQuery('retained'))
        assert result['results']
        assert result['coverage']['replication']['status'] == 'partial'


def test_received_replica_publishes_without_a_second_catalog_copy(tmp_path, monkeypatch):
    primary, replica = tmp_path / 'primary', tmp_path / 'replica'
    original = tmp_path / 'snapshot'
    with Catalog(primary) as catalog:
        catalog.ingest(SessionRevision('s', (Event('e', 'user', 'retained evidence'),)),
                       producer='d', request_id='one')
        result = create_snapshot(catalog, original, search_only=True)
    incoming = replica / 'incoming' / result['snapshot']
    incoming.parent.mkdir(parents=True)
    original.rename(incoming)
    inode = (incoming / 'catalog.sqlite3').stat().st_ino

    def unexpected_copy(*args, **kwargs):
        raise AssertionError('receive must not duplicate the transferred catalog')

    monkeypatch.setattr(shutil, 'copytree', unexpected_copy)
    assert receive_replica(incoming, replica)['status'] == 'verified'
    assert not incoming.exists()
    published = replica / 'generations' / result['snapshot'] / 'catalog.sqlite3'
    assert published.stat().st_ino == inode
    with Catalog(replica, readonly=True) as catalog:
        assert catalog.search(SearchQuery('retained'))['results']


def test_capacity_defers_before_snapshot_allocation(tmp_path, monkeypatch):
    from session_search.storage import replication
    primary, outbox = tmp_path / 'primary', tmp_path / 'outbox'
    with Catalog(primary):
        pass
    monkeypatch.setattr(replication.shutil, 'disk_usage',
                        lambda path: type('Usage', (), {'free': 0})())

    def no_transport(arguments):
        raise AssertionError('no transport should run without local snapshot space')

    result = replicate(primary, outbox, 'standby', '/replica', '/session-search', runner=no_transport)
    assert result['status'] == 'deferred' and result['stage'] == 'source_snapshot'
    assert not (outbox / 'pending').exists()
    assert not (outbox / 'receipt.json').exists()


def test_capacity_deferral_retains_pending_snapshot_and_does_not_upload(tmp_path):
    primary, outbox = tmp_path / 'primary', tmp_path / 'outbox'
    with Catalog(primary):
        pass
    calls = []

    def low_capacity(arguments):
        calls.append(arguments)
        assert arguments[0] == 'ssh'
        parts = shlex.split(arguments[-1])
        index = parts.index('replica-capacity')
        return json.dumps({'version': 1, 'status': 'deferred', 'reason': 'capacity',
                           'incoming_bytes': int(parts[index + 1]),
                           'reserve_bytes': int(parts[-1]), 'free_bytes': 0})

    def publish():
        return replicate(primary, outbox, 'standby', '/replica', '/session-search', runner=low_capacity)

    assert publish()['stage'] == 'standby_transfer'
    manifest = (outbox / 'pending/manifest.json').read_bytes()
    assert publish()['status'] == 'deferred'
    assert (outbox / 'pending/manifest.json').read_bytes() == manifest
    assert len(calls) == 2
    assert not (outbox / 'receipt.json').exists()
    with Catalog(primary, readonly=True) as catalog:
        assert catalog.status()['replication']['status'] == 'not_configured'


def test_capacity_reserve_boundary_and_invalid_sizes(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, 'disk_usage', lambda path: type('Usage', (), {'free': 100})())
    assert capacity(tmp_path / 'not-created', 60, 40)['status'] == 'ready'
    assert capacity(tmp_path, 61, 40)['status'] == 'deferred'
    assert not (tmp_path / 'not-created').exists()
    for incoming, reserve in [(-1, 0), (1, -1), (True, 0), (1, '2')]:
        with pytest.raises(ValueError):
            capacity(tmp_path, incoming, reserve)
