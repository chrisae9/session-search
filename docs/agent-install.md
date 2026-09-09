# Install Session Search for your user

This procedure is for the coding agent performing installation. The intended
result is working MCP retrieval, an installed retrieval skill, and background
capture that keeps the catalog current. Execute and verify the setup; do not leave
the user with a list of commands to run. Follow the user's requested deployment
mode and existing machine conventions.

## Inspect before changing anything

Identify the operating system, Python and uv availability, actual Codex home,
existing Session Search commands, MCP registration, skill, catalog, and scheduled
jobs. Resolve paths from this machine; do not copy another installation's paths.
Keep runtime state outside the checkout. Preserve existing configuration and use
[the migration guide](migration.md) when legacy data is present. Do not overwrite
an existing catalog or bootstrap an empty replacement for an unavailable one.

Use local keyword retrieval as the baseline when no mode was requested. It needs
no model or network service. If the user requests semantic search, inspect existing
model configuration and follow [embedding setup](usage.md#embeddings). Provision
models explicitly and keep their files outside the repository. Remote history or
embedding services require the user's intended destination and configuration.

## Install the runtime and retrieval skill

Clone the repository into a stable location, record its commit, and install with
Python 3.11+ and uv. From the checkout:

```sh
uv sync --locked --no-dev --extra mcp
```

The installed executable is `.venv/bin/session-search` inside that checkout.
Resolve its absolute path for MCP and scheduler configuration; those processes
cannot rely on your current shell or an activated virtual environment. Keep the
checkout in place while those configurations refer to it.

Install [skills/session-search/SKILL.md](../skills/session-search/SKILL.md) into the
agent host's recognized skill directory. Preserve any prior skill and its runtime
data separately. Avoid duplicate skills named `session-search`, including copies
reachable through symlinks. The retrieval skill directs normal search behavior;
this installation guide is not a replacement for it.

## Capture history and connect MCP

Choose a persistent data directory, a stable producer ID for this installation,
and the verified Codex source directory. Use the same values throughout setup.
The following are argument templates; substitute resolved absolute paths and
measured byte allowances rather than executing the placeholders literally:

```text
EXECUTABLE --data-dir DATA sync --codex-home CODEX_SOURCE --producer PRODUCER --max-bytes CAPTURE_BYTES --reserve-bytes RESERVE_BYTES
EXECUTABLE --data-dir DATA mcp
```

Choose a capture budget that admits the largest source file and respects available
space. Inspect the sync result for errors and deferred files. Capture preserves
native sessions. Raw archival and offload are separate opt-in features, not part
of this baseline installation.

Register the second command as the agent host's stdio MCP server named
`session-search`. For Codex, use the [MCP registration instructions](usage.md#agent-interface).
Preserve unrelated MCP settings. Use the exact same data directory for capture and
retrieval. When enabling embeddings, pass the same embedding configuration to the
indexer and MCP process; local mode must not enable remote fallback.

## Make refresh automatic

After a successful sync, configure a user-level launchd job on macOS or a systemd
user service and timer on Linux to run that same bounded sync command roughly
every five minutes. Adapt the [scheduler templates](../deploy/README.md#recurring-capture)
to the selected mode. The existing client templates include remote and raw-archive
options: omit those options for the local keyword baseline. Use absolute paths,
private log locations, and one scheduler per installation. Preserve unrelated jobs.
Validate and load the configuration, run it through the scheduler, and verify its
exit status and fresh `local_capture_sync` receipt.

`sync` captures history; it does not create embeddings. If semantic search is
configured, run a separate scheduled `embed --limit N` command against the same
catalog and provider, with N between 1 and 10000. Verify progress and failed work.
A large initial rebuild should run under the service manager so it survives the
installation session. Keep keyword retrieval available while it finishes. Report
partial semantic coverage and timeout fallback honestly. Do not infer completion
from a running process or an available model.

On other operating systems or without a usable user service manager, report that
automatic refresh is not configured rather than claiming the setup is complete.
User jobs run only when their host service manager is available; sleep and logout
can delay refresh.

## Verify the installed connection

Launch a fresh MCP client using the saved configuration, then verify:

- `status` reports the intended catalog and a recent successful capture receipt.
- `search` finds captured evidence and `context` expands its unchanged citation.
- A scoped search respects its session or project filter. Pass the actual calling
  thread ID as `current_session_id` and check current-thread exclusion.
- When semantic search was requested, it reports the intended embedding identity
  and semantic availability. Distinguish this check from full indexing coverage.

Keep transcript text and credentials out of installation logs and reports. Use
[the fictional walkthrough](quickstart.md) in a separate temporary store for a
smoke test when no real history is available; never mix test fixtures into the
user's catalog or present them as migrated history.

An independent MCP check does not reload an already-running agent connection.
If the host needs a reconnect or restart, state that remaining action without
interrupting the user's running work. Report the installed revision, selected mode,
verified coverage, scheduler state, and anything still pending.

## Update or roll back

Preserve the prior revision, configuration, and skill before updating. Read release
and migration notes, update the stable checkout and locked dependencies, then
repeat capture and fresh MCP verification. Avoid duplicate scheduled jobs.
For rollback, stop the new jobs and restore the prior runtime and registration;
do not delete captured history. Check catalog compatibility before pointing an
older executable at data changed by a newer version.
