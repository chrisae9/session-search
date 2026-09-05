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
