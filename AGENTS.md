# Session Search development

This is a standalone Python application. Keep live session data, model files, credentials, backup repositories, and runtime state outside the source tree. Use synthetic fixtures for committed tests.

Read `docs/implementation.md` before extending unfinished features. `docs/architecture.md` describes the target design; it is not evidence that a behavior is already deployed.

Run `uv sync --group dev --extra server --extra mcp --extra embeddings`, then `uv run pytest` and `uv run ruff check src tests`. Restic must be installed for the real backup/offload integration test; a skipped backup test does not establish recovery safety. The local inference extra is provisioned separately.

Preserve these invariants:

- Source disappearance never deletes canonical history. Legacy publishers cannot write this store.
- Citations address immutable revisions and events. Context never substitutes the latest revision.
- Query filters apply to matching evidence, including semantic candidates.
- Capture, lexical publication, replication, and backups do not depend on embeddings.
- Lightweight clients queue writes only for the primary. Standbys remain read-only.
- Local-only mode has no implicit model downloads or remote fallback.
- Offload requires exact restore verification from both required destinations, unchanged source identity and hashes, and stopped Codex writers. Never exercise removal on personal sessions as a test.
- Never print credentials or transcript-containing exceptions to logs.

Keep optional dependencies behind their corresponding feature boundary. Do not add a second production language, a UI, graph memory, or additional infrastructure without a measured need and a scope decision.

Keep committed examples, diagrams and documentation generic. Never include real deployment hostnames, private addresses, account paths, or home-network resource names. Before public release, audit the intended Git history as well as the current files and rendered assets.
