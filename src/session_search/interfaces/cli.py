"""Stable JSON CLI. No search operation starts capture implicitly."""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from session_search.capture.local import capture_home
from session_search.capture.queue import UploadQueue
from session_search.core.output import bounded_response
from session_search.core.records import SearchQuery, canonical_json
from session_search.interfaces.client import Client, RemoteError
from session_search.storage.catalog import Catalog


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="session-search")
    default = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    # A separate directory fences the old skill's archive and index formats.
    result.add_argument("--data-dir", type=Path, default=default / "session-search-v1")
    result.add_argument("--primary", help="explicit remote mode: primary HTTPS endpoint")
    result.add_argument("--standby", help="optional read-only search endpoint")
    result.add_argument("--token-file", type=Path, help="device credential file for remote mode")
    result.add_argument("--embedding-config", type=Path, help="explicit model configuration file")
    result.add_argument("--allow-remote-embeddings", action="store_true",
                        help="explicitly permit the configured remote model endpoint")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create an isolated catalog")
    legacy = commands.add_parser("import-legacy", help="import a verified legacy archive into a new store")
    legacy.add_argument("archive", type=Path)
    legacy.add_argument("--allow-superseded-conflicts", action="store_true",
                        help="permit conflicting old revisions only when a unique newer revision exists")
    commands.add_parser("status", help="inspect catalog coverage")
    commands.add_parser("compact-client", help="reclaim acknowledged upload payload pages when idle")
    commands.add_parser("fence-primary", help="prevent further cooperating local catalog and transfer writes")
    snapshot = commands.add_parser("snapshot", help="create a consistent verified backup input")
    snapshot.add_argument("destination", type=Path)
    snapshot.add_argument("--search-only", action="store_true",
                          help="exclude raw archival evidence; cannot be used for recovery backups")
    verify = commands.add_parser("verify-snapshot", help="check a snapshot and all its raw evidence")
    verify.add_argument("snapshot", type=Path)
    replica = commands.add_parser("activate-replica", help="stage, verify, and activate a read-only replica")
    replica.add_argument("snapshot", type=Path)
    receive = commands.add_parser("receive-replica", help="verify and consume a staged search replica")
    receive.add_argument("snapshot", type=Path)
    replicate = commands.add_parser("replicate", help="publish a search replica over SSH, resuming interrupted work")
    replicate.add_argument("--outbox", type=Path, required=True)
    replicate.add_argument("--host", required=True)
    replicate.add_argument("--remote-data", required=True)
    replicate.add_argument("--remote-executable", required=True)
    commands.add_parser("prune-replica", help="retain current, previous, and actively read replica generations")
    backup = commands.add_parser("backup", help="back up and restore-verify a snapshot on two repositories")
    backup.add_argument("snapshot", type=Path)
    backup.add_argument("--repositories", type=Path, required=True)
    backup.add_argument("--receipt", type=Path, required=True)
    restore = commands.add_parser("restore-backup", help="retain an exact verified read-only recovery snapshot")
    restore.add_argument("destination", type=Path)
    restore.add_argument("--repositories", type=Path, required=True)
    restore.add_argument("--repository", required=True)
    restore.add_argument("--receipt", type=Path, required=True)
    offload = commands.add_parser("plan-offload", help="write a reviewable manual offload plan")
    offload.add_argument("--receipt", type=Path, required=True)
    offload.add_argument("--output", type=Path, required=True)
    apply = commands.add_parser("apply-offload", help="reverify backups and apply the exact reviewed plan")
    apply.add_argument("plan", type=Path)
    apply.add_argument("--plan-id", required=True)
    apply.add_argument("--repositories", type=Path, required=True)
    commands.add_parser("mcp", help="serve the three read-only agent tools over stdio")
    embed = commands.add_parser("embed", help="process bounded pending semantic work")
    embed.add_argument("--limit", type=int, default=100)
    flush = commands.add_parser("flush", help="send queued revisions to the primary")
    flush.add_argument("--limit", type=int, default=100)
    flush.add_argument("--bootstrap-imports", action="store_true",
                       help="adopt exact legacy import heads before the first native upload")
    flush.add_argument("--reconcile-raw-prefixes", action="store_true",
                       help="rebase queued Codex revisions only against proven raw-file prefixes")
    serve = commands.add_parser("serve", help="serve HTTP on loopback behind a TLS proxy")
    serve.add_argument("--credentials", type=Path, required=True)
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--readonly", action="store_true")
    device = commands.add_parser("device", help="add or revoke one device credential")
    device.add_argument("action", choices=["add", "revoke"])
    device.add_argument("name")
    device.add_argument("--registry", type=Path, required=True)
    device.add_argument("--output", type=Path, help="new credential file; must not already exist")
    receive_credentials = commands.add_parser("receive-credentials", help="accept a versioned registry through stdin")
    receive_credentials.add_argument("--registry", type=Path, required=True)
    sync_credentials = commands.add_parser("sync-credentials", help="synchronize credential revisions over SSH")
    sync_credentials.add_argument("--registry", type=Path, required=True)
    sync_credentials.add_argument("--host", required=True)
    sync_credentials.add_argument("--remote-registry", required=True)
    sync_credentials.add_argument("--remote-executable", required=True)
    sync_credentials.add_argument("--receipt", type=Path, required=True)
    capture = commands.add_parser("capture", help="capture complete Codex records locally")
    capture.add_argument("--codex-home", type=Path,
                         default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    capture.add_argument("--producer", required=True, help="stable identity of this installation")
    capture.add_argument("--archive-raw", action="store_true",
                         help="explicitly retain exact raw session files in this local catalog")
    capture.add_argument("--force", action="store_true",
                         help="explicit recovery: recapture files even when checkpoints match")
    search = commands.add_parser("search", help="retrieve cited evidence")
    search.add_argument("query")
    search.add_argument("--literal", action="store_true")
    search.add_argument("--role", choices=["user", "assistant"])
    for option in ("after", "before", "project", "session-id", "producer"):
        search.add_argument("--" + option)
    search.add_argument("--include-subagents", action="store_true")
    search.add_argument("--include-current-session", action="store_true")
    search.add_argument("--exclude-session", action="append", default=[])
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--budget", type=int, default=16384)
    context = commands.add_parser("context", help="expand immutable search citations")
    context.add_argument("citations", help="JSON array of citation objects")
    context.add_argument("--neighbors", type=int, default=2)
    context.add_argument("--budget", type=int, default=32768)
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "import-legacy":
            if args.primary or args.standby or args.token_file:
                raise ValueError("legacy import runs locally on the destination data host")
            from session_search.capture.legacy import import_archive
            print(canonical_json(import_archive(args.archive, args.data_dir,
                allow_superseded_conflicts=args.allow_superseded_conflicts)))
            return 0
        if (args.standby or args.token_file) and not args.primary:
            raise ValueError("remote settings require an explicit primary endpoint")
        if args.primary and not args.token_file:
            raise ValueError("remote mode requires a token file")
        client = Client(args.primary, args.token_file, standby=args.standby) if args.primary else None
        from session_search.core.embeddings import load_provider
        provider = load_provider(args.embedding_config, allow_remote=args.allow_remote_embeddings)
        if args.command in {"backup", "restore-backup", "plan-offload", "apply-offload"}:
            if client:
                raise ValueError("backup and offload administration runs on the data host")
            from session_search.storage.backups import backup_all, load_repositories, restore_backup
            from session_search.storage.offload import apply_offload, plan_offload
            if args.command == "backup":
                output = backup_all(args.snapshot, load_repositories(args.repositories), args.receipt)
            elif args.command == "restore-backup":
                repositories = [r for r in load_repositories(args.repositories) if r.name == args.repository]
                receipt = json.loads(args.receipt.read_text())
                receipts = receipt.get("receipts", [receipt])
                matches = [r for r in receipts if r.get("repository") == args.repository]
                if len(repositories) != 1 or len(matches) != 1:
                    raise ValueError("select exactly one configured repository and matching receipt")
                output = restore_backup(repositories[0], matches[0], args.destination)
            elif args.command == "plan-offload":
                with Catalog(args.data_dir.resolve(), readonly=True) as catalog:
                    output = plan_offload(catalog, args.receipt)
                with args.output.open("x") as f:
                    f.write(canonical_json(output))
                output = {"version": 1, "status": "planned", "plan_id": output["plan_id"],
                          "candidates": len(output["candidates"]), "skipped": output["skipped"]}
            else:
                with Catalog(args.data_dir.resolve()) as catalog:
                    output = apply_offload(json.loads(args.plan.read_text()),
                        load_repositories(args.repositories), catalog=catalog,
                        expected_plan_id=args.plan_id)
            print(canonical_json(output))
            return 0
        if args.command in {"replicate", "receive-replica"}:
            from session_search.storage.replication import receive_replica, replicate
            if client:
                raise ValueError("replication administration runs on the data host")
            output = (receive_replica(args.snapshot, args.data_dir) if args.command == "receive-replica"
                      else replicate(args.data_dir, args.outbox, args.host,
                                     args.remote_data, args.remote_executable))
            print(canonical_json(output))
            return 0
        if args.command in {"snapshot", "verify-snapshot", "activate-replica", "prune-replica"}:
            from session_search.storage.snapshots import activate_replica, create_snapshot, verify_snapshot
            if client:
                raise ValueError("snapshot administration must run on the data host")
            if args.command == "snapshot":
                with Catalog(args.data_dir.resolve(), readonly=True) as catalog:
                    output = create_snapshot(catalog, args.destination, search_only=args.search_only)
            elif args.command == "verify-snapshot":
                output = verify_snapshot(args.snapshot)
            elif args.command == "prune-replica":
                from session_search.storage.generations import prune_replica
                output = prune_replica(args.data_dir)
            else:
                output = activate_replica(args.snapshot, args.data_dir)
            print(canonical_json(output))
            return 0
        if client and provider:
            raise ValueError("remote clients use the server's embedder, not a client model")
        if args.command in {"receive-credentials", "sync-credentials"}:
            from session_search.interfaces.credentials import receive_credentials, sync_credentials
            if client:
                raise ValueError("credential synchronization runs through SSH administration")
            if args.command == "receive-credentials":
                raw = sys.stdin.buffer.read(65537)
                if len(raw) > 65536:
                    raise ValueError("credential registry exceeds transfer limit")
                output = receive_credentials(args.registry, json.loads(raw))
            else:
                output = sync_credentials(args.registry, args.host, args.remote_registry,
                                          args.remote_executable, args.receipt)
            print(canonical_json(output))
            return 0 if output["status"] == "synchronized" else 2
        if args.command == "device":
            from session_search.interfaces.credentials import update_device
            if args.action == "add" and not args.output:
                raise ValueError("adding a device requires an output credential file")
            update_device(args.registry, args.name, args.output if args.action == "add" else None)
            print(canonical_json({"version": 1, "status": "ok", "device": args.name}))
            return 0
        if args.command == "serve":
            import uvicorn
            from session_search.interfaces.server import create_app
            with Catalog(args.data_dir.resolve(), readonly=args.readonly):
                pass
            uvicorn.run(create_app(args.data_dir, args.credentials, readonly=args.readonly,
                                   provider=provider),
                        host="127.0.0.1", port=args.port, access_log=False)
            return 0
        if args.command == "mcp":
            from session_search.interfaces.mcp import create_mcp
            create_mcp(args.data_dir, client, provider).run(transport="stdio")
            return 0
        if args.command == "embed":
            if provider is None:
                raise ValueError("embed requires an explicitly configured model")
            from session_search.storage.semantic import index_pending
            with Catalog(args.data_dir.resolve()) as catalog:
                output = index_pending(catalog, provider, limit=args.limit)
            print(canonical_json(output))
            return 0
        if args.command == "flush":
            if not client:
                raise ValueError("flush requires remote mode")
            with UploadQueue(args.data_dir) as queue:
                output = queue.flush(client, limit=args.limit, bootstrap_imports=args.bootstrap_imports,
                                     reconcile_raw_prefixes=args.reconcile_raw_prefixes)
            print(canonical_json(output))
            return 0
        if args.command == "fence-primary":
            from session_search.storage.fencing import fence_primary
            if client:
                raise ValueError("primary fencing runs locally on the data host")
            output = fence_primary(args.data_dir)
            print(canonical_json(output))
            return 0 if output["status"] == "fenced" else 2
        if args.command == "compact-client":
            if not (args.data_dir / "upload-queue.sqlite3").is_file():
                raise ValueError("client upload queue is not initialized")
            with UploadQueue(args.data_dir) as queue:
                output = queue.compact()
            print(canonical_json(output))
            return 0
        if client:
            output = remote_command(args, client)
            print(canonical_json(output))
            return 0
        write = args.command in {"init", "capture"}
        with Catalog(args.data_dir.resolve(), readonly=not write) as catalog:
            if args.command in {"init", "status"}:
                output = {"version": 1, "status": "ok", "coverage": catalog.status()}
            elif args.command == "capture":
                output = capture_home(catalog, args.codex_home, args.producer, archive_raw=args.archive_raw,
                                      force=args.force)
            elif args.command == "search":
                exclude = list(args.exclude_session)
                current = os.environ.get("CODEX_THREAD_ID")
                if current and not args.include_current_session:
                    exclude.append(current)
                query = SearchQuery(
                    args.query, literal=args.literal, role=args.role, after=args.after,
                    before=args.before, project=args.project, session_id=args.session_id,
                    producer=args.producer, exclude_sessions=tuple(exclude),
                    include_subagents=args.include_subagents, limit=args.limit,
                )
                from session_search.storage.semantic import hybrid_search
                output = bounded_response(hybrid_search(catalog, query, provider), args.budget)
            else:
                values = json.loads(args.citations)
                if not isinstance(values, list):
                    raise ValueError("citations must be a JSON array")
                output = bounded_response(catalog.context(
                    values, neighbors=args.neighbors,
                ), args.budget)
        print(canonical_json(output))
        return 0
    except (ValueError, TypeError, OSError, sqlite3.Error, RemoteError, RuntimeError) as exc:
        # SQLite and OS diagnostics can contain private paths. Expose error type,
        # with validation messages only where they are controlled by this package.
        detail = str(exc) if isinstance(exc, ValueError) and not isinstance(
            exc, json.JSONDecodeError
        ) else "operation failed; verify initialization, inputs, and data access"
        print(canonical_json({"version": 1, "status": "error",
                              "error": type(exc).__name__, "detail": detail}), file=sys.stderr)
        return 2


def remote_command(args, client: Client) -> dict:
    from dataclasses import asdict
    if args.command == "capture":
        with UploadQueue(args.data_dir) as queue:
            result = capture_home(queue, args.codex_home, args.producer, archive_raw=args.archive_raw,
                                  force=args.force)
            result["queue"] = queue.status()
            return result
    if args.command == "init":
        with UploadQueue(args.data_dir) as queue:
            return {"version": 1, "status": "ok", "queue": queue.status()}
    if args.command == "status":
        result = client.read("status")
        if (args.data_dir / "upload-queue.sqlite3").exists():
            with UploadQueue(args.data_dir) as queue:
                result["queue"] = queue.status()
        return result
    if args.command == "context":
        return bounded_response(client.read("context", {
            "citations": json.loads(args.citations), "neighbors": args.neighbors,
            "budget": args.budget}), args.budget)
    exclude = list(args.exclude_session)
    if not args.include_current_session and os.environ.get("CODEX_THREAD_ID"):
        exclude.append(os.environ["CODEX_THREAD_ID"])
    query = SearchQuery(args.query, args.literal, args.role, args.after, args.before,
                        args.project, args.session_id, args.producer, tuple(exclude),
                        args.include_subagents, args.limit)
    return bounded_response(client.read("search", {**asdict(query), "budget": args.budget}), args.budget)


if __name__ == "__main__":
    raise SystemExit(main())
