# Contributing

Start with the [synthetic quickstart](docs/quickstart.md) to see the user workflow.
Read [implementation status](docs/implementation.md) before extending a feature
and [AGENTS.md](AGENTS.md) for the repository's invariants. The architecture is a
design reference, not proof of deployment.

## Development setup and checks

With Python 3.11+ and uv, run from the repository root:

```sh
uv sync --locked --group dev --extra server --extra mcp --extra embeddings
uv run --no-sync ruff check src tests benchmarks
uv run --no-sync pytest -q
uv build
```

Install Restic to exercise the real backup/restore integration test; a skipped
test does not establish recovery safety. Native inference dependencies and model
files are provisioned separately. Preserve optional dependency boundaries: core
capture and keyword search must remain usable without server, MCP, or model packages.

[CI](.github/workflows/tests.yml) runs Linux and macOS with Python 3.11 and 3.14,
plus synthetic evaluation and offline core/MCP installation checks. Use targeted
tests while developing, then run the relevant full checks before submitting changes.

## Where to work

| Area | Source | Tests and examples |
| --- | --- | --- |
| Capture and parsing | `src/session_search/capture/` | `tests/test_capture.py`, `tests/test_codex.py`, `examples/codex-home/` |
| Evidence, citations, output budgets | `src/session_search/core/` | `tests/test_output.py`, `tests/test_catalog.py` |
| Ranking, indexing, durable storage | `src/session_search/storage/` | `tests/test_evidence_ranking.py`, `tests/test_semantic.py`, `tests/test_snapshots.py` |
| CLI, HTTP, MCP | `src/session_search/interfaces/` | `tests/test_transport.py`, `tests/test_queue_mcp.py`, `tests/test_mcp_dispatch.py` |
| Evaluation | `benchmarks/` | [Benchmark guide](benchmarks/README.md) |

## Change boundaries

- Use fictional fixtures. Keep session transcripts, credentials, models, generated
  catalogs, and private evaluation records outside the repository.
- Preserve immutable citations, query filters, offline behavior, and durable
  history. Source disappearance must not delete captured evidence.
- Test the behavior that changes. For ranking work, state the hypothesis and
  acceptance criteria before evaluating; distinguish regressions, synthetic
  results, and demonstrated general improvements.
- Explain the user-visible change and verification in the change description.
  Keep documentation generic and link to existing procedures instead of copying them.

New application code is Apache-2.0. The [provenance record](docs/provenance.md)
describes the unresolved parser permissions and dependency review required before
public distribution. Presentation changes do not resolve that release boundary.
