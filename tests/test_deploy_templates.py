"""Exercise shipped local capture commands against fictional history."""
import json
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize('platform', ['launchd', 'systemd'])
def test_local_scheduler_command_captures_searchable_history(platform, tmp_path):
    if platform == 'launchd':
        config = plistlib.loads((ROOT / 'deploy/launchd/session-search-local-sync.plist').read_bytes())
        command = config['ProgramArguments']
        assert config['StartInterval'] == 300 and config['RunAtLoad']
    else:
        service = (ROOT / 'deploy/systemd/session-search-local-sync.service').read_text()
        command = shlex.split(next(line.split('=', 1)[1] for line in service.splitlines()
                                   if line.startswith('ExecStart=')))
        timer = (ROOT / 'deploy/systemd/session-search-local-sync.timer').read_text()
        assert 'OnUnitInactiveSec=5min' in timer
    values = {
        '/absolute/local/data': str(tmp_path), '${SESSION_SEARCH_DATA}': str(tmp_path),
        '/absolute/codex/home': str(ROOT / 'examples/codex-home'),
        '${SESSION_SEARCH_CODEX_HOME}': str(ROOT / 'examples/codex-home'),
        '${SESSION_SEARCH_PRODUCER}': 'synthetic', '${SESSION_SEARCH_CAPTURE_BYTES}': '10485760',
        '${SESSION_SEARCH_RESERVE_BYTES}': '67108864', '2147483648': '67108864',
    }
    args = [values.get(arg, arg) for arg in command[1:]]
    assert not {'--primary', '--token-file', '--archive-raw', '--chunk-raw'} & set(args)
    cli = [sys.executable, '-m', 'session_search.interfaces.cli']
    result = json.loads(subprocess.check_output([*cli, *args], text=True, timeout=30))
    assert result['status'] == 'complete' and result['capture']['counts']['captured'] == 1
    assert json.loads((tmp_path / 'sync-status.json').read_text())['status'] == 'complete'
    result = json.loads(subprocess.check_output([
        *cli, '--data-dir', str(tmp_path), 'search', 'duplicate requests', '--literal'], text=True, timeout=30))
    citation = result['results'][0]['citation']
    context = json.loads(subprocess.check_output([
        *cli, '--data-dir', str(tmp_path), 'context', json.dumps([citation])], text=True, timeout=30))
    assert context['results'][0]['status'] == 'ok'
