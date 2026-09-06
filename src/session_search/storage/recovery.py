"""Prepare a separate primary from verified recovery evidence."""

import fcntl
import json
import os
import secrets
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from session_search.core.records import canonical_json, digest
from session_search.storage.catalog import Catalog
from session_search.storage.objects import ObjectStore, sync_directory
from session_search.storage.snapshots import verify_snapshot


def prepare_primary(snapshot: Path, destination: Path, *, expected_snapshot: str,
                    old_primary_isolated: bool = False) -> dict:
    if not old_primary_isolated:
        raise ValueError('isolate the old primary before preparing a replacement')
    if destination.is_symlink():
        raise FileExistsError('replacement primary destination must not be a symlink')
    snapshot = snapshot.resolve()
    destination = destination.resolve()
    if destination.is_relative_to(snapshot):
        raise ValueError('replacement primary must be outside the recovery snapshot')
    verified = verify_snapshot(snapshot)
    if verified['purpose'] != 'recovery' or verified['snapshot'] != expected_snapshot:
        raise ValueError('an exact verified recovery snapshot is required')
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.parent / ('.prepare-' + digest(destination.name.encode()) + '.lock')
    with lock_path.open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        if destination.exists():
            raise FileExistsError('replacement primary destination already exists')
        stage = Path(tempfile.mkdtemp(prefix='.primary-', dir=destination.parent))
        try:
            # Copy only verified recovery inputs; never inherit runtime pointers,
            # fences, transfer state, credential files, or replication receipts.
            manifest = json.loads((snapshot / 'manifest.json').read_text())
            shutil.copyfile(snapshot / 'catalog.sqlite3', stage / 'catalog.sqlite3')
            shutil.copyfile(snapshot / 'manifest.json', stage / 'manifest.json')
            source_objects, target_objects = ObjectStore(snapshot), ObjectStore(stage)
            for item in manifest['raw_objects']:
                source_objects.copy_to(target_objects, item['digest'], allow_links=False)
            copied = verify_snapshot(stage)
            if copied['snapshot'] != expected_snapshot:
                raise ValueError('recovery snapshot changed while preparing primary')
            os.rename(stage / 'manifest.json', stage / 'recovery-source.json')
            with Catalog(stage) as catalog:
                catalog.db.execute('UPDATE publication_state SET identity=?,epoch=0 WHERE id=1',
                                   (secrets.token_hex(16),))
                catalog.db.commit()
                publication = catalog.publication()
                catalog.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            receipt = {'version': 1, 'status': 'prepared', 'snapshot': expected_snapshot,
                       'source_publication': verified['publication'], 'publication': publication,
                       'old_primary_isolated': True,
                       'prepared_at': datetime.now(timezone.utc).isoformat()}
            (stage / 'recovery.json').write_text(canonical_json(receipt))
            for path in stage.rglob('*'):
                if path.is_file():
                    path.chmod(0o600)
                    with path.open('rb') as stream:
                        os.fsync(stream.fileno())
            for path in sorted(stage.rglob('*'), key=lambda p: len(p.parts), reverse=True):
                if path.is_dir():
                    sync_directory(path)
            sync_directory(stage)
            if destination.exists():
                raise FileExistsError('replacement primary destination already exists')
            os.rename(stage, destination)
            sync_directory(destination.parent)
            return receipt
        finally:
            if stage.exists():
                shutil.rmtree(stage)
