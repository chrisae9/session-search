"""Capture-to-interface qualification of default Codex thread-tree exclusion."""

import asyncio
import json

import pytest

from session_search.capture.local import capture_file
from session_search.core.embeddings import EmbeddingIdentity
from session_search.interfaces.cli import main
from session_search.interfaces.mcp import create_mcp
from session_search.storage.catalog import Catalog
from session_search.storage.semantic import index_pending


@pytest.mark.parametrize('semantic', [False, True])
@pytest.mark.parametrize('remote', [False, True])
def test_captured_current_tree_is_excluded_in_cli_and_mcp(
    tmp_path, monkeypatch, capsys, semantic, remote,
):
    current, child, grandchild, past = [f'00000000-0000-4000-8000-{i:012d}' for i in range(4)]
    root = tmp_path / 'catalog'

    class Provider:
        identity = EmbeddingIdentity('synthetic-thread-exclusion', dimensions=2)

        def embed(self, text, *, query=False):
            return [1, 0]

    provider = Provider() if semantic else None
    with Catalog(root) as catalog:
        for sid, parent in [(current, None), (child, current), (grandchild, child), (past, None)]:
            metadata = {'id': sid, 'cwd': '/synthetic/project'}
            if parent:
                metadata.update(parent_thread_id=parent, thread_source='subagent',
                                source={'subagent': {'thread_spawn': {'parent_thread_id': parent}}})
            rows = [
                {'type': 'session_meta', 'payload': metadata},
                {'type': 'response_item', 'timestamp': '2026-01-01T12:00:00Z', 'payload': {
                    'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': 'recovery pattern'}]}},
            ]
            path = tmp_path / f'{sid}.jsonl'
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            assert capture_file(catalog, path, 'device')['status'] == 'captured'
        if provider:
            index_pending(catalog, provider)

    monkeypatch.setenv('CODEX_THREAD_ID', current)
    monkeypatch.setattr('session_search.core.embeddings.load_provider',
                        lambda *a, **kw: None if remote else provider)
    client, remote_flags, query_root = None, [], root
    if remote:
        from fastapi.testclient import TestClient
        from session_search.interfaces.client import Client
        from session_search.interfaces.credentials import update_device
        from session_search.interfaces.server import create_app
        credentials, token = tmp_path / 'devices.json', tmp_path / 'token'
        update_device(credentials, 'device', token)
        http = TestClient(create_app(root, credentials, provider=provider))
        client = Client('http://localhost:8765', token)

        def request(endpoint, route, payload):
            response = http.post(route, json=payload,
                                 headers={'Authorization': 'Bearer ' + token.read_text().strip()})
            assert response.status_code == 200
            return response.json()

        monkeypatch.setattr(client, '_request', request)
        monkeypatch.setattr('session_search.interfaces.cli.Client', lambda *a, **kw: client)
        remote_flags = ['--primary', client.primary, '--token-file', str(token)]
        query_root = tmp_path / 'uninitialized-client'
    text = 'restore' if semantic else 'recovery'
    scenarios = [([], {}, {past}),
                 (['--include-current-session'], {'include_current_session': True},
                  {current, child, grandchild, past}),
                 (['--include-current-session', '--exclude-session', past],
                  {'include_current_session': True, 'exclude_sessions': [past]},
                  {current, child, grandchild})]
    for flags, options, expected in scenarios:
        assert main(['--data-dir', str(query_root), *remote_flags,
                     'search', text, '--include-subagents', *flags]) == 0
        cli = json.loads(capsys.readouterr().out)

        async def query():
            mcp = create_mcp(query_root, client=client, provider=None if remote else provider)
            response = await mcp.call_tool('search', {
                'text': text, 'include_subagents': True, **options,
            })
            return json.loads(response[0].text)

        for result in (cli, asyncio.run(query())):
            assert result['mode'] == ('hybrid' if semantic else 'keyword')
            assert {hit['citation']['session_id'] for hit in result['results']} == expected
    if remote:
        http.close()
        assert not query_root.exists()
