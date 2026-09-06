"""Device credentials are files, never command-line arguments or log output."""

import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from contextlib import contextmanager

from session_search.core.records import canonical_json


def update_device(registry: Path, producer: str, token_file: Path | None = None) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", producer):
        raise ValueError("invalid device name")
    registry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (registry.parent / (registry.name + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        document = read_registry(registry) if registry.exists() else {}
        if document.get("replica") is True:
            raise ValueError("replica credentials must be updated through their authority")
        entries = device_entries(document)
        if token_file:
            if producer in entries:
                raise ValueError("device exists; revoke before replacing credentials")
            token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            token = secrets.token_urlsafe(32)
            with os.fdopen(os.open(token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as f:
                f.write(token + "\n")
                f.flush()
                os.fsync(f.fileno())
            entries[producer] = hashlib.sha256(token.encode()).hexdigest()
        else:
            entries.pop(producer, None)
        if document.get("version") == 2:
            document["epoch"] += 1
            document["devices"] = entries
        else:
            document = entries
        write_registry(registry, document)


def device_entries(document: dict) -> dict:
    return document['devices'] if document.get('version') == 2 else document


def validate_registry(document: dict) -> dict:
    if not isinstance(document, dict):
        raise ValueError('invalid credential registry')
    if document.get('version') == 2:
        if (set(document) != {'version', 'authority', 'epoch', 'devices', 'replica'}
                or not isinstance(document['authority'], str)
                or not re.fullmatch('[0-9a-f]{32}', document['authority'])
                or type(document['epoch']) is not int or document['epoch'] < 0
                or type(document['replica']) is not bool):
            raise ValueError('invalid credential authority or revision')
    entries = device_entries(document)
    if not isinstance(entries, dict) or any(
        not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name)
        or not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value)
        for name, value in entries.items()
    ):
        raise ValueError('invalid credential entries')
    return document


def read_registry(registry: Path) -> dict:
    return validate_registry(json.loads(registry.read_text()))


def write_registry(registry: Path, document: dict):
    fd, temporary = tempfile.mkstemp(dir=registry.parent, prefix='.credentials-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(canonical_json(document))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, registry)
        from session_search.storage.objects import sync_directory
        sync_directory(registry.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def registry_lock(registry: Path):
    registry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (registry.parent / (registry.name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def credential_identity(document: dict) -> dict:
    value = {key: document[key] for key in ('version', 'authority', 'epoch', 'devices')}
    return {'authority': document['authority'], 'epoch': document['epoch'],
            'digest': hashlib.sha256(canonical_json(value).encode()).hexdigest()}


def export_credentials(registry: Path) -> dict:
    with registry_lock(registry):
        document = read_registry(registry)
        if document.get('replica') is True:
            raise ValueError('replica cannot publish credentials')
        if document.get('version') != 2:
            document = {'version': 2, 'authority': secrets.token_hex(16), 'epoch': 0,
                        'devices': document, 'replica': False}
            write_registry(registry, document)
        return document


def receive_credentials(registry: Path, document: dict) -> dict:
    validate_registry(document)
    if document.get('version') != 2 or document['replica']:
        raise ValueError('an authoritative versioned registry is required')
    with registry_lock(registry):
        current = read_registry(registry) if registry.exists() else None
        if current is not None:
            if current.get('version') != 2:
                # Bootstrap only an identical legacy copy. A divergent local
                # revocation must not be overwritten by an assumed authority.
                if current != document['devices']:
                    raise ValueError('legacy registry differs; reconcile before binding authority')
            elif (not current['replica'] or current['authority'] != document['authority']
                  or current['epoch'] > document['epoch']
                  or (current['epoch'] == document['epoch']
                      and credential_identity(current) != credential_identity(document))):
                raise ValueError('credential authority or revision conflicts with receiver')
        write_registry(registry, {**document, 'replica': True})
    return {'version': 1, 'status': 'synchronized', **credential_identity(document)}


def sync_credentials(registry: Path, host: str, remote_registry: str,
                     remote_executable: str, receipt: Path, *, runner=None) -> dict:
    import shlex
    import subprocess
    from datetime import datetime, timezone

    if receipt.resolve() == registry.resolve():
        raise ValueError('credential receipt must be separate from the registry')

    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]*', host):
        raise ValueError('credential destination must be an SSH host alias')
    for path in (remote_registry, remote_executable):
        if not path.startswith('/') or any(c in path for c in ('\n', '\r', '\x00')):
            raise ValueError('remote credential paths must be absolute single-line paths')
    receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with registry_lock(receipt):
        document = export_credentials(registry)
        encoded = canonical_json(document)
        if len(encoded.encode()) > 65536:
            raise ValueError('credential registry exceeds transfer limit')
        desired = credential_identity(document)
        previous = json.loads(receipt.read_text()) if receipt.exists() else {}
        destination = {'host': host, 'registry': remote_registry}
        if previous and previous.get('destination') != destination:
            raise ValueError('credential receipt belongs to another destination')
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', host,
                   shlex.join([remote_executable, 'receive-credentials', '--registry', remote_registry])]
        result = {'version': 1, 'destination': destination, 'desired': desired,
                  'acknowledged': previous.get('acknowledged'), 'status': 'partial'}
        try:
            completed = (runner or subprocess.run)(
                command, input=encoded, capture_output=True, text=True, timeout=45,
            )
            if completed.returncode:
                raise ValueError('peer rejected credentials')
            acknowledged = json.loads(completed.stdout)
            if (not isinstance(acknowledged, dict) or acknowledged.get('version') != 1
                    or acknowledged.get('status') != 'synchronized'
                    or any(acknowledged.get(key) != value for key, value in desired.items())):
                raise ValueError('peer did not acknowledge exact credential revision')
            result['acknowledged'] = desired
            result['verified_at'] = datetime.now(timezone.utc).isoformat()
            result['status'] = ('synchronized' if credential_identity(read_registry(registry)) == desired
                                else 'behind')
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            result['reason'] = 'peer_unavailable_or_rejected_revision'
        write_registry(receipt, result)
        return result
