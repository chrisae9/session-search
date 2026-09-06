"""Resumable, coalesced publication to one explicitly configured SSH standby."""

import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from session_search.core.records import canonical_json, digest
from session_search.storage.catalog import Catalog
from session_search.storage.objects import sync_directory
from session_search.storage.snapshots import activate_replica, create_snapshot, verify_snapshot


DEFAULT_RESERVE_BYTES = 2 * 1024 ** 3


def capacity(path: Path, incoming_bytes: int, reserve_bytes: int = DEFAULT_RESERVE_BYTES) -> dict:
    if (type(incoming_bytes) is not int or incoming_bytes < 0
            or type(reserve_bytes) is not int or reserve_bytes < 0):
        raise ValueError('capacity sizes must be nonnegative integers')
    existing = path.resolve()
    while not existing.exists():
        existing = existing.parent
    free = shutil.disk_usage(existing).free
    return {'version': 1, 'status': 'ready' if free >= incoming_bytes + reserve_bytes else 'deferred',
            'reason': 'capacity', 'free_bytes': free, 'incoming_bytes': incoming_bytes,
            'reserve_bytes': reserve_bytes}


def run_command(arguments: list[str]) -> str:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('replication transport unavailable; snapshot retained') from None
    if result.returncode:
        raise RuntimeError('replication transport failed; snapshot retained')
    return result.stdout


def save_receipt(path: Path, value: dict):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.receipt-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def receive_replica(snapshot: Path, root: Path) -> dict:
    root, snapshot = root.resolve(), snapshot.resolve()
    if snapshot.parent != root / 'incoming' or not re.fullmatch('[0-9a-f]{64}', snapshot.name):
        raise ValueError('received snapshot must be in the managed incoming directory')
    verified = verify_snapshot(snapshot)
    if verified['snapshot'] != snapshot.name or verified['purpose'] != 'search-replica':
        raise ValueError('received snapshot identity or purpose mismatch')
    result = activate_replica(snapshot, root, consume=True)
    sync_directory(snapshot.parent)
    return result


def record_acknowledgement(source: Path, receipt: dict):
    directory = source / 'replication-receipts'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = digest(canonical_json({name: receipt['destination'][name] for name in ('host', 'data')}).encode())
    save_receipt(directory / (key + '.json'), receipt)


def replicate(source: Path, outbox: Path, host: str, remote_data: str,
              remote_executable: str, *, runner=run_command,
              reserve_bytes: int = DEFAULT_RESERVE_BYTES) -> dict:
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]*', host):
        raise ValueError('replication host must be an SSH host alias')
    for value in (remote_data, remote_executable):
        if not value.startswith('/') or any(c in value for c in ('\n', '\r', '\x00')):
            raise ValueError('remote paths must be absolute single-line paths')
    outbox = outbox.resolve()
    outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = {'host': host, 'data': remote_data, 'executable': remote_executable}
    with (outbox / '.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'version': 1, 'status': 'coalesced'}
        receipt_path = outbox / 'receipt.json'
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        if receipt and any(receipt['destination'][name] != destination[name] for name in ('host', 'data')):
            raise ValueError('replication outbox belongs to another destination')
        pending = outbox / 'pending'
        if pending.exists() and receipt:
            previous = verify_snapshot(pending)
            if previous['snapshot'] == receipt['snapshot']:
                shutil.rmtree(pending)
                sync_directory(outbox)
        with Catalog(source.resolve(), readonly=True) as catalog:
            publication = catalog.publication()
            if publication is None:
                raise ValueError('initialize the primary with this version before replication')
            if not pending.exists() and receipt.get('publication') == publication:
                record_acknowledgement(source, receipt)
                return {'version': 1, 'status': 'up_to_date', **receipt}
            if not pending.exists():
                # SQLite backup may grow while readers and writers coexist. Leave
                # room for another catalog-sized allocation plus the reserve.
                needed = 2 * sum(p.stat().st_size for p in catalog.root.glob('catalog.sqlite3*')
                                 if p.is_file())
                available = capacity(outbox, needed, reserve_bytes)
                if available['status'] != 'ready':
                    return {**available, 'stage': 'source_snapshot'}
                create_snapshot(catalog, pending, search_only=True)
        verified = verify_snapshot(pending)
        incoming = remote_data.rstrip('/') + '/incoming/' + verified['snapshot']
        ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', host]
        incoming_bytes = sum(p.stat().st_size for p in pending.rglob('*') if p.is_file())
        output = runner([*ssh, shlex.join([remote_executable, '--data-dir', remote_data,
                        'replica-capacity', str(incoming_bytes), '--reserve-bytes', str(reserve_bytes)])])
        available = json.loads(output)
        if (available.get('version') != 1 or available.get('status') not in {'ready', 'deferred'}
                or available.get('incoming_bytes') != incoming_bytes
                or available.get('reserve_bytes') != reserve_bytes):
            raise ValueError('standby did not acknowledge the capacity preflight')
        if available['status'] != 'ready':
            return {**available, 'stage': 'standby_transfer'}
        runner([*ssh, 'umask 077; ' + shlex.join(['mkdir', '-p', incoming])])
        runner(['rsync', '-a', '--partial', '--checksum', '-e',
                'ssh -o BatchMode=yes -o ConnectTimeout=5',
                str(pending) + '/', host + ':' + shlex.quote(incoming + '/')])
        output = runner([*ssh, shlex.join([remote_executable, '--data-dir', remote_data,
                                         'receive-replica', incoming])])
        result = json.loads(output)
        if (result.get('version') != 1 or result.get('status') != 'verified'
                or result.get('snapshot') != verified['snapshot']
                or result.get('publication') != verified['publication']):
            raise ValueError('standby did not acknowledge the exact verified publication')
        receipt = {'destination': destination, 'snapshot': verified['snapshot'],
                   'publication': verified['publication'], 'created_at': verified['created_at'],
                   'verified_at': datetime.now(timezone.utc).isoformat()}
        save_receipt(receipt_path, receipt)
        record_acknowledgement(source, receipt)
        shutil.rmtree(pending)
        sync_directory(outbox)
        return {'version': 1, 'status': 'replicated', **receipt}
