# Backup ownership

Use the backup system that fits your environment. The core search service does not
schedule backups, select storage destinations, or require independent repositories.
Replication is optional and provides search availability, not disaster recovery.

A recoverable service needs:

- A consistent SQLite catalog and all referenced immutable objects from its data directory.
- Raw archives, if exact native session reconstruction is required and archival was enabled.
- Configuration, model identity and provisioning details, and credentials stored under the operator's recovery policy.

A home-directory backup can cover these inputs when they are under that directory
and not excluded. Coordinate SQLite consistency using an online database backup or
writer exclusion. Test a restore into a separate directory; a successful file copy
alone does not establish application recoverability. Project worktrees, attachments,
and the coding application's own state are outside the search catalog.

The optional snapshot, Restic and offload commands remain available for operators
who explicitly choose them. They are not the default deployment architecture. The
current offload implementation requires its configured restore receipts; it does
not infer safety from an unrelated backup job. Operators who use external backups
own retention and any subsequent native-file removal. Session Search never deletes
canonical history merely because a source file disappeared.
