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

When a native capture replaces a Codex import, its imported project slug remains
a search alias for that session's native project path. Both names work with
keyword and hybrid project filters. The alias follows later revisions at the same
path, but does not match a different project path or another session. Old revision
identities remain unchanged, and verified snapshots retain the alias mapping.
Deploy an alias-aware server before the first native upload; this does not
retroactively infer aliases for imports already replaced by older versions.

## Recovering client checkpoints

For native Codex sessions with raw archival enabled, `flush --reconcile-raw-prefixes` can repair an oldest queued revision's expected server revision. It obtains bounded metadata directly from the primary and hashes the corresponding prefix of the client's staged raw file. Reconciliation proceeds only when the server session is absent, or its archived raw bytes are an exact prefix of the client file. The final upload still compares the exact server revision at commit, preserving conflicts with intervening writers. Later queued revisions keep their ordering.

If a primary has been restored from an older backup, surviving client checkpoints can incorrectly consider newer local files already captured. After completing operator-controlled primary recovery, use `capture --archive-raw --force` to queue those local files again, then `flush --reconcile-raw-prefixes` until the queue drains. Forced capture rereads and stages the discovered files; allow space for those transfer copies. Ordinary capture continues to skip unchanged files.

Divergent or shorter raw files, unavailable staging, and existing heads without archived raw evidence remain conflicts. Inspect these cases explicitly; the reconciliation option does not force an overwrite or invent ancestry. It does not replace primary fencing, recover missing native files, or promote a read-only restored snapshot.

## Fence a primary before replacement

Stop capture, indexing, upload, and replication writers on the old primary, then run
`session-search --data-dir DATA_DIR fence-primary` on that host. A `busy` result
(exit 2) means a participating catalog or transfer writer still holds a lease;
stop it and retry. A `fenced` receipt (exit 0) records the publication and prevents
new participating catalog and transfer writes. Search and immutable context remain
available. Repeating the command returns the same receipt when the publication
has not changed. There is no automatic unfence command.

This is a cooperative local fence, not distributed promotion. Older binaries and
external database writers do not honor its lock. Stop those processes and isolate
the old host from client traffic before starting a replacement primary. Do not
remove the fence marker to reuse an old primary.

## Prepare and switch to a replacement primary

Restore an exact recovery snapshot with `restore-backup` (see [backup and restore usage](usage.md)),
then verify it and retain its snapshot digest. A search-only replica cannot serve
as recovery input. With the old primary isolated, run:

```sh
session-search prepare-primary ./recovered-snapshot ./replacement-store \
  --snapshot-id SNAPSHOT_DIGEST --old-primary-isolated
session-search --data-dir ./replacement-store status
```

The isolation flag is an operator assertion, not a network or host check. The
command refuses existing destinations and mismatched digests, verifies copied
catalog and raw evidence, and publishes the new directory only after preparation
succeeds. Allow space for a full copy. The source remains a read-only recovery
snapshot. `recovery.json` records the source snapshot and both publication
identities; `recovery-source.json` retains its original manifest for audit.

The replacement preserves revisions, citations, raw evidence, and upload receipts,
but starts a new publication identity. Existing replicas deliberately reject that
identity. Prepare a new replica directory from the replacement, verify its contents,
and point the standby service at it before restoring client failover. Do not delete
the old replica until its replacement has been verified.

Restore credentials separately from their authoritative registry backup or issue
fresh device credentials; snapshot preparation does not copy authentication or
service configuration. Start the primary service with the replacement directory,
verify authenticated search/context and a synthetic upload, then switch clients
to its endpoint. Reconcile surviving client queues using the checkpoint recovery
procedure above. Keep the old host isolated throughout. A later primary failure
requires another explicit recovery; this procedure does not automatically promote
a standby or eliminate the backup recovery-point gap.
