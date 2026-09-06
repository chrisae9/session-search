# Storage maintenance

Session Search reclaims several kinds of disposable state, but does not yet
provide bounded retention for every server artifact. Free-space checks defer new
work; they do not reclaim existing data or reserve capacity against other jobs.
Keep native session offload separate from housekeeping: it requires the exact
dual-restore checks described in [offload](offload.md).

## Implemented cleanup

| State | Trigger and retention |
| --- | --- |
| Client upload chunks and recipes | `flush` validates every pending upload under capture and flush locks, then removes unreferenced staging objects. Conflicted and rejected uploads remain roots. Recognized private chunk-write temporaries are also reclaimed; invalid state defers cleanup. |
| Client queue database free pages | `compact-client` compacts an idle, drained queue when scratch capacity is sufficient. It retains acknowledgements and capture checkpoints. |
| Completed server upload staging | Successful append/finalization removes that producer's partial file. Append and status retries can finish interrupted cleanup after verifying the completed object. Status cleanup takes the writer lease and a nonblocking per-transfer lock; busy or fenced stores retain the duplicate. Other producers' staging and the stable lock file remain intact. |
| Expired upload partials | Explicit `expire-transfers --apply` removes eligible managed partials after writer exclusion and identity rechecks. Dry run is the default; published objects, raw recovery references, and stable lock files are retained. |
| Replica generations | Activation and `prune-replica` retain current, previous, and actively pinned generations. Reader process exit releases its pin. Other staging directories are outside this cleanup. |
| Recovery snapshot outbox | One pending snapshot is retried until every required destination is restore-verified. The complete receipt is saved before retiring staging; a later cycle can finish interrupted retirement using that receipt. |
| Verification jobs | Admission is serialized, with at most 32 managed job records. Completed records expire after 900 seconds; subsequent new-job admission performs cleanup. Worker-owned scratch is reclaimed only after its ownership checks pass. Expiry is not a periodic deletion timer. |

These mechanisms preserve canonical revisions, cited evidence, and pending work.
They do not imply a fixed total disk budget: pending uploads, active reader pins,
and growing history can legitimately require more space.

## Retained state and remaining work

| State | Current behavior |
| --- | --- |
| Server upload capacity | Manual partial expiry is available below; optional shared staging admission is available below. No automatic expiry timer is implemented. |
| Server transfer lock files | Persist after transfers. Do not unlink them while readers or writers can hold open descriptors: a new file at the same path would create a different lock and break mutual exclusion. |
| Server or local archive chunk temporaries | Ordinary exception cleanup removes them, but a killed process can leave `.chunk-*` files. The client-only cleanup above must not be applied to a canonical catalog. No server sweep is implemented. |
| Interrupted backup snapshot construction | `.snapshot-*` staging defers the next cycle for operator inspection. It is not automatically deleted or treated as a completed backup. |
| Completed backup receipts | Retained per snapshot; no receipt retention policy is implemented. A saved receipt records earlier verification, not present repository availability. |
| Canonical objects and backup repositories | No automatic archival deletion or Restic pruning. Source disappearance does not authorize deletion. |

Additional server cleanup needs a separate ownership and concurrency protocol,
bounded work per run, and crash tests before it can remove these artifacts. An
old modification time alone cannot prove that a transfer is abandoned or that
an object is unreferenced. Receipt retention must preserve recovery references
and interrupted-cycle recovery. Generic recursive deletion is not a supported
maintenance workflow.

The queue can recover from a raw partial disappearing between its status lookup
and next append: the stale offset receives retryable HTTP 503, the client retains
its exact staged bytes, and the next flush starts from current server progress.
Regression tests cover file and shared-chunk layouts on both sides, verify that
no raw acknowledgement precedes completion, and expand the eventual citation.
Normalized-upload tests also expire an interrupted partial, reconstruct the
payload from the durable queue, and verify idempotent ingestion after a lost
acknowledgement. These tests use synthetic data.

## Upload staging budget

Start the primary with an explicit byte budget when upload staging needs a cap:

```sh
session-search --data-dir PRIMARY_DATA serve --credentials CREDENTIALS_FILE --max-staging-bytes 68719476736
```

The example permits 64 GiB of combined raw and normalized partials. Choose room
for the largest expected upload and concurrent clients. All server processes
writing the same data root must use the same setting. This is opt-in; the default
keeps the existing free-space check without adding a directory scan per chunk.

Admission holds a stable process-shared lock across a bounded directory scan and
durable append. It counts logical bytes in every regular `.part` file in both
transfer directories, conservatively including hardlinks and unrecognized names.
The scan includes lock files toward `--staging-scan-limit` (default 10,000,
maximum 1,000,000). A busy lock, exceeded byte/entry limit, or unsupported partial
file type returns retryable HTTP 503. Existing partials and client queues remain
intact. Completed uploads release their staging; an already complete partial can
finalize without allocating more upload bytes even after the budget is lowered.

If partial uploads fill the budget, they can stall one another before completion.
Review and raise the budget or use the explicit expiry workflow below; the server
does not discard pending bytes to make room. Retained lock files may eventually
require a larger scan limit. The scan runs per appended chunk, so raising its
limit can increase upload latency. No hard scan deadline is promised.

This cap covers upload partials only. Canonical history, archive chunk conversion,
lock-file disk allocation, local capture, backups, and unrelated processes still
need filesystem capacity. It is not a reservation of free space. Keep the capacity
lock inode intact, and do not mix configured writers with older or unconfigured
upload writers on the same primary.

## Manual transfer expiry

Plan first on the primary data host:

```sh
session-search --data-dir PRIMARY_DATA expire-transfers --older-than-days 7
session-search --data-dir PRIMARY_DATA expire-transfers --older-than-days 7 --apply
```

Choose the age explicitly; at least one day must be retained. Age is measured
from the partial's last write, not its last status lookup. An idle client may
need to retransmit expired staging from its durable queue. This policy applies
only to recognized raw and normalized upload partial names, not native sessions,
canonical evidence, chunk objects, completed transfer objects, or backup receipts.

The command takes the primary writer lock exclusively and without waiting;
active ingestion, capture, or indexing returns `deferred`. Fenced primaries and
snapshots/replicas are rejected. Each candidate also needs its existing regular
transfer lock, with no active reader, and unchanged inode, size, and modification
time. Symlinks, hardlinks, and special files are skipped. Raw recovery references
and existing object/recipe paths prevent expiry even if their content is corrupt.

The complete scan must fit `--scan-limit` (default 10,000 directory entries,
maximum 1,000,000, including lock and unrecognized files). Otherwise the run
defers before deleting anything; an operator can review a larger explicit limit.
Raw recovery references are streamed once from the catalog; the directory-entry
limit does not bound that read or establish a wall-clock deadline.
The result reports eligible, removed, and skipped counts and byte totals.
Removals are synced individually, and an interrupted run can be repeated.
No production expiry or scheduling is implied by the synthetic tests.

## Implementation references

Client staging is implemented in
[`capture/queue.py`](../src/session_search/capture/queue.py), server transfers in
[`storage/transfers.py`](../src/session_search/storage/transfers.py), replica
retention in [`storage/generations.py`](../src/session_search/storage/generations.py),
backup retirement in [`storage/backup_cycle.py`](../src/session_search/storage/backup_cycle.py),
and verification admission in
[`storage/verification_jobs.py`](../src/session_search/storage/verification_jobs.py).
The corresponding crash and retention tests use synthetic data; they do not
qualify deletion of personal native sessions.
