# Reliability contract

**Implementation requirements, not current guarantees.** These checks must pass before the corresponding feature is enabled.

## Failure behavior

| Condition | Required behavior |
| --- | --- |
| Primary unreachable | Retry eligible searches on the standby; retain pending uploads on clients. |
| Both search servers unreachable | Report unavailable, including queued-capture status. Do not report an empty search result. |
| Embedder unavailable or overloaded | Use bounded-deadline keyword fallback; continue capture, lexical indexing, replication, and backups. |
| Standby behind | Report its coverage and age. If a cited revision is absent, report unavailable context rather than substituting another revision. |
| Upload interrupted | Retry idempotently; acknowledge only durable content and metadata. |
| Disk nearly full | Apply visible backpressure; never silently discard pending history. |
| Local model missing | Provide keyword search and actionable status without a network download. |

## Durability and retention

Track upload acknowledgement, lexical coverage, semantic coverage, replication, and backup completion separately. A healthy repository or recent backup time does not prove that a particular session revision is recoverable.

Replicate every five minutes and after index publication, coalescing overlapping work. Stage and verify all referenced evidence before atomically activating a replica generation. Pin active readers; retain current and previous generations. Their retirement must not remove canonical evidence needed by old citations.

Back up every six hours using a consistent database snapshot and referenced objects. Offload receipts identify the raw digest, normalized revision, and containing snapshot on each required destination. Restore and hash verification must succeed for those exact contents.

Replication and backup intervals are schedules, not guaranteed recovery windows. Outages can extend both. Status reports the actual recoverable coverage; recently acknowledged but unreplicated data may be lost with the primary and its originating client.

Permanent primary loss requires an operator to fence the old writer, restore validated state, and start one replacement writer. Surviving clients replay retained work idempotently. Automatic search failover does not promote a writer.

Bound downloaded context caches, reclaim completed transfer staging, and retire obsolete unpinned index generations. Pending work is retained with backpressure. Canonical evidence deletion and backup pruning remain manual in v1. Backup credentials must be recoverable independently of the primary.

## Retrieval correctness

- Preserve exact literal and phrase matching, role filters, evidence-level time bounds, and project/session/device filters. Never silently widen a query after a miss.
- Exclude the current Codex thread tree by default to avoid finding the question being asked. Keep the matched event in bounded context.
- Preserve query instructions already used by the existing tool. Version model artifact, dimensions, preprocessing, normalization, and query instructions; an endpoint alias alone is not a compatibility check.
- Return bounded evidence with explicit truncation and coverage. Scores are ranking signals, not confidence probabilities.

## Migration and release gates

Import into a separate Session Search store and prevent legacy publishers from writing to it. Track migration per device, including offline devices that reconnect later. Missing source files never become shared deletions. Rollback can use the old installation without granting it write access to Session Search.

| Gate | Required evidence |
| --- | --- |
| Capture | Crash tests around content commit, metadata commit, and acknowledgement; retries lose no acknowledged revision and create no duplicates. |
| Search parity | Existing literal, role, time, filter, and matched-context cases pass through CLI and MCP. |
| Model outage | Newly captured evidence is keyword-searchable; semantic coverage catches up without duplicate records. |
| Interactive capacity | Sustained background ingestion does not monopolize embedding slots; deadlines and cancellation work. |
| Replica safety | Interrupted transfers expose only a complete old or new generation; immutable citations never resolve to different evidence. |
| Recovery | Restore exact session revisions from both destinations; recover a replacement primary with one writer. |
| Offload | Changed files, active writers, missing receipts, or failed restores block removal. Reconnecting legacy clients cannot delete retained history. |
| Local isolation | With networking disabled, capture and search work; no implicit downloads or remote fallback occur. |
| Storage | Repeated indexing and interrupted uploads reclaim abandoned artifacts without deleting pending work or cited evidence. |

Measure Recall@10, MRR@10, query latency, peak memory, ingestion throughput, and storage reclaimed against a fixed baseline. Use synthetic fixtures for public reproduction and a private representative query set for personal relevance. Report measured tradeoffs before changing embedding models or adding retrieval features.

## Replica capacity

`replicate` checks local space before creating a pending search snapshot and asks
the standby to run `replica-capacity` before starting rsync. Both hosts must run a
version that supports this command. The default free-space reserve is 2 GiB;
`replicate --reserve-bytes BYTES` changes it for both checks. Local admission allows
twice the current catalog and SQLite sidecar sizes plus the reserve. Remote
admission allows the full incoming snapshot size plus the reserve, without
subtracting an existing partial transfer because rsync may need a new temporary
copy. Current, previous, and pinned generations remain counted as occupied space.

Insufficient capacity returns `status: deferred` with `stage: source_snapshot` or
`stage: standby_transfer`, byte counts, and no new acknowledgement. A pending
snapshot is retained for retry. This preflight is a point-in-time check, not an
allocation reservation: concurrent writers and other applications can consume
space after it passes. Keep disk monitoring and capacity planning in place;
preflight alone does not make an undersized standby safe for routine replication
or full raw backups.
