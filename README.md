# Session Search

Find useful evidence from past coding sessions through your agent.

Ask “Why did we change request handling?” and retrieve the recorded decision, with
a citation you can expand into its original conversation:

> We prevent duplicate requests by storing a unique request token before executing
> the operation. Repeated tokens return the saved result instead of running again.

**[Try the five-minute synthetic quickstart](docs/quickstart.md).** It captures only
the included fictional conversation, searches it, and expands its citation.
No personal history, server, or embedding model is needed.

Your agent uses three read-only MCP tools: **search**, **context**, and **status**.
The CLI handles capture and administration. Use an isolated local store or connect
lightweight clients to an authenticated service. Keyword search works without a
model; semantic search is optional and models are provisioned explicitly.

| I want to… | Start here |
| --- | --- |
| Try capture, search, and citations | [Synthetic quickstart](docs/quickstart.md) |
| Connect my agent | [MCP and skill setup](docs/usage.md#agent-interface) |
| Configure my own history or models | [Usage guide](docs/usage.md) |
| Run a shared service or scheduled capture | [Operations](deploy/README.md) |
| Understand the design and guarantees | [Architecture](docs/architecture-rendered.md) · [Reliability](docs/reliability.md) |
| Change the code | [Contributing](CONTRIBUTING.md) |
| Evaluate search quality | [Synthetic benchmark](benchmarks/README.md) · [Findings and limitations](docs/retrieval-evaluation.md) |

Python 3.11+ is required. The core package has no third-party runtime dependencies;
server, MCP, and inference packages are optional. See the [documentation index](docs/README.md)
for recovery, storage, and advanced configuration.

**Development status:** implemented features run in a pilot deployment; broader
qualification remains unfinished. See [implementation status](docs/implementation.md)
for the distinction. Backups and retention are operator-owned; replica, Restic,
and offload workflows are optional.

New application code is Apache-2.0. Public distribution remains pending
[provenance clearance](docs/provenance.md) for reused parser components and dependency review.
