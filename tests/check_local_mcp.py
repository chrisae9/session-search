"""Qualify fresh-process semantic MCP with an installed model and OS network denial."""

import argparse
import asyncio
import errno
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time


def require_network_denial():
    with socket.socket() as probe:
        try:
            probe.connect(('127.0.0.1', 9))
        except OSError as exc:
            assert exc.errno in {errno.EPERM, errno.EACCES}
        else:
            raise AssertionError('OS networking must be denied')


def provider(args):
    from session_search.core.embeddings import EmbeddingIdentity, LocalEmbedder
    return LocalEmbedder(args.model, EmbeddingIdentity('sha256:' + args.sha256))


def prepare(args):
    from session_search.core.records import Event, SessionRevision
    from session_search.storage.catalog import Catalog
    from session_search.storage.semantic import index_pending
    with Catalog(args.root) as catalog:
        for sid, text in [('recovery', 'Restore the backup snapshot to replace a lost workstation.'),
                          ('food', 'The cake recipe uses flour, sugar, and butter.')]:
            catalog.ingest(SessionRevision(sid, (Event('one', 'user', text),)),
                           producer='synthetic', request_id=sid)
        assert index_pending(catalog, provider(args), limit=100)['failed'] == 0


async def scenario(args):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    command = [sys.executable, '-I', str(Path(__file__).resolve()), str(args.model), args.sha256]
    if args.short_timeout:
        command.append('--short-timeout')
    with tempfile.TemporaryDirectory(prefix='session-search-native-mcp-') as temporary:
        root = Path(temporary) / 'catalog'
        with (Path(temporary) / 'native.log').open('w') as errors:
            subprocess.run([*command, '--prepare', '--root', str(root)], stderr=errors,
                           stdout=subprocess.DEVNULL, check=True, timeout=120)
            params = StdioServerParameters(command=command[0],
                args=[*command[1:], '--serve', '--root', str(root)])
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    async def call(name, arguments):
                        result = await asyncio.wait_for(session.call_tool(name, arguments), timeout=30)
                        assert not result.isError
                        return json.loads(result.content[0].text)

                    started = time.monotonic()
                    cold = asyncio.create_task(call('search', {'text': 'recover a missing workstation'}))
                    await asyncio.sleep(0.05)
                    literal_started = time.monotonic()
                    literal = await call('search', {'text': 'snapshot', 'literal': True})
                    literal_seconds = time.monotonic() - literal_started
                    assert literal['results']
                    found = await cold
                    cold_seconds = time.monotonic() - started
                    fallback_observed = found.get('degraded', False)
                    if args.short_timeout:
                        assert fallback_observed and found['degradation'] == 'TimeoutError'
                        async with asyncio.timeout(10):
                            while found.get('semantic_available') is not True:
                                await asyncio.sleep(0.05)
                                found = await call('search', {'text': 'recover a missing workstation'})
                    assert found['semantic_available'] is True
                    assert found['results'][0]['citation']['session_id'] == 'recovery'
                    warm_seconds = None
                    if not args.short_timeout:
                        warm_started = time.monotonic()
                        warm = await call('search', {'text': 'recover a missing workstation'})
                        warm_seconds = time.monotonic() - warm_started
                        assert warm['results'][0]['citation'] == found['results'][0]['citation']
                    context = await call('context', {'citations': [found['results'][0]['citation']]})
                    assert context['results'][0]['events']
                    assert (await call('status', {}))['coverage']['sessions'] == 2
    print(json.dumps({'status': 'verified', 'network_denied_by_os': True,
                      'fresh_process_semantic_seconds': round(cold_seconds, 3),
                      'concurrent_literal_seconds': round(literal_seconds, 3),
                      'warm_semantic_seconds': round(warm_seconds, 3) if warm_seconds is not None else None,
                      'forced_timeout_fallback_recovered': args.short_timeout and fallback_observed}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('model', type=Path)
    parser.add_argument('sha256')
    parser.add_argument('--short-timeout', action='store_true')
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--root', type=Path)
    args = parser.parse_args()
    require_network_denial()
    if args.prepare:
        prepare(args)
    elif args.serve:
        from session_search.interfaces.mcp import create_mcp
        model = provider(args)
        if args.short_timeout:
            from session_search.core.local_worker import IsolatedLocalEmbedder
            model = IsolatedLocalEmbedder(model, timeout=0.001)
        create_mcp(args.root, provider=model).run(transport='stdio')
    else:
        asyncio.run(scenario(args))
