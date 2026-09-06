"""Three read-only agent tools, using the same local or remote operations."""

import os
import asyncio
from dataclasses import asdict
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from session_search.core.output import bounded_response
from session_search.core.records import SearchQuery, canonical_json
from session_search.interfaces.client import Client
from session_search.storage.catalog import Catalog


def create_mcp(data_dir: Path, client: Client | None = None, provider=None) -> FastMCP:
    from session_search.core.embeddings import LocalEmbedder
    if isinstance(provider, LocalEmbedder):
        from session_search.core.local_worker import IsolatedLocalEmbedder
        provider = IsolatedLocalEmbedder(provider)
    server = FastMCP("session-search")
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                  idempotentHint=True, openWorldHint=client is not None)

    capacity = asyncio.BoundedSemaphore(8)

    async def dispatch(function, *args):
        try:
            await asyncio.wait_for(capacity.acquire(), timeout=0.1)
        except TimeoutError:
            return {"version": 1, "status": "unavailable", "reason": "mcp_busy"}
        task = asyncio.create_task(asyncio.to_thread(function, *args))

        def completed(finished):
            capacity.release()
            if not finished.cancelled():
                finished.exception()  # Retrieve failures even after caller cancellation.

        task.add_done_callback(completed)
        # A cancelled caller must not release capacity while its thread still runs.
        return await asyncio.shield(task)

    def invoke(operation, payload):
        if client:
            from session_search.interfaces.capture_status import with_outage_capture_status
            result = client.read(operation, payload)
            return (with_outage_capture_status(result, data_dir)
                    if operation != 'status' else result)
        with Catalog(data_dir.resolve(), readonly=True) as catalog:
            if operation == "search":
                from session_search.storage.semantic import hybrid_search
                return hybrid_search(catalog, SearchQuery(**payload), provider)
            if operation == "context":
                return catalog.context(payload["citations"],
                                       neighbors=payload["neighbors"])
            return {"version": 1, "status": "ok", "coverage": catalog.status()}

    @server.tool(annotations=annotations, structured_output=False)
    async def search(text: str, literal: bool = False, role: str | None = None,
               after: str | None = None, before: str | None = None,
               project: str | None = None, session_id: str | None = None,
               producer: str | None = None, include_subagents: bool = False,
               include_current_session: bool = False, exclude_sessions: list[str] | None = None,
               limit: int = 10, budget: int = 16384, current_session_id: str | None = None) -> str:
        """Find past evidence. Pass current_session_id for thread exclusion on shared MCP hosts."""
        exclude = list(exclude_sessions or [])
        current = current_session_id or os.environ.get("CODEX_THREAD_ID")
        if not include_current_session and current:
            exclude.append(current)
        query = SearchQuery(text, literal, role, after, before, project, session_id, producer,
                            tuple(exclude), include_subagents, limit)
        result = await dispatch(invoke, "search", asdict(query))
        result['current_thread_exclusion'] = ('disabled_by_request' if include_current_session
                                              else 'applied' if current else 'unknown')
        return canonical_json(bounded_response(result, budget))

    @server.tool(annotations=annotations, structured_output=False)
    async def context(citations: list[dict], neighbors: int = 2, budget: int = 32768) -> str:
        """Expand exact cited revisions. Unavailable evidence is never replaced by a newer revision."""
        return canonical_json(bounded_response(await dispatch(invoke, "context", {
            "citations": citations, "neighbors": neighbors}), budget))

    def status_result() -> dict:
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
        return result

    @server.tool(annotations=annotations, structured_output=False)
    async def status() -> str:
        """Report coverage, local capture freshness, and pending uploads."""
        return canonical_json(bounded_response(await dispatch(status_result)))

    return server
