# Session Search

Search past Codex conversations from the terminal or through MCP. Find an old
command, recover the reasoning behind a change, or give an agent the original
conversation to work from.

Results include excerpts and citations that open the recorded conversation, even
if the source session has changed since capture. You can filter by project,
session, role, or time. Keyword search works without a model; optional embeddings
let you search by meaning.

## Install and search

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).
The commands below use a POSIX shell on macOS or Linux. Install from source:

```sh
git clone https://github.com/chrisae9/session-search.git
cd session-search
uv sync --locked --no-dev --extra mcp
```

Capture your local Codex history into a separate search store, then search it:

```sh
search_store="$HOME/.local/share/session-search"
uv run --no-sync session-search --data-dir "$search_store" capture \
  --codex-home "$HOME/.codex" --producer workstation
uv run --no-sync session-search --data-dir "$search_store" search \
  'duplicate requests' --literal
```

Capture leaves the source files in place. Run it again to pick up new or changed
sessions. If you use a custom Codex home, pass that directory to `--codex-home`.
To try the same workflow with a fictional conversation first, follow the
[example walkthrough](docs/quickstart.md), which also shows how to expand a citation.

## Use it from your agent

From the same checkout and shell, register the installed executable with Codex:

```sh
codex mcp add session-search -- "$PWD/.venv/bin/session-search" \
  --data-dir "$search_store" mcp
```

The agent gets three read-only tools: `search`, `context`, and `status`. Capture
runs separately. See [agent setup](docs/usage.md#agent-interface) for the optional
skill, current-conversation exclusion, and other connection options.

## Beyond keyword search

- [Semantic search](docs/usage.md#embeddings): configure a local model or an explicit
  embedding endpoint. Models are not downloaded automatically.
- [Scheduled capture](deploy/README.md): keep history current without manual runs.
- [Shared service](docs/usage.md#http-server-and-client): search across computers
  using authenticated clients. Local use needs no server.
- [Architecture](docs/architecture-rendered.md), [search evaluation](benchmarks/README.md),
  and [contributing](CONTRIBUTING.md): understand the code and run the checks.

This is a development version. Keyword capture and search have no third-party
runtime dependencies; MCP, server, and inference support are optional extras.
See [feature status](docs/implementation.md) for tested behavior and remaining work,
and the [documentation index](docs/README.md) for backup and recovery procedures.

New application code is Apache-2.0. The [source provenance record](docs/provenance.md)
describes the parser permissions and dependency review still required before public distribution.
