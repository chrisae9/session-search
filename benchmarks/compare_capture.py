"""Measure append parsing against full parsing on an independent source copy."""

import argparse
import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path

from session_search.capture.incremental import parse_incremental
from session_search.capture.local import normalize_session
from session_search.capture.parsers.codex import CodexParser


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def compare(source, scratch, *, filename=None):
    filename = filename or source.name
    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise ValueError("rollout filename must be a basename")
    size = source.stat().st_size
    if shutil.disk_usage(scratch).free < size + 2 * 1024 ** 3:
        raise ValueError("insufficient scratch space")
    before = file_hash(source)
    report = {"source_bytes": size, "source_sha256": before, "appends": []}
    with tempfile.TemporaryDirectory(prefix="capture-comparison-", dir=scratch) as work:
        path = Path(work) / filename
        shutil.copyfile(source, path)
        assert file_hash(path) == before, "source changed while copying"
        assert (source.stat().st_dev, source.stat().st_ino) != (path.stat().st_dev, path.stat().st_ino)
        started = time.monotonic()
        initial, checkpoint, _ = parse_incremental(path)
        report["initial_seconds"] = round(time.monotonic() - started, 3)
        report["initial_events"] = len(initial.events)
        del initial
        if checkpoint is None:
            raise ValueError("source has no checkpointable owned turn")
        for index in range(2):
            with path.open("a") as stream:
                for role in ("user", "assistant"):
                    stream.write(json.dumps({
                        "type": "response_item", "timestamp": "2026-01-01T00:00:00Z",
                        "payload": {"type": "message", "role": role, "content": [
                            {"type": "input_text", "text": f"synthetic append {index} {role}"}]},
                    }) + "\n")
            started = time.monotonic()
            actual, checkpoint, work_counts = parse_incremental(path, checkpoint)
            incremental = time.monotonic() - started
            started = time.monotonic()
            reference = normalize_session(CodexParser(
                session_index=path.parent / "no-index", strict=True).parse_session(path))
            full = time.monotonic() - started
            assert actual == reference and actual.revision == reference.revision, "revision parity failed"
            row = {"append": index + 1, "incremental_seconds": round(incremental, 3),
                   "full_seconds": round(full, 3), "exact_revision_parity": True,
                   "events": len(actual.events), "work": work_counts}
            report["appends"].append(row)
            print(json.dumps(row), flush=True)
            del actual, reference
    assert file_hash(source) == before, "source changed during qualification"
    report["source_unchanged"] = True
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--filename", help="original rollout basename when input is a raw object")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("report already exists")
    report = compare(args.source, args.scratch, filename=args.filename)
    with args.output.open("x") as stream:
        args.output.chmod(0o600)
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
