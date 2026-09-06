# Data-host services

The `systemd/session-search-server.service` user unit runs the authenticated API on loopback. Install it under the user's systemd unit directory, expose the selected release as `~/.local/bin/session-search`, and provide a private `~/.config/session-search-v1/server.env`:

```text
SESSION_SEARCH_DATA=/absolute/data/directory
SESSION_SEARCH_CREDENTIALS=/absolute/device-registry.json
SESSION_SEARCH_PORT=8765
SESSION_SEARCH_SERVER_OPTIONS=
```

On a standby, set `SESSION_SEARCH_SERVER_OPTIONS=--readonly` and use the replica directory. Add `SESSION_SEARCH_EMBEDDING_OPTIONS` only when configuring an explicitly provisioned model. For a remote model it contains `--embedding-config /absolute/model-config.json --allow-remote-embeddings`. Keep bearer tokens in private credential files, never in service arguments.

Verify the service locally, then put the loopback endpoint behind the host's trusted HTTPS proxy. The proxy must remain reachable only by authorized clients; requests still require a valid device token. Start the unit with `systemctl --user enable --now session-search-server.service` after checking its configuration.

## Recurring replication

Install Session Search with the server extra on both data hosts. The primary needs `ssh` and `rsync`, with noninteractive access to the standby's SSH alias. Both hosts must use the same release. Keep the primary and standby data directories separate from legacy archive and index paths.

The primary publishes a verified search-only snapshot with:

```sh
session-search --data-dir PRIMARY_DATA replicate \
  --outbox OUTBOX_DIRECTORY --host STANDBY_SSH_ALIAS \
  --remote-data /absolute/standby/data \
  --remote-executable /absolute/standby/bin/session-search
```

Each destination needs its own outbox. Failed work keeps the same snapshot for retry. Concurrent invocations coalesce, and successful receipts let subsequent runs skip an unchanged catalog. The standby verifies the complete received snapshot before activation and then removes its incoming transfer copy. Publication identities prevent an older state or a different primary from replacing the current replica automatically.

Primary status distinguishes an acknowledged publication from newer data still waiting for replication. An acknowledgement records a completed verification; it is not a live health probe. Standby status identifies its snapshot and creation time, so agents can report coverage when using failover.

For Linux user services, install the two files from `systemd/` under the user's systemd unit directory. Make the installed executable available as `~/.local/bin/session-search`, and create a private `~/.config/session-search-v1/replication.env` containing absolute paths:

```text
SESSION_SEARCH_DATA=/absolute/primary/data
SESSION_SEARCH_OUTBOX=/absolute/primary/replication-outbox
SESSION_SEARCH_STANDBY_HOST=standby-ssh-alias
SESSION_SEARCH_STANDBY_DATA=/absolute/standby/data
SESSION_SEARCH_STANDBY_EXECUTABLE=/absolute/standby/bin/session-search
```

Verify one manual replication before enabling its timer:

```sh
systemctl --user daemon-reload
systemctl --user start session-search-replicate.service
systemctl --user status session-search-replicate.service
systemctl --user enable --now session-search-replicate.timer
```

The timer runs every five minutes. A publication pipeline can also start the same service after capture or embedding publication; overlapping starts do not create additional transfers. User services require a running user service manager, including after reboot. Provision that through the host's normal administration workflow.

This timer only replicates searchable evidence. Credential propagation, capture scheduling, and six-hour recovery backups need separate configuration; it does not establish raw-file recovery coverage.

## Background semantic indexing

The primary can install `session-search-embed.service` and its timer alongside replication. Provide a private `embedding.env` containing `SESSION_SEARCH_DATA`, `SESSION_SEARCH_EMBEDDING_CONFIG`, and `SESSION_SEARCH_EMBEDDING_BATCH` (1–10000). The model configuration must identify the explicitly provisioned remote embedding service. This data-host unit does not download or start a model.

Each run indexes a bounded batch, then requests replication. The timer waits 30 seconds between completed runs. A process-shared catalog lock permits only one background indexer at a time; query embeddings bypass it. Three consecutive provider failures end the batch early, leaving the remaining evidence pending. Standbys cannot run this worker. For local-only installations, use the local provider directly without this remote-model service unit.

## Recurring capture

`sync` runs one capture cycle under a nonblocking process lock. Remote mode flushes
first, then admits capture within `--max-bytes` and the remaining
`--max-pending-bytes` allowance, and flushes again. The backlog calculation counts
logical raw bytes without a chunk-sharing discount, or serialized payload bytes
for normalized-only uploads. It is an admission threshold, not a filesystem quota;
normalized payload expansion and independent manual capture can exceed it. The
capture free-space preflight remains active. An unavailable primary retains queued
work and its retry schedule; the standby never receives writes.

Choose a capture budget that accommodates the largest session you need to ingest.
A file above that budget stays deferred on every run until the budget is raised.
`sync` does not embed, replicate, back up, or remove native files. Inspect its JSON
coverage and queue results during a manual run before installing a scheduler.
Each completed cycle atomically records its timestamp and result in the private
`sync-status.json` file in its data directory; overlapping attempts do not replace
the last completed result.

Linux clients can install `systemd/session-search-sync.service` and its timer.
Create a private `sync.env` beside the other configuration files:

```text
SESSION_SEARCH_DATA=/absolute/client/data
SESSION_SEARCH_PRIMARY=https://primary.example
SESSION_SEARCH_TOKEN_FILE=/absolute/config/device.token
SESSION_SEARCH_CODEX_HOME=/absolute/codex/home
SESSION_SEARCH_PRODUCER=unique-device-id
SESSION_SEARCH_CAPTURE_BYTES=8589934592
SESSION_SEARCH_BACKLOG_BYTES=17179869184
SESSION_SEARCH_RESERVE_BYTES=2147483648
```

After the manual cycle succeeds, reload the user service manager and enable
`session-search-sync.timer`. It waits five minutes after each completed run.

For macOS, customize every path, endpoint, device identity, and byte allowance in
`launchd/session-search-sync.plist`, then place it in the user's `Library/LaunchAgents`
directory. Validate it with `plutil -lint` before loading it with `launchctl bootstrap`
into the user's GUI domain. The template runs at load and every five minutes while
the user agent is loaded. Keep the token in its private file, never in the plist.

For a local-only computer, omit `--primary`, `--token-file`, and
`--max-pending-bytes` from the scheduled command. `sync` then captures into its local
catalog without network operations. A local catalog and a client upload queue must
use different data directories.


## Recurring recovery backups

Initialize at least two independent Restic repositories explicitly, then configure
them using the repository file described in [usage](../docs/usage.md). Run on the
authoritative data host:

```sh
session-search --data-dir PRIMARY_DATA backup-cycle   --outbox BACKUP_OUTBOX --repositories REPOSITORIES_JSON
```

Use a dedicated, initially empty outbox and keep the same source and repository
configuration for its lifetime. A cycle creates one immutable recovery snapshot,
backs it up, and restores every destination fully to verify its evidence. Partial
success retains that snapshot and each successful receipt; retries process the
remaining destinations after checking repository identities. Newer capture stays
outside that pending snapshot until the following cycle.

Only complete verification publishes `OUTBOX/latest.json` and an immutable receipt
under `OUTBOX/receipts/`. These receipts work with `restore-backup` and the existing
guarded manual offload flow. The cycle then reclaims its staging snapshot. It never
removes native sessions, prunes repositories, or deletes canonical evidence.
An interrupted snapshot-building directory causes a deferral for inspection,
preventing repeated crashes from creating more staging generations.

The source staging and restore scratch filesystems receive conservative free-space
preflights with a 2 GiB default reserve. Destination repository capacity still
requires provisioning and monitoring; Restic deduplication makes its incremental
allocation different from the full restored size. A failed destination leaves the
cycle partial, and no complete receipt is issued. Do not enable the timer while a
required destination lacks capacity.

After a successful manual cycle, install `session-search-backup.service` and its
timer. Supply a private `backup.env` beside the other service configuration files:

```text
SESSION_SEARCH_DATA=/absolute/primary/data
SESSION_SEARCH_BACKUP_OUTBOX=/absolute/backup-outbox
SESSION_SEARCH_BACKUP_REPOSITORIES=/absolute/repositories.json
SESSION_SEARCH_RESERVE_BYTES=2147483648
```

Reload the user service manager and enable `session-search-backup.timer`. It runs
at six-hour calendar intervals and catches up after downtime. The outbox lock
coalesces overlaps. The service timeout leaves pending work available for retry.
This schedule is not a guaranteed recovery window.

Agent status reports the latest cycle result, destination counts, completion time,
and whether its snapshot publication matches the current catalog. It is historical
verification evidence, not a current repository health probe. Standby snapshots do
not inherit the primary's local backup status file.
