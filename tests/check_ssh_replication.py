"""Qualify real SSH/rsync against an explicitly provisioned disposable receiver."""
import argparse
import json
from pathlib import Path
import sys

from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.storage.catalog import Catalog
from session_search.storage.replication import replicate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('host')
    parser.add_argument('scratch', type=Path)
    args = parser.parse_args()
    args.scratch.mkdir(parents=True, exist_ok=False)
    primary, replica, outbox = (args.scratch / name for name in ('primary', 'replica with spaces', 'outbox'))
    with Catalog(primary) as catalog:
        for device in ('device-a', 'device-b'):
            catalog.ingest(SessionRevision(device, (Event('e', 'user', 'retained evidence'),)),
                           producer=device, request_id=device)
        citation = catalog.search(SearchQuery('retained'))['results'][0]['citation']

    def publish():
        return replicate(primary, outbox, args.host, str(replica),
                         str(Path(sys.executable).parent / 'session-search'), reserve_bytes=67108864)

    assert publish()['status'] == 'replicated'
    assert publish()['status'] == 'up_to_date'
    with Catalog(replica, readonly=True) as catalog:
        assert len(catalog.search(SearchQuery('retained'))['results']) == 2
        assert catalog.context([citation])['results'][0]['status'] == 'ok'
    with Catalog(primary) as catalog:
        catalog.ingest(SessionRevision('device-a', (Event('e', 'user', 'updated evidence'),)),
                       producer='device-a', request_id='update')
    assert publish()['status'] == 'replicated'
    with Catalog(replica, readonly=True) as catalog:
        assert catalog.search(SearchQuery('updated'))['results']
        assert catalog.context([citation])['results'][0]['status'] == 'ok'
    print(json.dumps({'status': 'verified', 'transport': 'real SSH and rsync',
                      'checks': ['two producers', 'unchanged publication', 'update', 'old citation']}))


if __name__ == '__main__':
    main()
