"""Authenticated HTTP adapter; transports share the catalog's retrieval contract."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from session_search.core.output import bounded_response
from session_search.core.records import Citation, Event, SearchQuery, SessionRevision
from session_search.storage.catalog import Catalog

MAX_REQUEST = 8 * 1024 * 1024


def create_app(data_dir: Path, credentials: Path, *, readonly: bool = False,
               provider=None) -> FastAPI:
    app = FastAPI(title="Session Search", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        # Reload on each request: revocation does not depend on restarting workers.
        try:
            entries = json.loads(credentials.read_text())
        except (OSError, ValueError):
            return JSONResponse({"version": 1, "status": "unavailable"}, status_code=503)
        supplied = request.headers.get("authorization", "")
        if not supplied.startswith("Bearer "):
            return JSONResponse({"version": 1, "status": "unauthorized"}, status_code=401)
        token_hash = hashlib.sha256(supplied[7:].encode()).hexdigest()
        producer = next((name for name, expected in entries.items()
                         if isinstance(expected, str) and hmac.compare_digest(expected, token_hash)), None)
        if producer is None:
            return JSONResponse({"version": 1, "status": "unauthorized"}, status_code=401)
        request.state.producer = producer
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REQUEST:
                return JSONResponse({"version": 1, "status": "request_too_large"}, status_code=413)
        # Starlette's cached body is replayed to downstream request handling.
        request._body = bytes(body)
        try:
            return await call_next(request)
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"version": 1, "status": "invalid_request"}, status_code=400)

    def read():
        return Catalog(data_dir.resolve(), readonly=True)

    @app.get("/v1/status")
    def status():
        with read() as catalog:
            return {"version": 1, "status": "ok", "coverage": catalog.status(),
                    "server_role": "standby" if readonly else "primary"}

    @app.post("/v1/search")
    def search(data: dict, request: Request):
        from session_search.storage.semantic import hybrid_search
        budget = data.pop("budget", 16384)
        with read() as catalog:
            return bounded_response(hybrid_search(catalog, SearchQuery(**data), provider), budget)

    @app.post("/v1/context")
    def context(data: dict, request: Request):
        if set(data) - {"citations", "neighbors", "budget"}:
            raise HTTPException(400, "unknown context field")
        with read() as catalog:
            return bounded_response(catalog.context(
                [Citation(**value) for value in data["citations"]],
                neighbors=data.get("neighbors", 2)), data.get("budget", 32768))

    @app.post("/v1/revisions")
    def ingest(data: dict, request: Request):
        if readonly:
            raise HTTPException(409, "standby does not accept writes")
        if set(data) != {"request_id", "expected_revision", "session"}:
            raise HTTPException(400, "revision envelope fields do not match version 1")
        value = dict(data["session"])
        value["events"] = tuple(Event(**event) for event in value["events"])
        revision = SessionRevision(**value)
        with Catalog(data_dir.resolve()) as catalog:
            # BEGIN IMMEDIATE fences the compare-and-write from another request.
            catalog.db.execute("BEGIN IMMEDIATE")
            prior = catalog.db.execute(
                "SELECT revision FROM receipts WHERE producer=? AND request_id=?",
                (request.state.producer, data["request_id"]),
            ).fetchone()
            head = catalog.db.execute("SELECT revision FROM heads WHERE session_id=?",
                                      (revision.session_id,)).fetchone()
            current = head[0] if head else None
            if not prior and current not in {data["expected_revision"], revision.revision}:
                catalog.db.rollback()
                raise HTTPException(409, "session revision conflict; reconcile before retry")
            result = catalog.ingest(revision, producer=request.state.producer,
                                    request_id=data["request_id"])
            return {"version": 1, "status": "durable", **result}

    return app
