# Importing the legacy archive

`import-legacy` reads a version 1 immutable archive into a new, separate catalog. It verifies manifest and object checksums, canonical records, and revision identities before publishing the destination. Existing destinations are refused; the source is never modified.

Run this on the destination data host, with space for a temporary merge database and the resulting catalog:

```sh
session-search --data-dir ./imported-store import-legacy ./legacy-archive
session-search --data-dir ./imported-store status
session-search --data-dir ./imported-store search 'known historical phrase' --literal
```

Use a stable archive snapshot. If its manifest frontier changes during import, the command discards its staging directory and fails instead of publishing a partial migration. The merge runs on disk; catalog construction still materializes one session at a time. Large sessions therefore affect peak memory.

The importer selects each record's highest revision, rejects conflicting revisions, and respects tombstones. Historical adapters remain searchable. Native Codex session IDs are preserved; other adapters receive a source prefix to prevent collisions. Imported text passes through the same secret redaction used by capture.

If an audited archive contains conflicting *superseded* revisions, `--allow-superseded-conflicts` permits import only when each affected record has a unique newer revision. This reproduces the original archive's latest-revision selection and reports `superseded_conflicts` in the result. Conflicting current revisions always prevent publication. Keep the original archive for historical audit; this option does not repair or copy its older variants.

Old event and turn locators can be expanded through the existing context interface:

```sh
session-search --data-dir ./imported-store context \
  '[{"legacy_locator":"codex:example-session:turn:7"}]'
```

These aliases point to immutable imported evidence, so later capture does not redirect them to rewritten history. Unavailable locators are reported explicitly. Requests resolving to more than 100 events must be narrowed to individual event locators.

This imports normalized search evidence, not raw session files or existing embedding vectors. Rebuild semantic coverage with the explicitly configured provider. Raw recovery requires separate opt-in capture and verified backups. Do not switch production clients or offload source files based solely on a successful import: validate retrieval parity, reconcile client revision checkpoints, and verify recovery first.
