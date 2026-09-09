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
with real Restic backup/restore, offline core/MCP installation, workflow lint,
launchd syntax, and executable local scheduler commands. The synthetic keyword
[regression gate](benchmarks/README.md#ci-regression-gate) saves per-case reports
as run artifacts.

Run [qualification](.github/workflows/qualification.yml) manually from Actions
before a release or after transport/native-runtime changes. Its Linux job uses a
real loopback SSH server and rsync to verify replication, updates, and retained
citations, and validates systemd unit syntax. Its macOS job builds the native
runtime, downloads a pinned public model, and checks inference and MCP with OS
networking denied, including timeout fallback and recovery.

Qualification uses disposable runners and fictional sessions. It does not test
cross-device connectivity, Tailscale configuration, real scheduler activation,
or Linux native inference. The native checks establish operation, not retrieval
quality on a representative corpus. Use targeted tests while developing, then
run the relevant full checks before submitting changes.

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

Repository code is Apache-2.0. The [provenance record](docs/provenance.md)
describes the parser's origin and ownership confirmation. Dependencies retain
their own licenses.
