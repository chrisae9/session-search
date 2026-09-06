# Manual client offload

Offload removes eligible native Codex JSONL files after fresh restoration from every configured backup destination. It preserves searchable server evidence and raw recovery copies. It is never part of scheduled capture or an MCP search operation.

First enable [primary verification](../deploy/README.md) with a complete recovery receipt and enough restore workspace. Replica availability alone is insufficient. Each client needs exact raw acknowledgements from successful archival uploads; older queue records without that mapping are reported as missing and remain in place. Use `recover-client-acknowledgements` with the same client data, primary, and token options to recover missing mappings without reading or uploading raw files. It matches the primary’s current, device-owned record to the client’s existing checkpoint and acknowledged revision; it never rebases a head. Recovery scans up to 1,000 sources per call. When `next_cursor` is returned, pass it with `--cursor` to continue past unresolved records. Ambiguous or mismatched records remain unresolved; an explicitly requested archival recapture and flush may be needed.

Create a private plan for review:

```sh
session-search --data-dir <client-data> --primary <primary-https> --token-file <token-file> \
  plan-client-offload --codex-home <codex-home> --output <plan.json>
```

The plan contains at most 100 files and its digest. Candidates must be unchanged regular JSONL files within the chosen Codex home, at least 30 days old, with complete trailing records and exact current acknowledgements. Pending revisions, changed files, symlinks, and unknown mappings do not qualify. Existing plan files are never overwritten.

After review, stop local Codex writers and apply the exact plan from a process outside the active Codex session:

```sh
session-search --data-dir <client-data> --primary <primary-https> --token-file <token-file> \
  apply-client-offload <plan.json> --plan-id <reviewed-digest>
```

Apply holds the capture and upload locks, submits a fresh random nonce to the primary, and waits up to two hours for exact backup verification. `--verification-timeout` can shorten that wait. It never uses standby verification or a saved proof from another attempt. Primary errors, interrupted verification, invalid proofs, or restarted Codex writers stop application.

Each file's acknowledgement, age, identity, full digest, and trailing newline are checked again after verification. Writer and proof-expiry checks run again after hashing and before removal. Changed files are skipped. An interrupted apply can leave some reviewed files removed and others retained; inspect the result and make a new plan for the remainder. Backup verification reports are not reusable offline deletion permits.

Live offload still requires deployment qualification and full independent backup coverage. Automated tests remove only synthetic native files in temporary directories.
