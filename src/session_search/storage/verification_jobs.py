"""Bounded, device-owned recovery jobs with durable results and crash detection."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from pathlib import Path

from session_search.core.records import canonical_json, digest
from session_search.storage.objects import sync_directory

TTL = 900
MAX_JOBS = 32


def cleanup_scratch(record: dict):
    """Called only while holding the lock also inherited by active restores."""
    for index, value in enumerate(record.get('scratch', [])):
        path = Path(value)
        if not path.is_absolute() or path.name != f'.offload-{record["job_id"]}-{index}' or path.is_symlink():
            raise ValueError('invalid verification scratch identity')
        if not path.exists():
            continue
        marker = path / 'owner.json'
        if not marker.exists():
            # Only marker staging can exist before ownership is published.
            for child in path.iterdir():
                if not child.name.startswith('.verification-') or child.is_symlink() or not child.is_file():
                    raise ValueError('unowned verification scratch is not empty')
                child.unlink()
            path.rmdir()
            continue
        if read_record(marker) != {'version': 1, 'job_id': record['job_id']}:
            raise ValueError('verification scratch ownership changed')
        # Keep the marker until every restore member is removed. Interrupted
        # cleanup can then verify ownership again and finish on the next run.
        for child in path.iterdir():
            if child == marker:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        marker.unlink()
        path.rmdir()
        sync_directory(path.parent)
    record.pop('scratch', None)


def write_record(path: Path, value: dict):
    fd, temporary = tempfile.mkstemp(prefix='.verification-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(canonical_json(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_record(path: Path):
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError('invalid verification record')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('invalid verification record')
    return value


class VerificationJobs:
    def __init__(self, root: Path, repositories: Path, receipt: Path):
        self.root = root.absolute()
        self.repositories = repositories.absolute()
        self.receipt = receipt.absolute()

    def submit(self, producer: str, nonce: str, requirements: list[dict]) -> dict:
        if not isinstance(nonce, str) or not re.fullmatch('[0-9a-f]{64}', nonce):
            raise ValueError('a fresh 256-bit request nonce is required')
        if not isinstance(requirements, list) or not 1 <= len(requirements) <= 100:
            raise ValueError('verification accepts 1–100 raw requirements')
        for row in requirements:
            if not isinstance(row, dict) or set(row) != {'session_id', 'revision', 'digest', 'size'}:
                raise ValueError('invalid raw requirement fields')
            if not isinstance(row['session_id'], str) or not 0 < len(row['session_id']) <= 512:
                raise ValueError('invalid session identity')
            if any(not isinstance(row[key], str) or not re.fullmatch('[0-9a-f]{64}', row[key])
                   for key in ('revision', 'digest')):
                raise ValueError('invalid raw identity')
            if type(row['size']) is not int or not 0 < row['size'] <= 32 * 1024 ** 3:
                raise ValueError('invalid raw size')
        if len(canonical_json(requirements).encode()) > 49152:
            raise ValueError('raw requirements exceed request budget')
        owner = hashlib.sha256(producer.encode()).hexdigest()
        job_id = digest(canonical_json([owner, nonce, requirements]).encode())
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError('verification directory must not be a symlink')
        path = self.root / (job_id + '.json')
        if path.exists():
            return self.poll(producer, job_id)
        lock = (self.root / '.lock').open('a')
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'version': 1, 'status': 'busy'}
            now = time.time()
            for staged in self.root.glob('.verification-*'):
                if staged.is_file() and not staged.is_symlink():
                    staged.unlink()
            paths = list(self.root.glob('*.json'))
            if len(paths) > MAX_JOBS:
                return {'version': 1, 'status': 'unavailable'}
            for old_path in paths:
                old = read_record(old_path)
                try:
                    cleanup_scratch(old)
                except (OSError, ValueError):
                    return {'version': 1, 'status': 'unavailable'}
                if old['status'] in {'queued', 'running'}:
                    old.update(status='interrupted', completed_at=now, expires_at=now + TTL)
                    write_record(old_path, old)
                elif old['expires_at'] < now:
                    old_path.unlink()
            if len(list(self.root.glob('*.json'))) >= MAX_JOBS:
                return {'version': 1, 'status': 'busy'}
            record = {'version': 1, 'job_id': job_id, 'owner': owner, 'nonce': nonce,
                      'requirements': requirements, 'status': 'queued', 'created_at': now,
                      'repositories_path': str(self.repositories), 'receipt_path': str(self.receipt)}
            write_record(path, record)
            try:
                # The child inherits the same locked file description. Closing
                # the parent's copy does not release the child's admission lock.
                process = subprocess.Popen([sys.executable, '-m', __name__, str(path), str(lock.fileno())],
                                 pass_fds=(lock.fileno(),), start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                threading.Thread(target=process.wait, daemon=True).start()
            except OSError:
                record.update(status='failed', completed_at=now, expires_at=now + TTL)
                write_record(path, record)
                raise
        finally:
            lock.close()
        return {'version': 1, 'status': 'queued', 'job_id': job_id, 'nonce': nonce}

    def poll(self, producer: str, job_id: str) -> dict:
        if not isinstance(job_id, str) or not re.fullmatch('[0-9a-f]{64}', job_id):
            raise ValueError('invalid verification job identity')
        try:
            record = read_record(self.root / (job_id + '.json'))
        except FileNotFoundError:
            return {'version': 1, 'status': 'unavailable'}
        if record['owner'] != hashlib.sha256(producer.encode()).hexdigest():
            return {'version': 1, 'status': 'unavailable'}
        status = record['status']
        if status in {'queued', 'running'}:
            with (self.root / '.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    status = 'interrupted'
                except BlockingIOError:
                    pass
        elif time.time() > record['expires_at']:
            status = 'expired'
        result = {'version': 1, 'status': status, 'job_id': job_id, 'nonce': record['nonce']}
        if status == 'restore_verified':
            result.update(proof=record['proof'], expires_at=record['expires_at'])
        return result


def run_job(path: Path, lock_fd: int):
    # Refuse standalone invocation without the inherited admission lock.
    held = os.fstat(lock_fd)
    expected = (path.parent / '.lock').stat()
    if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError('incorrect verification lock')
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    record = read_record(path)
    record['status'] = 'running'
    write_record(path, record)
    try:
        from session_search.storage.backups import load_repositories
        from session_search.storage.offload import verify_offload_backups
        from dataclasses import replace
        repositories = []
        for index, repo in enumerate(load_repositories(Path(record['repositories_path']))):
            parent = (repo.restore_directory or Path(tempfile.gettempdir())).absolute()
            scratch = parent / f'.offload-{record["job_id"]}-{index}'
            if scratch.exists() or scratch.is_symlink():
                raise FileExistsError('verification scratch already exists')
            # Publish intent first, so even a crash during directory creation
            # leaves a discoverable, narrowly identified cleanup target.
            record.setdefault('scratch', []).append(str(scratch))
            write_record(path, record)
            scratch.mkdir(mode=0o700)
            write_record(scratch / 'owner.json', {'version': 1, 'job_id': record['job_id']})
            repositories.append(replace(repo, inherited_lock_fd=lock_fd, restore_directory=scratch))
        receipt = read_record(Path(record['receipt_path']))
        proof = verify_offload_backups(receipt, record['requirements'], repositories)
        record.update(status='restore_verified', proof=proof)
    except Exception:
        # Exception text can contain repository credentials or private paths.
        record['status'] = 'failed'
    finally:
        try:
            cleanup_scratch(record)
        except (OSError, ValueError):
            record['status'] = 'failed'
        now = time.time()
        record.update(completed_at=now, expires_at=now + TTL)
        write_record(path, record)
        os.close(lock_fd)


if __name__ == '__main__':
    run_job(Path(sys.argv[1]), int(sys.argv[2]))
