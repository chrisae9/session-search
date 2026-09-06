# Implementation progress

This checklist records delivered behavior, separately from the target architecture.

- [x] Standalone Python package and optional dependency groups.
- [x] Codex parser extraction with existing regression fixtures.
- [x] Streaming ownership scan and response parsing without retaining ignored raw payloads.
- [x] Bounded byte staging of complete records, including large partial tails, with strict validation during parsing.
- [x] Transactional canonical revisions and lexical retrieval.
- [x] Persistent active-evidence lookup index avoids repeated temporary-index construction for keyword queries.
- [x] Optional covering metadata index reduces transcript-page reads during keyword ranking and semantic candidate filtering, with fallback for catalogs without the index.
- [x] Opt-in atomic literal substring index with transactional maintenance and scan fallback.
- [x] Pilot primary literal-index deployment with authenticated retrieval, citation expansion, and standby fallback checks.
- [x] Frozen historical-catalog literal qualification: 32 full-response comparisons against the scan path across query forms and filters.
- [ ] Literal-index replica rollout and qualification on additional machines.
- [x] Shared immutable evidence records and compressed revision maps; unchanged text is not copied for each growing revision.
- [x] Immutable bounded context and CLI.
- [x] Durable upload queue and complete-record capture checkpoints.
- [x] Per-run capture byte admission and data-filesystem staging capacity preflights.
- [x] Idle client queue compaction that preserves acknowledgements and capture checkpoints.
- [x] Hybrid retrieval and explicit local/remote embedding providers.
- [x] Process-shared background embedding admission and bounded retries during provider outages.
- [x] Two-minute pilot embedding contention check: 120 foreground queries and 809 background requests completed without failures.
- [x] New embedding work selects chunks referenced by current session revisions, retaining older evidence and citations.
- [x] Three read-only MCP tools and authenticated HTTP transport.
- [x] Configurable server search admission with retryable overload and capacity retained after caller cancellation until worker completion.
- [x] Monotonic SSH credential propagation, exact acknowledgements, and independent retry timer templates.
- [x] Verified snapshot creation and atomic read-only replica activation.
- [x] Replica retention of current, previous, and actively pinned generations, including reader crash recovery.
- [x] Explicit search-only replicas, excluded from recovery/offload backup inputs.
- [x] Resumable SSH snapshot publication, unchanged-publication skipping, and five-minute timer templates.
- [x] Source and standby capacity preflights defer replica work while preserving pending snapshots and the current replica.
- [x] Two-repository Restic restore verification and guarded manual offload primitives.
- [x] Device-scoped asynchronous verification, durable client raw acknowledgements, and reviewed thin-client offload with fresh proof and native-file rechecks.
- [x] Producer-scoped recovery of missing client raw acknowledgements without rebasing or re-uploading files.
- [ ] Live thin-client offload qualification.
- [x] Persistent read-only recovery from an exact repository receipt, with overwrite and identity checks.
- [x] Offline core wheel installation and CLI isolation tests without optional packages or network access.
- [x] Offline MCP dependency-bundle installation and real stdio retrieval with server networking blocked.
- [x] Python 3.11 qualification on Linux x86_64 and Apple Silicon macOS: full tests, lint, offline core installation, and offline MCP stdio operation.
- [x] Incremental parser engine with complete-prefix verification, original line boundaries, and ownership-change fallback.
- [x] Bounded atomic parser checkpoints and opt-in capture/sync integration with full-parse fallback.
- [x] Mac persistent-cache capture/queue qualification, scheduled pilot reuse, and MCP status verification.
- [x] Persistent-checkpoint benchmark including load/save costs, with exact synthetic append parity on macOS and Linux.
- [ ] Broader persistent-cache performance qualification across clients.
- [x] Chunk-backed catalog acknowledgement, version 2 snapshots, dual Restic restoration, and replacement-primary preparation.
- [x] Opt-in server raw-upload chunking with the existing client protocol, shared-prefix reuse, and publication retry recovery.
- [x] Opt-in local-only capture into shared raw chunks.
- [x] Opt-in shared client staging with streaming resume, prefix reconciliation, and cleanup rooted in every pending upload.
- [x] Pilot deployment of shared raw storage, verified live uploads, and mixed-format backup restoration.
- [x] Resumable raw-object transfer from lightweight clients.
- [x] Upload completion publishes the verified staging inode without allocating a second full raw or normalized file.
- [x] Upload status retries reclaim verified completed staging after process crashes, respecting writer fencing and per-transfer locks.
- [x] Resumable large normalized payloads, isolated from raw archival, with bounded ingestion admission.
- [x] Verified legacy archive importer and immutable historical turn/event locator aliases.
- [x] Explicit first-upload checkpoint adoption for legacy imports, with concurrent-update conflict protection.
- [x] Explicit replacement-primary preparation from exact recovery snapshots, with a new publication identity and preserved evidence.
- [x] Cooperative local primary write fencing with active-writer detection and preserved read access.
- [x] Raw-prefix checkpoint reconciliation and forced recapture after client loss or an older primary restore.
- [x] Bounded sync cycles with backlog admission, overlap prevention, and Linux/macOS scheduling templates.
- [x] Pilot macOS recurring capture with successful scheduled uploads and timestamped status.
- [ ] Global embedding admission across independent catalogs or unrelated endpoint clients.
- [x] Storage-maintenance ownership audit distinguishing automatic cleanup from retained state; see [storage maintenance](storage-maintenance.md).
- [ ] Bounded server staging and archival maintenance retention, with separate recovery-safe receipt retention.
- [x] Pilot authenticated primary/standby deployment, actual MCP search failover, and credential revocation propagation.
- [ ] Additional client rollout, full independent backup coverage, and live replacement-primary rehearsal.
- [x] Retryable backup cycles, per-destination restore receipts, capacity checks, status, and six-hour timer template.
- [ ] Live recurring backup rollout after full independent destination capacity is available.
- [x] Pilot historical import with legacy citation resolution checks.
- [x] Apple Silicon macOS 26 native-inference bundle installation and semantic engine check with OS networking denied.
- [x] Fresh-process local semantic MCP, concurrent literal query, context, and status with OS networking denied.
- [x] Linux x86_64 CPU native-engine and MCP fallback/recovery checks with OS networking denied; cold semantic queries can exceed the default deadline on constrained CPUs.
- [x] Isolated native MCP model process, single-request inference admission, and cancellation-aware bounded MCP dispatch.
- [ ] Additional native-runtime platforms and whole-corpus quality/performance qualification.
- [x] Reproducible synthetic retrieval benchmark with dataset/model identity and explicit fallback detection.
- [x] Read-only fusion-policy comparison and separate conceptual/identifier regression cases; see [retrieval evaluation](retrieval-evaluation.md).
- [ ] Provenance clearance, GitHub publication, and deployment.

Legacy storage remains separate from the imported Session Search catalog. Offload tests use synthetic files and temporary backup repositories; no real session removal has been performed. Remote raw transfers are opt-in, chunked, and checksum-verified before revision acknowledgement. Thin-client offload uses fresh server-side recovery verification; live rollout remains gated on full independent backup coverage.

Background indexers share a lock on the authoritative catalog; standby catalogs cannot index. Query embeddings bypass this background lock. This bounds Session Search background ingestion across primary worker processes, but does not control unrelated applications sharing the model endpoint. Remote artifact identity relies on explicit configuration plus the returned model name; an unchanged alias is not cryptographic proof of the served artifact.

Parser regression fixtures cover the extracted Codex behavior. [Migration and recovery](migration.md) tests cover historical locator preservation, tombstones, corrupt archives, first-upload checkpoint adoption, lost client checkpoints, and older-primary recovery. Raw-prefix reconciliation covers absent heads or proven byte ancestry; divergent histories remain queued for explicit review. Whole-corpus retrieval parity still needs evaluation; do not infer it from the current unit tests.

The development catalog is schema version 2. The earlier prototype schema is deliberately rejected rather than silently modified. Historical archives are imported through the explicit importer; this is distinct from a schema upgrade. Snapshot verification checks canonical revision digests in addition to database and raw-object integrity.
