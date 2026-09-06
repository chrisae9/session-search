"""Bounded, read-only summaries of this installation's last completed sync."""

import json
import os
import stat
from datetime import datetime
from pathlib import Path


def with_outage_capture_status(result: dict, root: Path) -> dict:
    """Keep a search outage distinct from missing local capture or pending work."""
    if result.get('status') != 'unavailable':
        return result
    import sqlite3
    import time
    queue = {'status': 'not_initialized'}
    path = root / 'upload-queue.sqlite3'
    try:
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None:
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('queue is not a regular file')
            db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.1)
            try:
                deadline = time.monotonic() + 0.1
                db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                rows = db.execute('SELECT state,COUNT(*) FROM pending GROUP BY state').fetchall()
                if any(state not in {'pending', 'conflict', 'rejected'} for state, _ in rows):
                    raise ValueError('unknown queue state')
                queue = {'pending': sum(count for _, count in rows), 'states': dict(rows)}
            finally:
                db.close()
    except (OSError, ValueError, sqlite3.Error):
        queue = {'status': 'unavailable'}
    return {**result, 'local_capture_sync': capture_status(root), 'queue': queue}


def capture_status(root: Path) -> dict:
    path = root / 'sync-status.json'
    try:
        # Check the opened inode so pathname replacement cannot bypass validation
        # or turn a status request into a blocking FIFO read.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('sync receipt is not a regular file')
            content = stream.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError('oversized sync receipt')
        value = json.loads(content)
        if value['version'] != 1 or value['status'] not in {'complete', 'partial', 'empty', 'unavailable'}:
            raise ValueError('invalid sync receipt')
        completed = datetime.fromisoformat(value['completed_at'])
        if completed.tzinfo is None:
            raise ValueError('sync receipt requires a timezone')
        capture = value['capture']
        counts = capture.get('counts', {})
        summary = {key: counts[key] for key in ('captured', 'unchanged', 'deferred', 'changed_during_read')
                   if key in counts}
        if any(type(count) is not int or count < 0 for count in summary.values()):
            raise ValueError('invalid capture counts')
        errors = capture.get('errors', [])
        if not isinstance(errors, list):
            raise ValueError('invalid capture errors')
        result = {'status': value['status'], 'completed_at': completed.isoformat(),
                'counts': summary, 'capture_error_count': len(errors),
                'backlog_deferred': capture.get('reason') == 'backlog_byte_budget'}
        if 'parser' in capture:
            parser = capture['parser']
            if not isinstance(parser, dict):
                raise ValueError('invalid parser summary')
            selected = {key: parser[key] for key in
                        ('full', 'incremental', 'reused_events', 'checkpoints_saved') if key in parser}
            if any(type(count) is not int or count < 0 for count in selected.values()):
                raise ValueError('invalid parser counts')
            result['parser'] = selected
        return result
    except FileNotFoundError:
        return {'status': 'not_observed'}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {'status': 'unavailable'}
