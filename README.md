# Session Search

Find useful evidence from past coding sessions through your agent.

**Under development.** Local and remote capture, cited retrieval, CLI/MCP, authenticated search failover, shared raw storage, and scheduled client sync are implemented and running in a pilot deployment. Historical import and exact backup restoration have been exercised. Full independent backup coverage, thin-client offload coordination, local inference packaging, and release qualification remain unfinished.

- [Architecture and diagrams](docs/architecture-rendered.md)
- [Editable Mermaid source](docs/architecture.md)
- [Reliability contract](docs/reliability.md)
- [Implementation progress](docs/implementation.md)
- [Development usage](docs/usage.md)

Python 3.11 or later is required. The core package has no third-party runtime dependencies; server, MCP, and inference dependencies are optional.

```sh
uv sync --group dev --extra server --extra mcp --extra embeddings
uv run pytest
uv run session-search --help
```

After installation, the `session-search` executable runs without a package-manager or network bootstrap. Embedding model files are provisioned explicitly. Keyword search does not require a model.

Licensed under Apache-2.0. See [provenance](docs/provenance.md) for reused components.
