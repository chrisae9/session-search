"""Device credentials are files, never command-line arguments or log output."""

import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

from session_search.core.records import canonical_json


def update_device(registry: Path, producer: str, token_file: Path | None = None) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", producer):
        raise ValueError("invalid device name")
    registry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (registry.parent / (registry.name + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        entries = json.loads(registry.read_text()) if registry.exists() else {}
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
        fd, temporary = tempfile.mkstemp(dir=registry.parent, prefix=".credentials-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(canonical_json(entries))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, registry)
            parent = os.open(registry.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            Path(temporary).unlink(missing_ok=True)
