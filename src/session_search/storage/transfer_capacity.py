"""Optional process-shared admission for raw and normalized upload staging."""

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path


def validate_budget(value, scan_limit=10000):
    if type(scan_limit) is not int or not 1 <= scan_limit <= 1000000:
        raise ValueError('staging scan limit must be between 1 and 1000000 entries')
    if value is not None and (type(value) is not int or value < 1):
        raise ValueError('staging byte limit must be a positive integer')


@contextmanager
def staging_admission(root: Path, incoming: int, limit: int | None, scan_limit: int = 10000):
    validate_budget(limit, scan_limit)
    if limit is None or incoming == 0:
        yield
        return
    # Keep this inode permanently. All configured upload writers on this data
    # root serialize the scan and durable append, across both namespaces.
    fd = os.open(root / '.transfer-capacity.lock',
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('invalid staging capacity lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        used = entries = 0
        for directory in ('transfers', 'revision-transfers'):
            try:
                directory_fd = os.open(root / directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue
            try:
                with os.scandir(directory_fd) as children:
                    for child in children:
                        entries += 1
                        if entries > scan_limit:
                            raise OSError('staging capacity scan limit reached')
                        if not child.name.endswith('.part'):
                            continue
                        try:
                            info = child.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            # Completed-upload cleanup can only reduce usage.
                            continue
                        if not stat.S_ISREG(info.st_mode):
                            raise OSError('unsupported staging partial type')
                        used += info.st_size
                        if used + incoming > limit:
                            raise OSError('staging byte limit reached; retain upload for retry')
            finally:
                os.close(directory_fd)
        if used + incoming > limit:
            raise OSError('staging byte limit reached; retain upload for retry')
        yield
    finally:
        os.close(fd)
