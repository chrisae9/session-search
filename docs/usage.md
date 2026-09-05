# Development usage

These commands operate on an isolated development store. They do not migrate the legacy skill. Run `uv sync --group dev --extra server --extra mcp --extra embeddings` first; examples below use the installed executable.

## Local capture and search

```sh
.venv/bin/session-search --data-dir demo-state/local init
.venv/bin/session-search --data-dir demo-state/local capture --producer workstation
.venv/bin/session-search --data-dir demo-state/local search 'backup restore'
.venv/bin/session-search --data-dir demo-state/local status
```

Capture defaults to the configured Codex home; `--codex-home` selects a different source. It reads completed JSONL records, preserves a partial tail for a later scan, and never deletes history when a source file disappears. Changed files are currently reparsed; unchanged files are skipped.

Pass the returned citation objects unchanged to `context` as a JSON array. Search excludes the active Codex thread tree when `CODEX_THREAD_ID` is available. `--literal` bypasses embeddings; role, project, session, producer, and time filters remain strict.

Add `--archive-raw` to local capture only when exact raw files should be retained. Raw files may contain content omitted or redacted from search evidence. This option is currently unavailable on lightweight remote clients.

## Agent interface

Launch `.venv/bin/session-search --data-dir demo-state/local mcp` as a stdio MCP server. It exposes only `search`, `context`, and `status`; capture and administrative maintenance are separate CLI operations. The MCP adapter returns one bounded text payload per tool call to avoid duplicating evidence in structured and text outputs.

## HTTP server and client

```sh
.venv/bin/session-search device add workstation \
  --registry demo-state/devices.json --output demo-state/workstation.token
.venv/bin/session-search --data-dir demo-state/server serve \
  --credentials demo-state/devices.json --port 8765
```

The server binds only to loopback. Put it behind a trusted HTTPS proxy for cross-machine access. Device tokens are written to private files; the server registry contains hashes. Removing a device with `device revoke` takes effect on its next request to that server. Propagation to other serving replicas is still a release requirement.

In another shell:

```sh
.venv/bin/session-search --data-dir demo-state/client \
  --primary http://127.0.0.1:8765 --token-file demo-state/workstation.token \
  capture --producer workstation
.venv/bin/session-search --data-dir demo-state/client \
  --primary http://127.0.0.1:8765 --token-file demo-state/workstation.token flush
```

`--standby` adds a read-only alternate for search, context, and status. Uploads never fail over. Conflicting revisions stay queued for reconciliation; repeated connection failures back off without deleting pending payloads. A flush sends at most one pending revision per session per pass, preserving order.

## Embeddings

Use `--embedding-config` to select an explicitly provisioned provider. Configuration contains `mode`, an `identity` object with artifact and dimensions, and either `model_path` for local mode or `endpoint`, `model`, and `response_model` for remote mode. Remote credentials, when needed, use `token_file`.

Local artifacts require `identity.artifact` to equal `sha256:` followed by the model file's SHA-256 digest. Remote embeddings additionally require `--allow-remote-embeddings`; no remote fallback is inferred. Optional local inference dependencies must be installed beforehand.

Run `embed --limit 100` with the same data directory and embedding configuration to process a bounded backlog. Searches with that configuration use hybrid retrieval and report keyword fallback if the provider fails. Literal searches never request embeddings.

## Recovery development

`snapshot DESTINATION` uses SQLite's backup API and includes referenced raw objects. `verify-snapshot SNAPSHOT` verifies the database checksum, database references, and every raw object. `activate-replica SNAPSHOT` stages and verifies a copy before switching the replica's current generation.

`backup` takes a repository configuration containing at least two distinct initialized Restic repositories, each with `name`, `repository`, and `password_file`. It restores each completed backup before writing a recovery receipt. Repository initialization and backup pruning are never implicit.

`plan-offload` produces a reviewable candidate plan. `apply-offload` requires that plan's ID, re-restores both backups, checks exact revision coverage, and refuses to run while Codex writers are detected. These primitives are tested with synthetic data; complete the deployment and migration gates before using them on retained personal sessions.
