# Implementation progress

This checklist records delivered behavior, separately from the target architecture.

- [x] Standalone Python package and optional dependency groups.
- [x] Codex parser extraction with existing regression fixtures.
- [x] Transactional canonical revisions and lexical retrieval.
- [x] Immutable bounded context and CLI.
- [x] Durable upload queue and complete-record capture checkpoints.
- [x] Hybrid retrieval and explicit local/remote embedding providers.
- [x] Three read-only MCP tools and authenticated HTTP transport.
- [x] Verified snapshot creation and atomic read-only replica activation.
- [x] Two-repository Restic restore verification and guarded manual offload primitives.
- [ ] Incremental parsing of appended records without reparsing the changed session.
- [ ] Resumable raw-object transfer from lightweight clients.
- [ ] Background scheduling, global embedding admission control, and bounded maintenance retention.
- [ ] Cross-host replica transport, credential revocation propagation, and recovery automation.
- [ ] Offline installation, quality/performance evaluations, and migration.
- [ ] Provenance clearance, GitHub publication, and deployment.

Existing production storage remains separate. Offload tests use synthetic files and temporary backup repositories; no real session removal has been performed. Remote raw archival is explicitly rejected until the transfer protocol is implemented.

The present semantic provider limit applies per worker, not across hosts or processes. This is not yet the deployment-wide admission control required for release. Remote artifact identity relies on explicit configuration plus the returned model name; an unchanged alias is not cryptographic proof of the served artifact.

Parser regression fixtures cover the extracted Codex behavior. Historical turn-number compatibility and whole-corpus retrieval parity still need migration/evaluation tests. Do not infer those guarantees from the current unit tests.
