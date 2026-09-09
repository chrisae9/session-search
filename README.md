# Session Search

Give your coding agent access to past conversations: decisions, commands, failed
approaches, and the context behind a change. The agent searches recorded evidence
and follows citations back to the original exchange.

Codex session files are the live source. New conversations are captured in the
background, and the agent uses three MCP tools—`search`, `context`, and `status`—to
retrieve them when earlier work is relevant. The included skill teaches it to
check the evidence, exclude its current conversation, and report missing coverage.

## Ask your agent to set it up

Give your coding agent this repository and ask:

> Install Session Search from https://github.com/chrisae9/session-search.
> Follow docs/agent-install.md. Configure local search over my Codex history,
> install the retrieval skill, and schedule background capture. Verify search and
> citation expansion through MCP, then tell me what is ready and what is still
> indexing. Preserve any existing installation and history.

The [agent installation guide](docs/agent-install.md) covers discovery, setup,
automatic refresh, verification, and updates. The agent needs terminal access and
permission to configure your machine. This repository provides the procedure;
installation still requires the agent to carry it out.

## What you can ask afterward

- “Find the conversation where we decided how retries should work.”
- “What did we try last time this test failed?”
- “Before changing this, check whether we discussed the same constraint before.”

Search returns excerpts with citations to specific recorded revisions. The agent
can expand those citations and filter by project, session, role, or time. A search
hit is evidence to inspect, not a guarantee that an old proposal was adopted.

Local keyword search needs no model or server. Optional embeddings support
searching by meaning; a shared service lets agents retrieve history captured on
multiple computers. Models and remote connections are configured explicitly.

## Inside the repository

- [Agent installation](docs/agent-install.md) and [retrieval skill](skills/session-search/SKILL.md)
- [Usage reference](docs/usage.md) and [background services](deploy/README.md)
- [Architecture](docs/architecture-rendered.md) and [search evaluation](benchmarks/README.md)
- [Contributing](CONTRIBUTING.md) and [fictional walkthrough](docs/quickstart.md)

Development version; see [implementation status](docs/implementation.md) for tested
behavior and remaining work. Python 3.11+ is required. MCP, server, and inference
support are optional dependencies. [All documentation](docs/README.md).

Apache-2.0. See [source provenance](docs/provenance.md) for the origin of the parser
and dependency licensing.
