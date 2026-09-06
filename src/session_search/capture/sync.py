"""One bounded capture/flush cycle for an external scheduler."""

import fcntl
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from session_search.capture.local import capture_home
from session_search.capture.queue import UploadQueue
from session_search.storage.catalog import Catalog
from session_search.core.records import canonical_json
from session_search.storage.objects import sync_directory


def _record(root: Path, result: dict) -> dict:
    result["completed_at"] = datetime.now(timezone.utc).isoformat()
    fd, name = tempfile.mkstemp(prefix=".sync-result-", dir=root)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(canonical_json(result) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "sync-status.json")
        sync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)
    return result


def sync_once(root: Path, home: Path, producer: str, *, client=None,
              max_bytes: int, max_pending_bytes: int | None = None,
              reserve_bytes: int = 2 * 1024 ** 3, archive_raw=False, chunk_raw=False,
              flush_limit: int = 100) -> dict:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('sync requires a positive capture byte budget')
    if type(reserve_bytes) is not int or reserve_bytes < 0:
        raise ValueError('sync reserve must be nonnegative')
    if not 1 <= flush_limit <= 1000:
        raise ValueError('sync flush limit must be between 1 and 1000')
    if chunk_raw and not archive_raw:
        raise ValueError('shared raw chunks require raw archival')
    if client and (type(max_pending_bytes) is not int or max_pending_bytes <= 0):
        raise ValueError('remote sync requires a positive backlog byte budget')
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (root / ('catalog.sqlite3' if client else 'upload-queue.sqlite3')).exists():
        raise ValueError('sync data directory belongs to the other operating mode')
    with (root / '.sync.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'version': 1, 'status': 'coalesced'}
        if not client:
            with Catalog(root) as catalog:
                captured = capture_home(catalog, home, producer, archive_raw=archive_raw,
                                        chunk_raw=chunk_raw, max_bytes=max_bytes,
                                        reserve_bytes=reserve_bytes)
            return _record(root, {'version': 1, 'status': captured['status'], 'capture': captured})
        with UploadQueue(root) as queue:
            before = queue.flush(client, limit=flush_limit)
            if before['status'] == 'coalesced':
                return {'version': 1, 'status': 'coalesced', 'flush_before': before}
            # Count logical raw sizes, conservatively ignoring chunk sharing.
            # Normalized-only uploads count their serialized UTF-8 payload bytes.
            pending_bytes = queue.db.execute(
                "SELECT COALESCE(SUM(COALESCE(json_extract(payload,'$.raw.size'),"
                "length(CAST(payload AS BLOB)))),0) FROM pending").fetchone()[0]
            remaining = max(0, max_pending_bytes - pending_bytes)
            if remaining == 0:
                captured = {'status': 'deferred', 'reason': 'backlog_byte_budget'}
            else:
                captured = capture_home(queue, home, producer, archive_raw=archive_raw,
                                        chunk_raw=chunk_raw, max_bytes=min(max_bytes, remaining),
                                        reserve_bytes=reserve_bytes)
            # Do not immediately repeat a shared outage request. The next cycle
            # honors the queue's retry timestamps and retains every pending item.
            after = None if before['failed'] else queue.flush(client, limit=flush_limit)
            pending = queue.status()
            state = 'complete' if captured['status'] == 'complete' and not pending['pending'] else 'partial'
            return _record(root, {'version': 1, 'status': state, 'capture': captured,
                    'flush_before': before, 'flush_after': after, 'queue': pending,
                    'pending_bytes_before_capture': pending_bytes})
