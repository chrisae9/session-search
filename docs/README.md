# Documentation

Give the [installation guide](agent-install.md) to your coding agent to set up
retrieval and background capture. The [fictional walkthrough](quickstart.md)
provides a separate smoke test without personal history.

| Task | Guide |
| --- | --- |
| Automate installation | [Agent installation procedure](agent-install.md) |
| Connect an agent | [MCP registration and optional skill](usage.md#agent-interface) |
| Capture personal history | [Local capture and search](usage.md#local-capture-and-search) |
| Configure a shared search service | [HTTP server and client](usage.md#http-server-and-client) |
| Configure semantic search | [Embeddings](usage.md#embeddings) · [Offline local inference](usage.md#offline-local-inference) |
| Schedule capture or operate optional services | [Deployment templates and operations](../deploy/README.md) |
| Plan backups and recovery | [Backup ownership](backup-ownership.md) · [Recovery commands](usage.md#recovery-development) · [Migration](migration.md) |
| Understand retained storage | [Storage maintenance](storage-maintenance.md) · [Optional offload](offload.md) |
| Understand design and guarantees | [Rendered architecture](architecture-rendered.md) · [Editable Mermaid](architecture.md) · [Reliability contract](reliability.md) |
| Check feature status | [Implementation progress](implementation.md) |
| Evaluate retrieval | [Reproducible synthetic benchmark](../benchmarks/README.md) · [Evaluation findings](retrieval-evaluation.md) |
| Contribute or inspect release boundaries | [Contributing](../CONTRIBUTING.md) · [Source provenance](provenance.md) |

The [usage reference](usage.md) retains detailed commands and stable section links.
Architecture describes the design; implementation status and evaluation results
describe what has been exercised. Backup, replica, and offload configuration is
optional, not a prerequisite for local search.
