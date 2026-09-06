"""Reviewed thin-client offload with fresh primary verification and local rechecks."""

import fcntl
import hashlib
import re
import secrets
import stat
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from session_search.capture.local import fingerprint
from session_search.core.records import canonical_json, digest
from session_search.interfaces.client import RemoteError
from session_search.storage.objects import sync_directory
from session_search.storage.offload import writers_running


@contextmanager
def idle_queue(queue):
    with ExitStack() as stack:
        for name in ('.flush.lock', '.capture.lock'):
            lock = stack.enter_context((queue.root / name).open('a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def acknowledged(queue, candidate):
    row = queue.db.execute(
        'SELECT a.* FROM raw_acknowledgements a '
        'JOIN captured c ON c.path=a.path AND c.fingerprint=a.fingerprint '
        'JOIN raw_captures r ON r.path=a.path AND r.fingerprint=a.fingerprint AND r.digest=a.digest '
        'JOIN acknowledged h ON h.session_id=a.session_id AND h.revision=a.revision '
        'WHERE a.path=? AND NOT EXISTS(SELECT 1 FROM pending p WHERE p.session_id=a.session_id)',
        (candidate['path'],),
    ).fetchone()
    return row is not None and dict(row) == candidate


def native_matches(candidate, codex_home: Path, now: float):
    path = Path(candidate['path'])
    try:
        if (not path.is_absolute() or path.resolve() != path or not path.is_relative_to(codex_home)
                or path.suffix != '.jsonl' or not stat.S_ISREG(path.lstat().st_mode)
                or fingerprint(path) != candidate['fingerprint']):
            return False
        metadata = path.stat()
        if metadata.st_size != candidate['size'] or metadata.st_mtime > now - 30 * 86400:
            return False
        with path.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != candidate['digest']:
                return False
            if not metadata.st_size:
                return False
            source.seek(-1, 2)
            if source.read(1) != b'\n':
                return False
        return fingerprint(path) == candidate['fingerprint']
    except OSError:
        return False


def plan_client_offload(queue, client, codex_home: Path, *, limit: int = 100):
    if not 1 <= limit <= 100:
        raise ValueError('offload plans accept 1–100 files')
    root = codex_home.resolve()
    now = time.time()
    candidates = []
    skipped = 0
    with idle_queue(queue):
        missing = queue.db.execute('SELECT COUNT(*) FROM raw_captures r WHERE NOT EXISTS '
                                  '(SELECT 1 FROM raw_acknowledgements a WHERE a.path=r.path)').fetchone()[0]
        for row in queue.db.execute('SELECT * FROM raw_acknowledgements ORDER BY path'):
            candidate = dict(row)
            if not acknowledged(queue, candidate) or not native_matches(candidate, root, now):
                skipped += 1
                continue
            candidates.append(candidate)
            if len(candidates) == limit:
                break
    result = {'version': 1, 'kind': 'client-offload', 'created_at': now, 'retention_days': 30,
              'primary': client.primary, 'queue_root': str(queue.root.resolve()), 'codex_home': str(root),
              'candidates': candidates, 'skipped': skipped, 'missing_acknowledgements': missing,
              'limit_reached': len(candidates) == limit}
    return {**result, 'plan_id': digest(canonical_json(result).encode())}


def fresh_proof(client, requirements, *, timeout: float):
    if not 0 < timeout <= 7200:
        raise ValueError('verification timeout must be within two hours')
    nonce = secrets.token_hex(32)
    start = time.monotonic()
    job_id = None
    failures = 0
    while time.monotonic() - start < timeout:
        if writers_running():
            raise RuntimeError('Codex restarted; offload stopped')
        submitting = job_id is None
        try:
            response = (client.request_offload_verification(nonce, requirements) if submitting
                        else client.poll_offload_verification(job_id))
            failures = 0
        except RemoteError as exc:
            failures += 1
            if exc.status not in {None, 429, 500, 502, 503, 504} or failures >= 3:
                raise
            time.sleep(min(2 ** (failures - 1), max(0, timeout - (time.monotonic() - start))))
            continue
        if time.monotonic() - start >= timeout:
            break
        if submitting:
            if response.get('status') == 'busy':
                time.sleep(min(1, max(0, timeout - (time.monotonic() - start))))
                continue
            job_id = response.get('job_id')
            if not isinstance(job_id, str) or not re.fullmatch('[0-9a-f]{64}', job_id):
                raise RuntimeError('fresh verification could not be started')
        if response.get('job_id') != job_id or response.get('nonce') != nonce:
            raise ValueError('verification response does not match this request')
        if response.get('status') == 'restore_verified':
            proof = response.get('proof', {})
            if not isinstance(proof, dict):
                raise ValueError('invalid verification proof')
            repos = proof.get('repositories', [])
            expires = response.get('expires_at')
            now = time.time()
            verified = proof.get('verified_at')
            if (proof.get('version') != 1 or proof.get('status') != 'restore_verified'
                    or proof.get('requirements_digest') != digest(canonical_json(requirements).encode())
                    or type(proof.get('candidates_verified')) is not int
                    or proof['candidates_verified'] != len(requirements)
                    or type(expires) not in (int, float) or not now < expires <= now + 930
                    or type(verified) not in (int, float) or not now - 900 <= verified <= now + 30
                    or not isinstance(proof.get('snapshot'), str)
                    or not re.fullmatch('[0-9a-f]{64}', proof['snapshot'])
                    or not isinstance(repos, list) or len(repos) < 2):
                raise ValueError('verification proof is incomplete or expired')
            for repo in repos:
                if not isinstance(repo, dict) or not isinstance(repo.get('repository'), str):
                    raise ValueError('invalid verified repository')
                for key in ('repository_id', 'backup_id'):
                    if not isinstance(repo.get(key), str) or not re.fullmatch('[0-9a-f]{64}', repo[key]):
                        raise ValueError('invalid verified repository identity')
            if (len({r['repository_id'] for r in repos}) != len(repos)
                    or len({r['repository'] for r in repos}) != len(repos)):
                raise ValueError('verified repositories are not distinct')
            return {**response, 'expires_at': min(expires, now + 900)}
        if response.get('status') not in {'queued', 'running'}:
            raise RuntimeError('fresh backup verification did not succeed')
        if submitting:
            continue
        time.sleep(min(1, max(0, timeout - (time.monotonic() - start))))
    raise TimeoutError('backup verification did not finish within the requested time')


def apply_client_offload(queue, client, plan: dict, *, expected_plan_id: str, timeout: float = 7200):
    original = dict(plan)
    plan_id = original.pop('plan_id', None)
    if plan_id != expected_plan_id or digest(canonical_json(original).encode()) != plan_id:
        raise ValueError('offload plan changed after review')
    if (plan.get('version') != 1 or plan.get('kind') != 'client-offload'
            or plan.get('retention_days') != 30 or plan.get('primary') != client.primary
            or plan.get('queue_root') != str(queue.root.resolve())):
        raise ValueError('offload plan belongs to another client or policy')
    candidates = plan.get('candidates')
    if not isinstance(candidates, list) or len(candidates) > 100:
        raise ValueError('invalid offload candidate list')
    root = Path(plan['codex_home'])
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError('offload source root changed')
    if writers_running():
        raise RuntimeError('stop Codex writers before applying an offload plan')
    with idle_queue(queue):
        eligible = [c for c in candidates if acknowledged(queue, c) and native_matches(c, root, time.time())]
        skipped = len(candidates) - len(eligible)
        if not eligible:
            return {'version': 1, 'status': 'partial' if skipped else 'ok', 'removed': 0, 'skipped': skipped}
        requirements = [{k: c[k] for k in ('session_id', 'revision', 'digest', 'size')} for c in eligible]
        response = fresh_proof(client, requirements, timeout=timeout)
        deadline = time.monotonic() + min(900, max(0, response['expires_at'] - time.time()))
        removed = 0
        for candidate in eligible:
            if writers_running() or time.time() >= response['expires_at'] or time.monotonic() >= deadline:
                raise RuntimeError('writers restarted or verification expired; remaining offloads stopped')
            if not acknowledged(queue, candidate) or not native_matches(candidate, root, time.time()):
                skipped += 1
                continue
            # Hashing a large file takes time. Check the writer and expiry gates
            # again after that read, immediately before removing this exact path.
            if writers_running() or time.time() >= response['expires_at'] or time.monotonic() >= deadline:
                raise RuntimeError('writers restarted or verification expired; remaining offloads stopped')
            path = Path(candidate['path'])
            if fingerprint(path) != candidate['fingerprint']:
                skipped += 1
                continue
            path.unlink()
            sync_directory(path.parent)
            removed += 1
    return {'version': 1, 'status': 'partial' if skipped else 'ok', 'removed': removed, 'skipped': skipped}


def recover_raw_acknowledgements(queue, client, *, limit: int = 1000, cursor: str = ''):
    """Recover only exact metadata; never rebase heads or infer backup durability."""
    if not 1 <= limit <= 10000:
        raise ValueError('acknowledgement recovery accepts 1–10000 sources')
    after = bytes.fromhex(cursor).decode() if cursor else ''
    if len(after.encode()) > 4096:
        raise ValueError('invalid recovery cursor')
    recovered = 0
    with idle_queue(queue):
        rows = [dict(row) for row in queue.db.execute(
            'SELECT r.* FROM raw_captures r JOIN captured c '
            'ON c.path=r.path AND c.fingerprint=r.fingerprint WHERE r.path>? '
            'AND NOT EXISTS(SELECT 1 FROM raw_acknowledgements a WHERE a.path=r.path) '
            'ORDER BY r.path LIMIT ?', (after, limit),
        )]
        for start in range(0, len(rows), 50):
            batch = rows[start:start + 50]
            matches = client.raw_acknowledgements(batch)
            with queue.db:
                for match in matches:
                    source = batch[match['index']]
                    # Server head lookup cannot silently replace a client's
                    # acknowledged head or bridge an outstanding local revision.
                    known = queue.db.execute(
                        'SELECT 1 FROM acknowledged h JOIN raw_captures r ON r.path=? '
                        'JOIN captured c ON c.path=r.path AND c.fingerprint=r.fingerprint '
                        'WHERE h.session_id=? AND h.revision=? AND r.fingerprint=? AND r.digest=? '
                        'AND NOT EXISTS(SELECT 1 FROM pending p WHERE p.session_id=h.session_id)',
                        (source['path'], match['session_id'], match['revision'], source['fingerprint'], source['digest']),
                    ).fetchone()
                    if known:
                        changed = queue.db.execute('INSERT OR IGNORE INTO raw_acknowledgements VALUES (?,?,?,?,?,?)',
                            (source['path'], source['fingerprint'], source['digest'], match['size'],
                             match['session_id'], match['revision'])).rowcount
                        recovered += changed
    return {'version': 1, 'status': 'ok', 'scanned': len(rows), 'recovered': recovered,
            'unresolved': len(rows) - recovered,
            'next_cursor': rows[-1]['path'].encode().hex() if len(rows) == limit else None}
