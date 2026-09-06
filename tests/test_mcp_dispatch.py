import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from session_search.interfaces.mcp import create_mcp


class SlowClient:
    def __init__(self):
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.started = 0

    def read(self, operation, payload):
        if operation == 'search':
            with self.lock:
                self.started += 1
            assert self.release.wait(5)
        return {'version': 1, 'status': 'ok', 'results': []}


async def started(client, count):
    async with asyncio.timeout(2):
        while client.started < count:
            await asyncio.sleep(0.001)


def test_blocked_search_does_not_block_status(tmp_path):
    async def scenario():
        client = SlowClient()
        server = create_mcp(tmp_path, client=client)
        task = asyncio.create_task(server.call_tool('search', {'text': 'slow'}))
        try:
            await started(client, 1)
            result = await asyncio.wait_for(server.call_tool('status', {}), 0.5)
            assert json.loads(result[0].text)['status'] == 'ok'
        finally:
            client.release.set()
            await task
    asyncio.run(scenario())


def test_cancellation_keeps_dispatch_capacity_until_threads_finish(tmp_path):
    async def scenario():
        # Exercise all eight admitted operations simultaneously, independent of
        # the CI host's CPU-derived default executor size (which may be seven).
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=8))
        client = SlowClient()
        server = create_mcp(tmp_path, client=client)
        tasks = [asyncio.create_task(server.call_tool('search', {'text': 'slow'})) for _ in range(8)]
        try:
            await started(client, 8)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            result = await server.call_tool('search', {'text': 'ninth'})
            assert json.loads(result[0].text)['reason'] == 'mcp_busy'
            assert client.started == 8
        finally:
            client.release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(scenario())
