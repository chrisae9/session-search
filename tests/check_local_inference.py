"""Run explicitly with a provisioned native runtime/model and OS networking denied."""

import argparse
import errno
import json
from pathlib import Path
import resource
import socket
import tempfile
import time

from session_search.core.embeddings import EmbeddingIdentity, LocalEmbedder
from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.storage.catalog import Catalog
from session_search.storage.semantic import hybrid_search, index_pending


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('model', type=Path)
    parser.add_argument('sha256')
    args = parser.parse_args()
    # A refused connection is insufficient: require the OS to deny networking.
    with socket.socket() as probe:
        try:
            probe.connect(('127.0.0.1', 9))
        except OSError as exc:
            assert exc.errno in {errno.EPERM, errno.EACCES}, 'OS networking must be denied'
        else:
            raise AssertionError('networking was not denied')
    provider = LocalEmbedder(args.model, EmbeddingIdentity('sha256:' + args.sha256))
    start = time.monotonic()
    vector = provider.embed('recover a lost computer', query=True)
    assert len(vector) == 1024
    assert len(provider.embed('transport repair ' * 350)) == 1024
    try:
        provider.embed('transport repair ' * 10000)
    except ValueError:
        pass
    else:
        raise AssertionError('oversized input was silently accepted')
    with tempfile.TemporaryDirectory(prefix='session-search-native-isolation-') as temporary:
        with Catalog(Path(temporary)) as catalog:
            for sid, text in [('recovery', 'Restore the backup snapshot to replace a lost workstation.'),
                              ('food', 'The cake recipe uses flour, sugar, and butter.')]:
                catalog.ingest(SessionRevision(sid, (Event('one', 'user', text),)),
                               producer='synthetic', request_id=sid)
            indexed = index_pending(catalog, provider, limit=100)
            assert indexed['status'] == 'ok' and indexed['failed'] == 0
            found = hybrid_search(catalog, SearchQuery('recover a lost computer', limit=2), provider)
            assert found['semantic_available'] is True
            assert found['results'][0]['citation']['session_id'] == 'recovery'
            cite = found['results'][0]['citation']
            assert catalog.context([cite])['results'][0]['events']
    print(json.dumps({'status': 'verified', 'model_sha256': args.sha256,
                      'dimensions': len(vector), 'network_denied_by_os': True,
                      'seconds': round(time.monotonic() - start, 3),
                      'peak_rss_platform_units': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))


if __name__ == '__main__':
    main()
