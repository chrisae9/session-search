import json
import shlex
import shutil
from pathlib import Path

import pytest

from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.storage.catalog import Catalog
from session_search.storage.replication import receive_replica, replicate
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
        target = shlex.split(command)[-1]
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
        catalog.ingest(SessionRevision('s', (Event('e', 'user', 'new evidence'),)),
                       producer='d', request_id='second')
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
