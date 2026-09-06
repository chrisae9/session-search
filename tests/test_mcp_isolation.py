"""Exercise actual MCP stdio requests while denying network access in the server."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys


def serve(source: str, root: Path):
    import socket
    attempts = []

    def isolated(event, args):
        # asyncio may create an AF_UNIX socketpair for local wakeups. Internet
        # sockets, DNS, connections, and child commands remain forbidden.
        if (event == 'socket.__new__' and args[1] in {socket.AF_INET, socket.AF_INET6}
                or event in {'socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname',
                             'socket.gethostbyaddr', 'socket.getnameinfo',
                             'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn',
                             'os.fork', 'os.forkpty'}):
            attempts.append(event)
            raise RuntimeError('network and child commands disabled for MCP isolation test')

    sys.addaudithook(isolated)
    try:
        socket.socket(socket.AF_INET)
    except RuntimeError:
        pass
    else:
        raise AssertionError('network guard is inactive')
    assert attempts
    attempts.clear()
    sys.path.insert(0, source)
    from session_search.interfaces.mcp import create_mcp
    from session_search.core.embeddings import EmbeddingIdentity, LocalEmbedder
    provider = LocalEmbedder(root / 'missing.gguf', EmbeddingIdentity('sha256:' + '0' * 64))
    try:
        create_mcp(root / 'catalog', provider=provider).run(transport='stdio')
    finally:
        (root / 'network-attempts.json').write_text(json.dumps(attempts))


async def scenario(source: str, root: Path):
    sys.path.insert(0, source)
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from session_search.core.records import Event, SessionRevision
    from session_search.storage.catalog import Catalog
    with Catalog(root / 'catalog') as catalog:
        catalog.ingest(SessionRevision('offline', (Event('one', 'user', 'offline recovery pattern'),)),
                       producer='synthetic', request_id='one')
    server = StdioServerParameters(command=sys.executable,
        args=['-I', str(Path(__file__).resolve()), '--serve', source, str(root)])
    with (root / 'server-stderr.log').open('w') as errors:
        async with stdio_client(server, errlog=errors) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == {'search', 'context', 'status'}
                assert all(tool.annotations.readOnlyHint for tool in listed.tools)

                async def call(name, args):
                    response = await session.call_tool(name, args)
                    assert not response.isError
                    return json.loads(response.content[0].text)

                found = await call('search', {'text': 'recovery'})
                assert found['results'] and found['semantic_available'] is False
                citation = found['results'][0]['citation']
                # An update must not redirect the immutable evidence from a prior hit.
                with Catalog(root / 'catalog') as catalog:
                    catalog.ingest(SessionRevision('offline', (Event('two', 'user', 'later work'),)),
                                   producer='synthetic', request_id='two')
                context = await call('context', {'citations': [citation], 'neighbors': 0})
                assert context['results'][0]['events'][0]['text'] == 'offline recovery pattern'
                assert (await call('search', {'text': 'later', 'literal': True}))['results']
                assert (await call('status', {}))['coverage']['sessions'] == 1
    assert json.loads((root / 'network-attempts.json').read_text()) == []
    print(json.dumps({'status': 'verified', 'network_attempts': 0, 'tools': 3}))


def test_local_mcp_stdio_without_network(tmp_path):
    result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--scenario',
        str(Path(__file__).resolve().parents[1] / 'src'), str(tmp_path)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'status': 'verified', 'network_attempts': 0, 'tools': 3}


if __name__ == '__main__':
    if sys.argv[1] == '--serve':
        serve(sys.argv[2], Path(sys.argv[3]))
    else:
        asyncio.run(scenario(sys.argv[2], Path(sys.argv[3])))
