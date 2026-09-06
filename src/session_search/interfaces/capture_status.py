"""Bounded, read-only summaries of this installation's last completed sync."""

import json
from datetime import datetime
from pathlib import Path


def capture_status(root: Path) -> dict:
    path = root / 'sync-status.json'
    if path.is_symlink():
        return {'status': 'unavailable'}
    try:
        with path.open('rb') as stream:
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
        return {'status': value['status'], 'completed_at': completed.isoformat(),
                'counts': summary, 'capture_error_count': len(errors),
                'backlog_deferred': capture.get('reason') == 'backlog_byte_budget'}
    except FileNotFoundError:
        return {'status': 'not_observed'}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {'status': 'unavailable'}
