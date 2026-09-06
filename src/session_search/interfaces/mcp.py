"""Three read-only agent tools, using the same local or remote operations."""

import os
from dataclasses import asdict
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from session_search.core.output import bounded_response
from session_search.core.records import SearchQuery, canonical_json
from session_search.interfaces.client import Client
from session_search.storage.catalog import Catalog


def create_mcp(data_dir: Path, client: Client | None = None, provider=None) -> FastMCP:
    server = FastMCP("session-search")
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                  idempotentHint=True, openWorldHint=client is not None)

    def invoke(operation, payload):
        if client:
            return client.read(operation, payload)
        with Catalog(data_dir.resolve(), readonly=True) as catalog:
            if operation == "search":
                from session_search.storage.semantic import hybrid_search
                return hybrid_search(catalog, SearchQuery(**payload), provider)
            if operation == "context":
                return catalog.context(payload["citations"],
                                       neighbors=payload["neighbors"])
            return {"version": 1, "status": "ok", "coverage": catalog.status()}

    @server.tool(annotations=annotations, structured_output=False)
    def search(text: str, literal: bool = False, role: str | None = None,
               after: str | None = None, before: str | None = None,
               project: str | None = None, session_id: str | None = None,
               producer: str | None = None, include_subagents: bool = False,
               include_current_session: bool = False, exclude_sessions: list[str] | None = None,
               limit: int = 10, budget: int = 16384) -> str:
        """Find past evidence. Preserve citations for context; check coverage before negative claims."""
        exclude = list(exclude_sessions or [])
        if not include_current_session and os.environ.get("CODEX_THREAD_ID"):
            exclude.append(os.environ["CODEX_THREAD_ID"])
        query = SearchQuery(text, literal, role, after, before, project, session_id, producer,
                            tuple(exclude), include_subagents, limit)
        return canonical_json(bounded_response(invoke("search", asdict(query)), budget))

    @server.tool(annotations=annotations, structured_output=False)
    def context(citations: list[dict], neighbors: int = 2, budget: int = 32768) -> str:
        """Expand exact cited revisions. Unavailable evidence is never replaced by a newer revision."""
        return canonical_json(bounded_response(invoke("context", {
            "citations": citations, "neighbors": neighbors}), budget))

    @server.tool(annotations=annotations, structured_output=False)
    def status() -> str:
        """Report search coverage and availability; replication and backup are separate states."""
        from session_search.interfaces.capture_status import capture_status
        result = invoke("status", None)
        result["local_capture_sync"] = capture_status(data_dir)
        queue_path = data_dir / "upload-queue.sqlite3"
        if client and queue_path.exists():
            # Use a read-only connection: even status must not bootstrap a queue.
            import sqlite3
            db = sqlite3.connect(queue_path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                result["pending_uploads"] = db.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
            finally:
                db.close()
        return canonical_json(bounded_response(result))

    return server
