import asyncio
import hashlib
import json
import threading

import httpx
import pytest

from session_search.interfaces.server import SearchBusy, SearchPool, create_app
from session_search.storage.catalog import Catalog


def test_cancelled_call_holds_slot_until_worker_finishes():
    async def scenario():
        pool = SearchPool(1)
        entered, release = threading.Event(), threading.Event()

        def work():
            entered.set()
            assert release.wait(5)
            raise ValueError('synthetic worker failure')

        task = asyncio.create_task(pool.run(work))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(SearchBusy):
                await pool.run(lambda: 'must not start')
            assert len(pool.tasks) == 1
        finally:
            release.set()
            await asyncio.gather(*pool.tasks, return_exceptions=True)
        assert await pool.run(lambda: 'recovered') == 'recovered'
        assert not pool.tasks

    asyncio.run(scenario())


@pytest.mark.parametrize('cancel_caller', [False, True])
def test_server_overload_is_retryable_and_status_remains_available(
    tmp_path, monkeypatch, cancel_caller,
):
    from session_search.storage import semantic
    entered, release = threading.Event(), threading.Event()

    def slow(catalog, query, provider):
        entered.set()
        assert release.wait(5)
        return catalog.search(query)

    monkeypatch.setattr(semantic, 'hybrid_search', slow)
    with Catalog(tmp_path / 'data'):
        pass
    credentials = tmp_path / 'credentials.json'
    credentials.write_text(json.dumps({'test': hashlib.sha256(b'token').hexdigest()}))
    app = create_app(tmp_path / 'data', credentials, search_workers=1)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://test',
                                     headers={'Authorization': 'Bearer token'}) as client:
            task = asyncio.create_task(client.post('/v1/search', json={'text': 'slow'}))
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set():
                        await asyncio.sleep(0.001)
                busy = await asyncio.wait_for(client.post('/v1/search', json={'text': 'next'}), 1)
                assert busy.status_code == 503
                assert busy.json()['reason'] == 'search_busy'
                assert busy.headers['retry-after'] == '1'
                status = await asyncio.wait_for(client.get('/v1/status'), 1)
                assert status.status_code == 200
                if cancel_caller:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    busy = await client.post('/v1/search', json={'text': 'still busy'})
                    assert busy.status_code == 503
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            async with asyncio.timeout(2):
                while True:
                    response = await client.post('/v1/search', json={'text': 'next'})
                    if response.status_code != 503:
                        assert response.status_code == 200
                        break
                    await asyncio.sleep(0.001)

    asyncio.run(scenario())


@pytest.mark.parametrize('workers', [0, -1, 65, True, 1.5])
def test_invalid_pool_size(workers):
    with pytest.raises(ValueError):
        SearchPool(workers)
