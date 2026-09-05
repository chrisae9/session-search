# Implementation progress

This checklist records delivered behavior, separately from the target architecture.

- [x] Standalone Python package and optional dependency groups.
- [x] Codex parser extraction with existing regression fixtures.
- [x] Transactional canonical revisions and lexical retrieval.
- [x] Shared immutable evidence records and compressed revision maps; unchanged text is not copied for each growing revision.
- [x] Immutable bounded context and CLI.
- [x] Durable upload queue and complete-record capture checkpoints.
- [x] Hybrid retrieval and explicit local/remote embedding providers.
- [x] Three read-only MCP tools and authenticated HTTP transport.
- [x] Verified snapshot creation and atomic read-only replica activation.
- [x] Replica retention of current, previous, and actively pinned generations, including reader crash recovery.
- [x] Explicit search-only replicas, excluded from recovery/offload backup inputs.
- [x] Two-repository Restic restore verification and guarded manual offload primitives.
- [ ] Incremental parsing of appended records without reparsing the changed session.
- [x] Resumable raw-object transfer from lightweight clients.
- [x] Resumable large normalized payloads, isolated from raw archival, with bounded ingestion admission.
- [x] Verified legacy archive importer and immutable historical turn/event locator aliases.
- [ ] Background scheduling, global embedding admission control, and bounded maintenance retention.
- [ ] Cross-host replica transport, credential revocation propagation, and recovery automation.
- [ ] Offline installation, quality/performance evaluations, and migration.
- [ ] Provenance clearance, GitHub publication, and deployment.

Existing production storage remains separate. Offload tests use synthetic files and temporary backup repositories; no real session removal has been performed. Remote raw transfers are opt-in, chunked, and checksum-verified before revision acknowledgement. Thin-client offload coordination still needs to be connected to server-side recovery verification.

The present semantic provider limit applies per worker, not across hosts or processes. This is not yet the deployment-wide admission control required for release. Remote artifact identity relies on explicit configuration plus the returned model name; an unchanged alias is not cryptographic proof of the served artifact.

Parser regression fixtures cover the extracted Codex behavior. [Legacy migration](migration.md) tests cover historical locator preservation after subsequent capture, tombstones, and conflicting or corrupt archives. Whole-corpus retrieval parity and client checkpoint reconciliation still need migration/evaluation tests. Do not infer those guarantees from the current unit tests.

The development catalog is schema version 2. The earlier prototype schema is deliberately rejected rather than silently modified; no production Session Search catalog has been migrated. Snapshot verification checks canonical revision digests in addition to database and raw-object integrity.
