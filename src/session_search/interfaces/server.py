"""Authenticated HTTP adapter; transports share the catalog's retrieval contract."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from session_search.core.output import bounded_response
from session_search.core.protocol import MAX_REQUEST, MAX_REVISION_UPLOAD
from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.storage.catalog import Catalog
from session_search.storage.fencing import WriteFenced

def create_app(data_dir: Path, credentials: Path, *, readonly: bool = False,
               provider=None) -> FastAPI:
    app = FastAPI(title="Session Search", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        # Reload on each request: revocation does not depend on restarting workers.
        try:
            from session_search.interfaces.credentials import read_registry, device_entries
            entries = device_entries(read_registry(credentials))
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
        except WriteFenced:
            return JSONResponse({"version": 1, "status": "primary_fenced"}, status_code=503)
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
                data["citations"],
                neighbors=data.get("neighbors", 2)), data.get("budget", 32768))

    @app.post("/v1/revisions")
    def ingest(data: dict, request: Request):
        if readonly:
            raise HTTPException(409, "standby does not accept writes")
        if set(data) - {"request_id", "expected_revision", "session", "raw"} or not {
            "request_id", "expected_revision", "session"
        }.issubset(data):
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
                                    request_id=data["request_id"], raw=data.get("raw"))
            return {"version": 1, "status": "durable", **result}

    @app.post("/v1/migration-heads")
    def migration_heads(data: dict):
        if readonly:
            raise HTTPException(409, "migration checkpoints must come from the primary")
        sessions = data.get("sessions")
        if (set(data) != {"sessions"} or not isinstance(sessions, list)
                or not 1 <= len(sessions) <= 100
                or not all(isinstance(s, str) and 0 < len(s) <= 256 for s in sessions)):
            raise HTTPException(400, "migration lookup requires 1–100 session identities")
        placeholders = ",".join("?" for _ in sessions)
        with read() as catalog:
            rows = catalog.db.execute("SELECT h.session_id,h.revision,r.source,r.parser_version "
                "FROM heads h JOIN revisions r ON r.session_id=h.session_id AND r.revision=h.revision "
                f"WHERE h.session_id IN ({placeholders})", sessions).fetchall()
        return {"version": 1, "heads": {r["session_id"]: dict(r) for r in rows}}

    @app.post("/v1/recovery-heads")
    def recovery_heads(data: dict):
        if readonly:
            raise HTTPException(409, "recovery checkpoints must come from the primary")
        sessions = data.get("sessions")
        if (set(data) != {"sessions"} or not isinstance(sessions, list)
                or not 1 <= len(sessions) <= 10
                or not all(isinstance(s, str) and 0 < len(s) <= 256 for s in sessions)):
            raise HTTPException(400, "recovery lookup requires 1–10 session identities")
        heads = {}
        with read() as catalog:
            catalog.db.execute("BEGIN")
            for sid in sessions:
                row = catalog.db.execute(
                    "SELECT h.session_id,h.revision,CASE WHEN r.source='codex' THEN 'codex' "
                    "ELSE 'unsupported' END source FROM heads h JOIN revisions r "
                    "ON r.session_id=h.session_id AND r.revision=h.revision WHERE h.session_id=?",
                    (sid,),
                ).fetchone()
                if row is None:
                    continue
                raw = catalog.db.execute(
                    "SELECT digest,size FROM raw_sources WHERE session_id=? AND revision=? "
                    "ORDER BY size DESC,digest LIMIT 1", (sid, row['revision']),
                ).fetchone()
                heads[sid] = {**dict(row), "raw": dict(raw) if raw else None}
        return {"version": 1, "heads": heads}

    @app.get("/v1/objects/{key}")
    def raw_status(key: str, request: Request):
        if readonly:
            raise HTTPException(409, "raw transfer is available only on the primary")
        from session_search.storage.transfers import RawTransfers
        return RawTransfers(data_dir).status(request.state.producer, key)

    @app.put("/v1/objects/{key}")
    async def raw_chunk(key: str, offset: int, total: int, request: Request):
        if readonly:
            raise HTTPException(409, "standby does not accept raw uploads")
        from starlette.concurrency import run_in_threadpool
        from session_search.storage.transfers import OffsetConflict, RawTransfers
        chunk = await request.body()
        try:
            return await run_in_threadpool(RawTransfers(data_dir).append,
                request.state.producer, key, offset, total, chunk)
        except OffsetConflict:
            raise HTTPException(503, "transfer progress changed; retry from durable offset") from None
        except OSError:
            raise HTTPException(503, "storage unavailable; retain upload for retry") from None

    def revision_transfers():
        from session_search.storage.transfers import RawTransfers
        return RawTransfers(data_dir, namespace="revision-upload", max_size=MAX_REVISION_UPLOAD)

    @app.get("/v1/revision-objects/{key}")
    def revision_status(key: str, request: Request):
        if readonly:
            raise HTTPException(409, "revision transfer is available only on the primary")
        return revision_transfers().status(request.state.producer, key)

    @app.put("/v1/revision-objects/{key}")
    async def revision_chunk(key: str, offset: int, total: int, request: Request):
        if readonly:
            raise HTTPException(409, "standby does not accept revision uploads")
        if total > MAX_REVISION_UPLOAD:
            raise HTTPException(413, "normalized revision exceeds transfer limit")
        from starlette.concurrency import run_in_threadpool
        from session_search.storage.transfers import OffsetConflict
        try:
            return await run_in_threadpool(revision_transfers().append,
                request.state.producer, key, offset, total, await request.body())
        except (OffsetConflict, OSError):
            raise HTTPException(503, "retain revision upload and retry from durable progress") from None

    @app.post("/v1/revision-objects")
    def ingest_revision_object(data: dict, request: Request):
        if readonly:
            raise HTTPException(409, "standby does not accept writes")
        if set(data) != {"digest", "size"} or type(data["size"]) is not int:
            raise HTTPException(400, "invalid revision object reference")
        if not 0 < data["size"] <= MAX_REVISION_UPLOAD:
            raise HTTPException(413, "normalized revision exceeds transfer limit")
        try:
            # Bound large JSON materialization across server worker processes.
            with (data_dir / ".revision-ingest.lock").open("a") as guard:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                objects = revision_transfers().objects
                path = objects.path(data["digest"])
                if path.stat().st_size != data["size"] or not objects.verify(data["digest"]):
                    raise HTTPException(400, "revision object checksum or size mismatch")
                value = json.loads(path.read_bytes())
                if not isinstance(value, dict):
                    raise HTTPException(400, "revision envelope must be an object")
                result = ingest(value, request)
                # This object is transfer staging, never raw archival evidence.
                # A lost response safely repeats the transfer and idempotent ingest.
                path.unlink(missing_ok=True)
                return result
        except OSError:
            raise HTTPException(503, "revision staging unavailable; retain payload and retry") from None

    return app
