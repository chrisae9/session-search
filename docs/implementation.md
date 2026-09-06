# Implementation progress

This checklist records delivered behavior, separately from the target architecture.

- [x] Standalone Python package and optional dependency groups.
- [x] Codex parser extraction with existing regression fixtures.
- [x] Streaming ownership scan and response parsing without retaining ignored raw payloads.
- [x] Transactional canonical revisions and lexical retrieval.
- [x] Shared immutable evidence records and compressed revision maps; unchanged text is not copied for each growing revision.
- [x] Immutable bounded context and CLI.
- [x] Durable upload queue and complete-record capture checkpoints.
- [x] Hybrid retrieval and explicit local/remote embedding providers.
- [x] Process-shared background embedding admission and bounded retries during provider outages.
- [x] Three read-only MCP tools and authenticated HTTP transport.
- [x] Verified snapshot creation and atomic read-only replica activation.
- [x] Replica retention of current, previous, and actively pinned generations, including reader crash recovery.
- [x] Explicit search-only replicas, excluded from recovery/offload backup inputs.
- [x] Resumable SSH snapshot publication, unchanged-publication skipping, and five-minute timer templates.
- [x] Two-repository Restic restore verification and guarded manual offload primitives.
- [ ] Incremental parsing of appended records without reparsing the changed session.
- [x] Resumable raw-object transfer from lightweight clients.
- [x] Resumable large normalized payloads, isolated from raw archival, with bounded ingestion admission.
- [x] Verified legacy archive importer and immutable historical turn/event locator aliases.
- [x] Explicit first-upload checkpoint adoption for legacy imports, with concurrent-update conflict protection.
- [ ] Background scheduling, global embedding admission control, and bounded maintenance retention.
- [ ] Live cross-host rollout, credential revocation propagation, and recovery automation.
- [ ] Offline installation, quality/performance evaluations, and migration.
- [ ] Provenance clearance, GitHub publication, and deployment.

Existing production storage remains separate. Offload tests use synthetic files and temporary backup repositories; no real session removal has been performed. Remote raw transfers are opt-in, chunked, and checksum-verified before revision acknowledgement. Thin-client offload coordination still needs to be connected to server-side recovery verification.

Background indexers share a lock on the authoritative catalog; standby catalogs cannot index. Query embeddings bypass this background lock. This bounds Session Search background ingestion across primary worker processes, but does not control unrelated applications sharing the model endpoint. Remote artifact identity relies on explicit configuration plus the returned model name; an unchanged alias is not cryptographic proof of the served artifact.

Parser regression fixtures cover the extracted Codex behavior. [Legacy migration](migration.md) tests cover historical locator preservation after subsequent capture, tombstones, conflicting or corrupt archives, and first-upload checkpoint adoption. Whole-corpus retrieval parity and general recovery of lost client checkpoints still need migration/evaluation tests. Do not infer those guarantees from the current unit tests.

The development catalog is schema version 2. The earlier prototype schema is deliberately rejected rather than silently modified; no production Session Search catalog has been migrated. Snapshot verification checks canonical revision digests in addition to database and raw-object integrity.
