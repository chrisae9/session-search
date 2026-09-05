# Recurring replication

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
