"""Small stdlib-only client. Failover is restricted to read operations."""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from session_search.core.records import canonical_json


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RemoteError(Exception):
    def __init__(self, status: int | None):
        self.status = status
        super().__init__(f"remote request failed ({status or 'connection'})")


def validate_endpoint(endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("endpoint must not contain credentials, query, or fragment")
    loopback = parsed.hostname == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("HTTPS is required except on loopback")
    if not parsed.hostname:
        raise ValueError("endpoint hostname is required")
    return endpoint.rstrip("/")


class Client:
    def __init__(self, primary: str, token_file: Path, *, standby: str | None = None,
                 timeout: float = 10):
        self.primary = validate_endpoint(primary)
        self.standby = validate_endpoint(standby) if standby else None
        if not 0 < timeout <= 60:
            raise ValueError("timeout must be greater than zero and at most 60 seconds")
        self.timeout = timeout
        self.token_file = token_file
        self.opener = urllib.request.build_opener(NoRedirect())

    def _request(self, endpoint: str, route: str, payload: dict | bytes | None,
                 *, method: str | None = None) -> dict:
        token = self.token_file.read_text().strip()
        request = urllib.request.Request(
            endpoint + route, data=(payload if isinstance(payload, bytes) else
                                   canonical_json(payload).encode() if payload is not None else None),
            headers={"Content-Type": "application/octet-stream" if isinstance(payload, bytes)
                     else "application/json", "Authorization": "Bearer " + token}, method=method,
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                content = response.read(65537)
                if len(content) > 65536:
                    raise RemoteError(502)
                result = json.loads(content)
                if not isinstance(result, dict) or result.get("version") != 1:
                    raise RemoteError(502)
                return result
        except urllib.error.HTTPError as exc:
            raise RemoteError(exc.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            raise RemoteError(None) from None
        except (ValueError, UnicodeError):
            raise RemoteError(502) from None

    def read(self, operation: str, payload: dict | None = None) -> dict:
        if operation not in {"search", "context", "status"}:
            raise ValueError("unsupported read operation")
        for index, endpoint in enumerate(filter(None, (self.primary, self.standby))):
            try:
                response = self._request(endpoint, "/v1/" + operation, payload)
                response["served_by"] = "standby" if index else "primary"
                return response
            except RemoteError as exc:
                if exc.status not in {None, 500, 502, 503, 504}:
                    raise
        return {"version": 1, "status": "unavailable", "reason": "search_servers_unreachable"}

    def upload(self, payload: dict) -> dict:
        # No alternate destination: a standby never becomes a writer implicitly.
        return self._request(self.primary, "/v1/revisions", payload)

    def upload_raw(self, path: Path, digest: str) -> dict:
        from session_search.storage.transfers import MAX_CHUNK
        from session_search.storage.objects import ObjectStore
        ObjectStore(path.parent).path(digest)  # Validate the URL segment.
        route = "/v1/objects/" + digest
        progress = self._request(self.primary, route, None)
        total = path.stat().st_size
        if progress["status"] == "complete":
            if progress["offset"] != total:
                raise RemoteError(409)
            return progress
        offset = progress["offset"]
        if not isinstance(offset, int) or not 0 <= offset <= total:
            raise RemoteError(502)
        with path.open("rb") as source:
            source.seek(offset)
            while True:
                chunk = source.read(min(MAX_CHUNK, total - offset))
                progress = self._request(self.primary, f"{route}?offset={offset}&total={total}",
                                         chunk, method="PUT")
                expected = offset + len(chunk)
                if progress.get("offset") != expected:
                    raise RemoteError(502)
                offset = expected
                if progress.get("status") == "complete":
                    break
                if not chunk:
                    raise RemoteError(502)
        if progress.get("status") != "complete":
            raise RemoteError(502)
        return progress
