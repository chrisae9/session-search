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

Add `--archive-raw` to capture only when exact raw files should be retained. Raw files may contain content omitted or redacted from search evidence. Lightweight clients stage these files and upload resumable chunks before submitting their normalized revision. Acknowledgement reclaims the client transfer copy, while the original Codex file remains in place. On the server, completion publishes the verified staging inode with an exclusive hard link and durable directory updates, then removes its staging name. This avoids a second full-file allocation during completion; staging and object storage must share a filesystem. With default server settings, successive changed raw revisions are still separate archived files. `serve --chunk-raw` stores new raw uploads as shared 4 MiB chunks instead, preserving the original full-file digest and the existing client protocol. Identical chunks are verified and reused without rewriting them. Existing whole-file archives remain readable and are not automatically converted or deleted. Both local-only and client capture support `capture --archive-raw --chunk-raw`. Clients stream the reconstructed bytes directly from shared staging chunks using the existing resumable upload protocol. They still read and transmit the whole changed file on a new upload. Changing this option does not convert existing archives or recapture unchanged files.

For bounded background runs, use `capture --max-bytes BYTES --reserve-bytes BYTES`.
The budget counts changed source bytes admitted during that run, including failed
parses; unchanged checkpoints cost no budget. A file larger than the remaining
budget is deferred with its checkpoint intact. Choose a budget large enough for
the largest session that must be captured. Deferred work reports partial coverage.
Capture stages complete records on the data directory's filesystem and checks free
space for that source copy, another source-sized allowance when raw archival is
enabled, and the configured reserve (64 MiB by default). This is a conservative
preflight, not a disk reservation: concurrent activity, source growth, and catalog
writes can still exhaust space. Capture never removes native files to make room.

`status` includes `local_capture_sync`, a read-only summary of this installation's
last completed scheduled sync: timestamp, capture counts, error count, and backlog
deferral. `not_observed` means no sync receipt is present; it does not mean the
catalog is empty. This receipt describes the local computer, not every client or
the standby's replication and backup coverage. Inspect its timestamp as well as
its result; an old successful run does not establish current capture freshness.

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

Use `--embedding-config` to select an explicitly provisioned provider. Configuration contains `mode`, an `identity` object with artifact and dimensions, and either `model_path` for local mode or `endpoint`, `model`, and `response_model` for remote mode. Remote credentials, when needed, use `token_file`. Remote configuration can set `timeout` (default 10 seconds) and `query_timeout` (default 2 seconds), each greater than zero and at most 60 seconds. Interactive embedding requests use the smaller allowance; background indexing retains `timeout`. These are socket I/O timeouts, not a deadline for the full database search. An embedding timeout produces explicit keyword fallback.

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

The storage layer can also verify shared-chunk raw evidence. Snapshots containing
such evidence use manifest version 2 with `raw_storage: files-and-chunks-v1`;
older snapshot verifiers reject that version. Whole-file snapshots retain version 1, and
current code reads both. Raw-file identities remain hashes of the complete
original bytes. Backup and replacement-primary preparation preserve shared chunks
and verify every reconstructed file. Enable it with `serve --chunk-raw` for uploads
or `capture --archive-raw --chunk-raw` for local or client capture. Upgrade recovery tools
before enabling chunk-backed archival; it is not yet the live deployment default.

Client `flush` removes unreferenced staging recipes and chunks after reading all
pending upload references under the capture lock. Conflicted and rejected work
retains its chunks. Interrupted cleanup repeats on a later flush; missing recipes,
corruption, or unexpected storage paths defer cleanup. This applies only to client
transfer copies, never native sessions or server archives. Local archival chunks
remain retained; automatic archival retention is not implemented.

Flush also reclaims private chunk-write temporary files left by a crashed client,
after validating pending recipes and acquiring both capture and flush locks.
Unknown filenames, symlinks, unexpected permissions, or missing pending data defer
cleanup. The cleanup result reports these separately as `removed_temporaries`.

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

## Offline local inference

A local semantic installation needs a compatible native `llama-cpp-python` wheel,
its dependency wheels, and an explicitly provisioned embedding GGUF. Prepare the
wheel bundle on a connected build host matching the target OS and architecture;
then install the Session Search wheel with its `local` extra using
`uv --offline pip install --no-index --find-links BUNDLE_DIRECTORY WHEEL_PATH[local]`.
The wheel path with its extra should be shell-quoted. Python and the installer
must already be available on the offline target.

The pilot qualified Python 3.14 on Apple Silicon macOS 26 with
`llama-cpp-python==0.3.35`, built using `GGML_METAL=ON` and `GGML_NATIVE=OFF`.
The resulting native wheel targets macOS 26; it is not evidence of compatibility
with older macOS versions, Linux, or Windows. Build and qualify separate bundles
for those targets. The remote client installation does not need this runtime.

The local loader verifies the configured model SHA-256, requires a pooled sequence
vector, and disables silent token truncation. Inputs beyond its 8,192-token batch
are rejected. Configure an embedding model whose default pooling produces a
sequence vector; token-level vectors are not interchangeable with that result.
See the upstream [embedding behavior](https://github.com/abetlen/llama-cpp-python#embeddings).

After installing the bundle, run the native qualification separately from the unit
suite, with OS networking denied. On the qualified macOS host:

```sh
sandbox-exec -p '(version 1)(allow default)(deny network*)' \
  .venv/bin/python -I tests/check_local_inference.py MODEL_PATH MODEL_SHA256
```

The check requires a permission-denied network probe, loads the real model, checks
long-input handling, indexes synthetic evidence, and verifies semantic retrieval
and cited context. It reports elapsed time and peak memory; native model startup
and memory are additional costs compared with keyword-only mode. This engine check does not qualify whole-corpus retrieval quality.

Run `tests/check_local_mcp.py MODEL_PATH MODEL_SHA256` under the same OS network
denial with both the `local` and `mcp` extras installed to check the actual stdio
interface. It prepares synthetic vectors in a separate process, starts a fresh
MCP server with an unloaded model, then checks first-query and warm semantic
retrieval, a concurrent literal query, context, and status. Fresh process does not
mean a cold filesystem or Metal shader cache.

The original synchronous pilot measured 0.67 seconds for the first semantic request,
0.04 seconds warm, and 0.62 seconds for a concurrent literal request. With native
model isolation and asynchronous dispatch, a repeat measured roughly 0.75 seconds,
0.04 seconds, and 0.004 seconds respectively. These small synthetic measurements
establish that model startup no longer occupies the MCP event loop; they are not
whole-corpus latency guarantees.

MCP runs local native inference in one spawned child process, isolating the
runtime's process-wide output changes from the stdio transport. There is at most
one outstanding model request. The parent waits up to two seconds for inference;
busy or timed-out calls fall back to keyword retrieval while the existing request
finishes. A later matching request may reuse its completed result. If an outstanding
request is still unfinished after 60 seconds, the next call terminates the worker
before retry can start a replacement. Worker shutdown also terminates the child.
This is checked on calls, not an independent watchdog.

MCP dispatch admits at most eight operations, waiting up to 100 ms for a slot before
reporting `mcp_busy`. Cancelling a caller does not free that slot until its underlying
thread completes. This bounds dispatch work; it does not forcibly interrupt SQLite
queries or remote requests. Native-process startup and IPC add overhead beyond the
inference wait itself. Other frontends' local embedding calls remain in-process.

Add `--short-timeout` to the native MCP check to force keyword fallback with a
1 ms inference wait, then verify that the same worker completes and semantic
retrieval recovers. This exercises fallback without a duplicate model process.

## Optional literal index

`build-literal-index` creates a trigram candidate index on a local writable catalog.
It is opt-in because it adds storage: the pilot measured about 115 MB for its
catalog. The command requires SQLite's FTS5 trigram tokenizer and checks free space
against twice the stored text bytes plus a configurable reserve (2 GiB by default).
As with other capacity checks, this is a preflight rather than a reservation.

```sh
session-search --data-dir DATA_DIRECTORY build-literal-index
```

Construction, readiness metadata, and the insertion trigger commit together.
An interrupted build leaves the previous catalog state intact. The trigger indexes
new immutable evidence in its ingestion transaction, including writes from older
application releases. Building the index advances the publication so future
snapshots can carry it. It consumes replica and backup space as part of the catalog.

Literal retrieval uses at most eight printable ASCII triples to narrow candidates,
then applies its original exact substring predicate, filters, and ordering. Rows containing NULs
are tracked separately and always pass the candidate filter, accommodating older
tokenizers that stop at NUL without altering original evidence. Short
or other queries without a usable triple use the scan path. Missing readiness,
a missing maintenance trigger, unsupported tokenizer, or an evidence high-water
mismatch also selects the scan path. It does not change keyword or hybrid ranking.
The pilot primary has the index enabled; its standby still uses the scan path.
Broader workload and replica qualification remain outstanding.


## Optional incremental capture

Add `--incremental` to `capture` or `sync` to reuse completed normalized turns
from a disposable local parser checkpoint. The source's entire previous byte
prefix is verified before reuse. The unfinished final turn is parsed again with
its original line numbers, and changed ownership metadata forces a full parse.
This does not skip raw archival or change the capture byte and space preflights.

Checkpoints live in `DATA_DIRECTORY/parser-checkpoints`, with a 512 MiB encoded
file budget and a 128 MiB decoded entry limit. Old entries are evicted when needed;
replacement reserves room for both old and new entries and preflights an extra
64 MiB of free space. Oversized, unavailable,
corrupt, or incompatible entries fall back to full parsing. Checkpoints contain
redacted normalized events and ownership metadata, are private, and are not
included in recovery snapshots. They are an optimization, not recovery evidence.

Capture summaries report full and incremental parse counts, reused event totals,
and saved checkpoint counts. `--force` bypasses checkpoint reuse. Parser-source changes
invalidate stored checkpoints automatically. Cache publication follows successful
catalog or queue ingestion; a failed cache write does not undo captured evidence.
The flag remains opt-in. It is enabled in the Mac pilot after capture/queue
qualification and a successful scheduled reuse; other clients need their own
performance qualification.
