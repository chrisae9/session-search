"""Exercise the public local CLI with only the Python standard library available."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys


def test_local_core_without_network_or_optional_packages(tmp_path):
    result = subprocess.run(
        [sys.executable, '-I', '-S', '-B', str(Path(__file__).resolve()),
         str(Path(__file__).resolve().parents[1] / 'src'), str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'status': 'verified', 'network_attempts': 0}


def isolated_scenario(source, root):
    import importlib.util
    import os
    import socket

    attempts = []

    def deny_network(event, args):
        if event.startswith('socket.') or event in {'subprocess.Popen', 'os.system', 'os.exec'}:
            attempts.append(event)
            raise RuntimeError('network and external processes disabled by isolation test')

    sys.addaudithook(deny_network)
    try:
        socket.socket()
    except RuntimeError:
        pass
    else:
        raise AssertionError('isolation guard was not active')
    assert attempts
    attempts.clear()
    sys.path.insert(0, source)
    for package in ('numpy', 'llama_cpp', 'fastapi', 'mcp'):
        assert importlib.util.find_spec(package) is None
    os.environ.update(HOME=str(root), CODEX_HOME=str(root / 'codex'),
                      XDG_DATA_HOME=str(root / 'data'), PATH='')
    os.environ.pop('CODEX_THREAD_ID', None)
    from session_search.interfaces.cli import main

    def run(*args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert main(['--data-dir', str(root / 'catalog'), *args]) == 0
        return json.loads(output.getvalue())

    sessions = root / 'codex' / 'sessions'
    sessions.mkdir(parents=True)
    path = sessions / 'offline-example.jsonl'
    rows = [
        {'type': 'session_meta', 'payload': {'id': 'offline-example', 'cwd': '/synthetic'}},
        {'type': 'response_item', 'timestamp': '2026-01-01T12:00:00Z', 'payload': {
            'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'Offline learned recovery pattern'}]}},
    ]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    run('init')
    assert run('capture', '--producer', 'offline', '--archive-raw')['counts']['captured'] == 1
    citation = run('search', 'recovery')['results'][0]['citation']
    with path.open('a') as output:
        output.write(json.dumps({'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': 'Verify every restored byte'}]}}) + '\n')
    assert run('capture', '--producer', 'offline', '--archive-raw')['counts']['captured'] == 1
    assert run('search', 'restored', '--role', 'assistant')['results']
    old = run('context', json.dumps([citation]), '--neighbors', '0')
    assert old['results'][0]['events'][0]['text'] == 'Offline learned recovery pattern'
    from session_search.core.embeddings import EmbeddingIdentity, LocalEmbedder
    missing = LocalEmbedder(root / 'missing.gguf', EmbeddingIdentity('sha256:' + '0' * 64))
    try:
        missing.embed('recovery', query=True)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError('a missing local model must require explicit provisioning')
    configuration = root / 'local-model.json'
    configuration.write_text(json.dumps({'mode': 'local', 'model_path': str(root / 'missing.gguf'),
                                        'identity': {'artifact': 'sha256:' + '0' * 64}}))
    fallback = run('--embedding-config', str(configuration), 'search', 'recovery')
    assert fallback['results'] and fallback['semantic_available'] is False
    path.unlink()
    assert run('capture', '--producer', 'offline')['status'] == 'empty'
    assert run('status')['coverage']['sessions'] == 1
    snapshot = root / 'snapshot'
    assert run('snapshot', str(snapshot))['status'] == 'verified'
    assert run('verify-snapshot', str(snapshot))['status'] == 'verified'
    assert not attempts, attempts
    print(json.dumps({'status': 'verified', 'network_attempts': 0}))


if __name__ == '__main__':
    isolated_scenario(sys.argv[1], Path(sys.argv[2]))
