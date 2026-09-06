# Development usage

These commands operate on an isolated development store. They do not migrate the legacy skill. Run `uv sync --group dev --extra server --extra mcp --extra embeddings` first; examples below use the installed executable.

## Local capture and search

```sh
.venv/bin/session-search --data-dir demo-state/local init
.venv/bin/session-search --data-dir demo-state/local capture --producer workstation
.venv/bin/session-search --data-dir demo-state/local search 'backup restore'
.venv/bin/session-search --data-dir demo-state/local status
```

Capture defaults to the configured Codex home; `--codex-home` selects a different source. It reads completed JSONL records, preserves a partial tail for a later scan, and never deletes history when a source file disappears. Ownership metadata and response records are scanned in separate passes so ignored raw payloads do not accumulate in memory. Changed files are still reparsed; unchanged files are skipped.

Pass the returned citation objects unchanged to `context` as a JSON array. Search excludes the active Codex thread tree when `CODEX_THREAD_ID` is available. `--literal` bypasses embeddings; role, project, session, producer, and time filters remain strict.

Add `--archive-raw` to capture only when exact raw files should be retained. Raw files may contain content omitted or redacted from search evidence. Lightweight clients stage these files and upload resumable chunks before submitting their normalized revision. Acknowledgement reclaims the client transfer copy, while the original Codex file remains in place. On the server, completion publishes the verified staging inode with an exclusive hard link and durable directory updates, then removes its staging name. This avoids a second full-file allocation during completion; staging and object storage must share a filesystem. Successive changed raw revisions are still separate archived files, so routine capture retention requires further coordination.

## Agent interface

Launch `.venv/bin/session-search --data-dir demo-state/local mcp` as a stdio MCP server. It exposes only `search`, `context`, and `status`; capture and administrative maintenance are separate CLI operations. The MCP adapter returns one bounded text payload per tool call to avoid duplicating evidence in structured and text outputs.

For Codex, register the installed executable as a stdio server. For a local-only
store:

```sh
codex mcp add session-search -- /absolute/path/session-search \
  --data-dir /absolute/path/store mcp
```

For a lightweight remote client, add `--primary`, `--standby`, and `--token-file`
before `mcp`, using the same configuration as the CLI examples below. Credentials
remain in the token file; do not put token values in command arguments. Install the
`mcp` extra on the client; remote clients do not need the embedding extra or model
files. The server configuration applies when Codex loads its MCP connections.

The optional [agent skill](../skills/session-search/SKILL.md) explains evidence
selection, immutable context, and coverage limits. It uses the three MCP tools and
does not trigger capture or maintenance during search. When replacing a legacy
skill, preserve its runtime data separately and avoid installing two skills with
the same name. Registering MCP alone does not replace legacy skill instructions.


## HTTP server and client

```sh
.venv/bin/session-search device add workstation \
  --registry demo-state/devices.json --output demo-state/workstation.token
.venv/bin/session-search --data-dir demo-state/server serve \
  --credentials demo-state/devices.json --port 8765
```

The server binds only to loopback. Put it behind a trusted HTTPS proxy for cross-machine access. Device tokens are written to private files; the server registry contains hashes. Removing a device with `device revoke` takes effect on its next request to that server.

Use `sync-credentials --registry REGISTRY --host STANDBY --remote-registry REMOTE_REGISTRY --remote-executable REMOTE_EXECUTABLE --receipt RECEIPT` to propagate credentials over SSH. The receiver must already run this version. Initial binding accepts a missing receiver registry or an identical legacy copy; divergent copies require reconciliation. Versioned receivers reject older revisions, changed contents at the same revision, and different authorities. Edit credentials only on the authority.

The credential service and timer templates retry once per minute after the previous run finishes, independently of search replication and embeddings. Configure `SESSION_SEARCH_CREDENTIALS`, `SESSION_SEARCH_CREDENTIAL_HOST`, `SESSION_SEARCH_REMOTE_CREDENTIALS`, `SESSION_SEARCH_REMOTE_EXECUTABLE`, and `SESSION_SEARCH_CREDENTIAL_RECEIPT` in the service environment file. A synchronization is confirmed only when the peer acknowledges the exact revision; failures retain the last acknowledgement and report partial status. An intervening local edit reports behind status and needs another synchronization.

Revocation is asynchronous across hosts: an unreachable peer may still accept its previous device list until synchronization succeeds. After a sensitive revocation, run synchronization immediately and inspect its result before claiming every server has revoked access. The timer retries failures but does not establish an instantaneous global revocation guarantee.

In another shell:

```sh
.venv/bin/session-search --data-dir demo-state/client \
  --primary http://127.0.0.1:8765 --token-file demo-state/workstation.token \
  capture --producer workstation
.venv/bin/session-search --data-dir demo-state/client \
  --primary http://127.0.0.1:8765 --token-file demo-state/workstation.token flush
```

`--standby` adds a read-only alternate for search, context, and status. Uploads never fail over. Conflicting revisions stay queued for reconciliation; repeated connection failures back off without deleting pending payloads. A flush sends at most one pending revision per session per pass, preserving order.

Normalized payloads over 8 MiB automatically use resumable 1 MiB chunks in a separate temporary namespace. A checksum-verified payload then goes through the same revision conflict and idempotency checks as an ordinary upload. Successful ingestion removes this transfer copy; it does not archive raw files. The current normalized payload ceiling is 256 MiB. Larger payloads remain in the client queue as rejected work, requiring a supported format or size change before retry. Large JSON parsing is serialized across server worker processes to bound memory use; busy clients retain their payloads and retry.

## Embeddings

Use `--embedding-config` to select an explicitly provisioned provider. Configuration contains `mode`, an `identity` object with artifact and dimensions, and either `model_path` for local mode or `endpoint`, `model`, and `response_model` for remote mode. Remote credentials, when needed, use `token_file`.

Local artifacts require `identity.artifact` to equal `sha256:` followed by the model file's SHA-256 digest. Remote embeddings additionally require `--allow-remote-embeddings`; no remote fallback is inferred. Optional local inference dependencies must be installed beforehand.

Run `embed --limit 100` with the same data directory and embedding configuration to process a bounded backlog. Searches with that configuration use hybrid retrieval and report keyword fallback if the provider fails. Literal searches never request embeddings.

## Client maintenance

Run `session-search --data-dir CLIENT_DATA compact-client` after draining the upload queue to reclaim unused SQLite payload pages. It preserves acknowledgements and capture checkpoints, and defers while capture or flush is busy, any pending/conflicted/rejected work remains, or scratch space is insufficient. It affects only the local upload queue; search catalogs and native session files are untouched.

## Offline installation verification

The core wheel has no third-party dependencies. After building it, run `python tests/check_offline_wheel.py dist` to install it into a clean environment with package-manager networking disabled. The installed CLI scenario also blocks Python socket operations and external processes, and runs without optional packages. It covers capture, search, immutable context, missing-model fallback, and snapshots. For MCP, prepare dependency wheels on a connected computer matching the target
operating system, architecture, and Python version:

```sh
BUNDLE_DIR=/absolute/path/offline-bundle
mkdir -p "$BUNDLE_DIR"
uv export --locked --no-dev --extra mcp --no-emit-project \
  --format requirements-txt --output-file "$BUNDLE_DIR/mcp-requirements.txt"
python -m pip download --only-binary=:all: --require-hashes \
  -r "$BUNDLE_DIR/mcp-requirements.txt" --dest "$BUNDLE_DIR/wheels"
python tests/check_offline_mcp.py dist "$BUNDLE_DIR/wheels"
```

Keep these generated requirements and wheels outside the source checkout. The
MCP verifier installs from the prepared wheels with index access and package cache
disabled, then exercises actual stdio search, immutable context, and status while
blocking Internet sockets, DNS, connections, and child commands in the server.
It also verifies keyword fallback when a local model is absent. Python and `uv`
must already be installed. This qualifies local keyword MCP operation; local
inference still requires separately provisioned model artifacts and native runtime
qualification. CI repeats core and MCP checks on its supported platform matrix.

## Recovery development

`snapshot DESTINATION` uses SQLite's backup API and includes referenced raw objects. `verify-snapshot SNAPSHOT` verifies the database checksum, database references, and every raw object. `activate-replica SNAPSHOT` stages and verifies a copy before switching the replica's current generation.

For a space-constrained search standby, `snapshot DESTINATION --search-only` keeps all normalized revisions, citations, and search indexes while excluding raw objects and their recovery references. The snapshot declares its purpose as `search-replica`; recovery backup commands reject it. Use the default complete snapshot for raw-file recovery and offload verification.

Activation retires obsolete generations while retaining the current and previous snapshots and any older snapshot held by an active reader. Reader pins use operating-system locks and are released if the reader crashes. `prune-replica` repeats this cleanup after readers finish. It only removes managed replica generations; canonical evidence in the current catalog, source snapshots, and backup repositories remain intact.

SSH replication consumes its verified incoming directory during activation, avoiding a second catalog copy on the standby. The sender retains its snapshot until acknowledgement and can resend after interruption. Reserve space for the current, previous, and incoming generations, plus any generations pinned by active readers and separate recovery backups.

`backup` takes a repository configuration containing at least two distinct initialized Restic repositories, each with `name`, `repository`, and `password_file`. It restores each completed backup before writing a recovery receipt. Repository initialization and backup pruning are never implicit.

Set each repository's optional `restore_directory` to an existing scratch directory with room for the complete uncompressed restore. Restore verification runs on the invoking data host, even when the repository is remote. Do not size this directory from the compressed backup size.

To keep a recovered snapshot for inspection, select one repository from a saved receipt:

```sh
session-search restore-backup recovered-snapshot \
  --repositories repositories.json --repository backup-one --receipt receipt.json
session-search --data-dir recovered-snapshot search "recovery"
```

The destination must not exist. Recovery checks the repository identity, restores the exact backup ID, and verifies all referenced evidence against the receipt before publishing the destination. Reserve uncompressed restore space beside that destination. Both combined two-repository receipts and individual repository receipts are accepted; restoring one repository does not establish two-repository offload safety.

Snapshots reject capture, ingestion, and embedding writes. This command does not promote a standby, enable a server, or fence an old primary. Replacement-writer preparation and fencing remain a separate recovery requirement.

`plan-offload` produces a reviewable candidate plan. `apply-offload` requires that plan's ID, re-restores both backups, checks exact revision coverage, and refuses to run while Codex writers are detected. These primitives are tested with synthetic data; complete the deployment and migration gates before using them on retained personal sessions.
