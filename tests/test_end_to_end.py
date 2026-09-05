"""Real process and transport checks; all data and credentials are synthetic."""

import asyncio
import json
import socket
import subprocess
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from session_search.capture.queue import UploadQueue
from session_search.core.records import Event, SessionRevision
from session_search.interfaces.client import Client, RemoteError
from session_search.interfaces.credentials import update_device
from session_search.storage.catalog import Catalog
from session_search.storage.snapshots import activate_replica, create_snapshot


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(root, credentials, port, *, readonly=False):
    args = [sys.executable, "-m", "session_search.interfaces.cli", "--data-dir", str(root),
            "serve", "--credentials", str(credentials), "--port", str(port)]
    if readonly:
        args.append("--readonly")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_ready(client, process):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        assert process.poll() is None, "server exited before readiness"
        try:
            if client.read("status")["status"] == "ok":
                return
        except RemoteError:
            pass
        time.sleep(0.02)
    raise AssertionError("server did not become ready")


def test_real_http_failover_retains_upload_and_recovers_on_reconnect(tmp_path):
    primary_dir, replica_dir = tmp_path / "primary", tmp_path / "replica"
    credentials, token = tmp_path / "registry.json", tmp_path / "token"
    update_device(credentials, "device", token)
    with Catalog(primary_dir) as catalog:
        catalog.ingest(SessionRevision("old", (Event("e", "user", "existing evidence"),)),
                       producer="device", request_id="old")
        create_snapshot(catalog, tmp_path / "snapshot")
    activate_replica(tmp_path / "snapshot", replica_dir)
    primary_port, standby_port = free_port(), free_port()
    primary = start_server(primary_dir, credentials, primary_port)
    standby = start_server(replica_dir, credentials, standby_port, readonly=True)
    client = Client(f"http://127.0.0.1:{primary_port}", token,
                    standby=f"http://127.0.0.1:{standby_port}", timeout=1)
    try:
        wait_ready(Client(client.primary, token), primary)
        wait_ready(Client(client.standby, token), standby)
        assert client.read("search", {"text": "existing"})["served_by"] == "primary"
        primary.terminate()
        primary.wait(timeout=5)
        response = client.read("search", {"text": "existing"})
        assert response["served_by"] == "standby" and response["results"]
        with UploadQueue(tmp_path / "client") as queue:
            queue.ingest(SessionRevision("new", (Event("e", "user", "queued evidence"),)),
                         producer="device", request_id="new")
            assert queue.flush(client, now=0)["queue"]["pending"] == 1
            primary = start_server(primary_dir, credentials, primary_port)
            wait_ready(Client(client.primary, token), primary)
            assert queue.flush(client, now=10)["sent"] == 1
        assert client.read("search", {"text": "queued"})["results"]
    finally:
        for process in (primary, standby):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


def test_real_stdio_mcp_returns_cited_evidence(tmp_path):
    with Catalog(tmp_path) as catalog:
        catalog.ingest(SessionRevision("example", (Event("e", "user", "stdio recovery"),)),
                       producer="device", request_id="example")

    async def run():
        parameters = StdioServerParameters(command=sys.executable, args=[
            "-m", "session_search.interfaces.cli", "--data-dir", str(tmp_path), "mcp"
        ])
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert {tool.name for tool in tools.tools} == {"search", "context", "status"}
                answer = await client.call_tool("search", {"text": "recovery"})
                assert not answer.isError
                payload = json.loads(answer.content[0].text)
                assert payload["results"][0]["citation"]["session_id"] == "example"

    asyncio.run(run())
