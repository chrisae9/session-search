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
| Replica generations | Activation and `prune-replica` retain current, previous, and actively pinned generations. Reader process exit releases its pin. Other staging directories are outside this cleanup. |
| Recovery snapshot outbox | One pending snapshot is retried until every required destination is restore-verified. The complete receipt is saved before retiring staging; a later cycle can finish interrupted retirement using that receipt. |
| Verification jobs | Admission is serialized, with at most 32 managed job records. Completed records expire after 900 seconds; subsequent new-job admission performs cleanup. Worker-owned scratch is reclaimed only after its ownership checks pass. Expiry is not a periodic deletion timer. |

These mechanisms preserve canonical revisions, cited evidence, and pending work.
They do not imply a fixed total disk budget: pending uploads, active reader pins,
and growing history can legitimately require more space.

## Retained state and remaining work

| State | Current behavior |
| --- | --- |
| Abandoned server uploads | Partial files remain resumable; no age-based expiry or server-wide staging quota is implemented. |
| Server transfer lock files | Persist after transfers. Do not unlink them while readers or writers can hold open descriptors: a new file at the same path would create a different lock and break mutual exclusion. |
| Server or local archive chunk temporaries | Ordinary exception cleanup removes them, but a killed process can leave `.chunk-*` files. The client-only cleanup above must not be applied to a canonical catalog. No server sweep is implemented. |
| Interrupted backup snapshot construction | `.snapshot-*` staging defers the next cycle for operator inspection. It is not automatically deleted or treated as a completed backup. |
| Completed backup receipts | Retained per snapshot; no receipt retention policy is implemented. A saved receipt records earlier verification, not present repository availability. |
| Canonical objects and backup repositories | No automatic archival deletion or Restic pruning. Source disappearance does not authorize deletion. |

Future server cleanup needs a separate ownership and concurrency protocol,
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
This establishes a retry prerequisite; it does not authorize expiry, prove that
a particular partial is disposable, or qualify normalized-transfer maintenance.

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
