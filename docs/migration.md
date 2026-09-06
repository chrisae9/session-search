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

This imports normalized search evidence, not raw session files or existing embedding vectors. Rebuild semantic coverage with the explicitly configured provider. Raw recovery requires separate opt-in capture and verified backups. Do not offload source files based solely on a successful import: validate retrieval parity and verify recovery first.

After a client captures its native Codex files into its durable queue, `flush --bootstrap-imports` can adopt matching legacy import heads from the primary. This explicit migration option only changes a first upload's expected revision when the primary still identifies that session as a legacy Codex import. The server checks that exact revision again when committing. Existing native heads and intervening updates remain conflicts; the option never forces an overwrite. Later queued revisions retain their original ordering, and imported citations continue to address their original evidence.

## Recovering client checkpoints

For native Codex sessions with raw archival enabled, `flush --reconcile-raw-prefixes` can repair an oldest queued revision's expected server revision. It obtains bounded metadata directly from the primary and hashes the corresponding prefix of the client's staged raw file. Reconciliation proceeds only when the server session is absent, or its archived raw bytes are an exact prefix of the client file. The final upload still compares the exact server revision at commit, preserving conflicts with intervening writers. Later queued revisions keep their ordering.

If a primary has been restored from an older backup, surviving client checkpoints can incorrectly consider newer local files already captured. After completing operator-controlled primary recovery, use `capture --archive-raw --force` to queue those local files again, then `flush --reconcile-raw-prefixes` until the queue drains. Forced capture rereads and stages the discovered files; allow space for those transfer copies. Ordinary capture continues to skip unchanged files.

Divergent or shorter raw files, unavailable staging, and existing heads without archived raw evidence remain conflicts. Inspect these cases explicitly; the reconciliation option does not force an overwrite or invent ancestry. It does not replace primary fencing, recover missing native files, or promote a read-only restored snapshot.
