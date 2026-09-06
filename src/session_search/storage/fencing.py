"""Cooperative local write fencing; host isolation remains an operator operation."""

import fcntl
import json
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from session_search.storage.objects import sync_directory


class WriteFenced(PermissionError):
    pass


def acquire_writer(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (root / '.writers.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_SH)
        if (root / 'FENCED.json').exists():
            raise WriteFenced('primary write access is fenced')
        if (root / 'CURRENT').exists() or (root / 'manifest.json').exists():
            raise PermissionError('replicas and snapshots are read-only')
        return lock
    except BaseException:
        lock.close()
        raise


@contextmanager
def writer_lease(root: Path):
    lock = acquire_writer(root)
    try:
        yield
    finally:
        lock.close()


def fence_primary(root: Path) -> dict:
    from session_search.storage.catalog import Catalog
    from session_search.interfaces.credentials import write_registry
    root = root.resolve()
    if (not (root / 'catalog.sqlite3').is_file() or (root / 'CURRENT').exists()
            or (root / 'manifest.json').exists()):
        raise ValueError('fencing requires an initialized primary catalog')
    with (root / '.writers.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'version': 1, 'status': 'busy', 'reason': 'active_catalog_or_transfer_writer'}
        with Catalog(root, readonly=True) as catalog:
            publication = catalog.publication()
        marker = root / 'FENCED.json'
        if marker.exists():
            receipt = json.loads(marker.read_text())
            if (not isinstance(receipt, dict) or receipt.get('version') != 1
                    or receipt.get('status') != 'fenced' or receipt.get('publication') != publication):
                raise ValueError('fenced catalog or receipt changed; isolate old writers')
            return receipt
        receipt = {'version': 1, 'status': 'fenced', 'publication': publication,
                   'fence_id': secrets.token_hex(16),
                   'fenced_at': datetime.now(timezone.utc).isoformat(),
                   'scope': 'cooperating local catalog and transfer writers'}
        write_registry(marker, receipt)
        sync_directory(root)
        return receipt
