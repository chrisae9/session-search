# Try Session Search

This walkthrough uses the [included fictional conversation](../examples/codex-home/sessions/demo-retry.jsonl).
It does not read your Codex history or contact a search or embedding service.
The source stays in the checkout; captured data and output go into a new temporary
directory outside it. Commands below assume a POSIX shell on macOS or Linux.

## 1. Install the core

From the repository root, with Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/) installed:

```sh
uv sync --locked --no-dev
```

Installation may download Python or build dependencies. The following keyword
workflow needs no network or model. Server, MCP, and inference extras are not
required for this demo.

## 2. Capture the fictional source

Keep the same shell open for the remaining steps:

```sh
demo_dir="$(mktemp -d)"
uv run --no-sync session-search --data-dir "$demo_dir/store" init
uv run --no-sync session-search --data-dir "$demo_dir/store" capture \
  --codex-home "$PWD/examples/codex-home" --producer demo
```

The capture response should report `status: "complete"` and `discovered: 1`.
The explicit `--codex-home` is what selects the fictional input. Changing only
`--data-dir` would create a separate store but still read your configured history.

## 3. Find the decision

```sh
uv run --no-sync session-search --data-dir "$demo_dir/store" search \
  'duplicate requests' --literal --role assistant > "$demo_dir/search.json"
uv run --no-sync python -m json.tool "$demo_dir/search.json"
```

The first result should contain this excerpt and a `citation` whose `session_id`
is `demo-retry`:

> We prevent duplicate requests by storing a unique request token before executing
> the operation. Repeated tokens return the saved result instead of running again.

The citation also includes a revision digest, event ID, and offset. Preserve the
whole object: it addresses the original evidence, not whichever session revision
happens to be newest.

## 4. Expand its citation

Extract the returned citation unchanged and pass it to `context`:

```sh
citations="$(uv run --no-sync python -c \
  'import json, sys; print(json.dumps([json.load(sys.stdin)["results"][0]["citation"]]))' \
  < "$demo_dir/search.json")"
uv run --no-sync session-search --data-dir "$demo_dir/store" context \
  "$citations" --neighbors 1
uv run --no-sync session-search --data-dir "$demo_dir/store" status
```

Context should include both “Why did we change request handling?” and the answer
above, with `status: "ok"` for the cited result. Catalog status should report one
session. All generated demo files remain under `$demo_dir`; the source fixture is
unchanged. You can remove that temporary directory when finished.

## Next steps

- [Connect your agent through MCP](usage.md#agent-interface); install the `mcp` extra first.
- [Capture your own history](usage.md#local-capture-and-search) with an explicit source and a separate store.
- [Add semantic search](usage.md#embeddings) using an explicitly provisioned model.
- [Develop and run checks](../CONTRIBUTING.md), or browse the [documentation index](README.md).
