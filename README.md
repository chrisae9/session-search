# Session Search

Find useful evidence from past coding sessions through your agent.

**Under development.** Local capture, cited keyword/hybrid retrieval, CLI/MCP, authenticated HTTP, upload queuing, and verified snapshot primitives are implemented. Deployment automation, remote raw-file transfer, migration, and release qualification remain unfinished. Existing session files have not been migrated.

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
