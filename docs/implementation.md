# Implementation progress

This checklist records delivered behavior, separately from the target architecture.

- [x] Standalone Python package and optional dependency groups.
- [x] Codex parser extraction with existing regression fixtures.
- [x] Streaming ownership scan and response parsing without retaining ignored raw payloads.
- [x] Transactional canonical revisions and lexical retrieval.
- [x] Shared immutable evidence records and compressed revision maps; unchanged text is not copied for each growing revision.
- [x] Immutable bounded context and CLI.
- [x] Durable upload queue and complete-record capture checkpoints.
- [x] Idle client queue compaction that preserves acknowledgements and capture checkpoints.
- [x] Hybrid retrieval and explicit local/remote embedding providers.
- [x] Process-shared background embedding admission and bounded retries during provider outages.
- [x] New embedding work selects chunks referenced by current session revisions, retaining older evidence and citations.
- [x] Three read-only MCP tools and authenticated HTTP transport.
- [x] Monotonic SSH credential propagation, exact acknowledgements, and independent retry timer templates.
- [x] Verified snapshot creation and atomic read-only replica activation.
- [x] Replica retention of current, previous, and actively pinned generations, including reader crash recovery.
- [x] Explicit search-only replicas, excluded from recovery/offload backup inputs.
- [x] Resumable SSH snapshot publication, unchanged-publication skipping, and five-minute timer templates.
- [x] Two-repository Restic restore verification and guarded manual offload primitives.
- [x] Persistent read-only recovery from an exact repository receipt, with overwrite and identity checks.
- [x] Offline core wheel installation and CLI isolation tests without optional packages or network access.
- [ ] Incremental parsing of appended records without reparsing the changed session.
- [x] Resumable raw-object transfer from lightweight clients.
- [x] Resumable large normalized payloads, isolated from raw archival, with bounded ingestion admission.
- [x] Verified legacy archive importer and immutable historical turn/event locator aliases.
- [x] Explicit first-upload checkpoint adoption for legacy imports, with concurrent-update conflict protection.
- [x] Cooperative local primary write fencing with active-writer detection and preserved read access.
- [x] Raw-prefix checkpoint reconciliation and forced recapture after client loss or an older primary restore.
- [ ] Background scheduling, global embedding admission control, and bounded maintenance retention.
- [ ] Live cross-host rollout, credential revocation propagation, and recovery automation.
- [ ] Offline installation with MCP/local inference extras, quality/performance evaluations, and migration.
- [x] Reproducible synthetic retrieval benchmark with dataset/model identity and explicit fallback detection.
- [ ] Provenance clearance, GitHub publication, and deployment.

Existing production storage remains separate. Offload tests use synthetic files and temporary backup repositories; no real session removal has been performed. Remote raw transfers are opt-in, chunked, and checksum-verified before revision acknowledgement. Thin-client offload coordination still needs to be connected to server-side recovery verification.

Background indexers share a lock on the authoritative catalog; standby catalogs cannot index. Query embeddings bypass this background lock. This bounds Session Search background ingestion across primary worker processes, but does not control unrelated applications sharing the model endpoint. Remote artifact identity relies on explicit configuration plus the returned model name; an unchanged alias is not cryptographic proof of the served artifact.

Parser regression fixtures cover the extracted Codex behavior. [Migration and recovery](migration.md) tests cover historical locator preservation, tombstones, corrupt archives, first-upload checkpoint adoption, lost client checkpoints, and older-primary recovery. Raw-prefix reconciliation covers absent heads or proven byte ancestry; divergent histories remain queued for explicit review. Whole-corpus retrieval parity still needs evaluation; do not infer it from the current unit tests.

The development catalog is schema version 2. The earlier prototype schema is deliberately rejected rather than silently modified; no production Session Search catalog has been migrated. Snapshot verification checks canonical revision digests in addition to database and raw-object integrity.
