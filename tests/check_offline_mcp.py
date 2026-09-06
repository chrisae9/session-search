"""Install a prepared MCP wheel bundle offline and exercise the real stdio server."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile


if __name__ == '__main__':
    wheels = list(Path(sys.argv[1]).resolve().glob('*.whl'))
    if len(wheels) != 1:
        raise SystemExit('provide a distribution directory containing exactly one application wheel')
    wheelhouse = Path(sys.argv[2]).resolve()
    if not wheelhouse.is_dir():
        raise SystemExit('provide a pre-provisioned dependency wheelhouse for this platform and Python')
    with tempfile.TemporaryDirectory(prefix='session-search-offline-mcp-') as temporary:
        root = Path(temporary)
        environment = root / 'environment'
        subprocess.run(['uv', '--offline', 'venv', '--python', sys.executable, str(environment)], check=True)
        python = environment / 'bin' / 'python'
        subprocess.run(['uv', '--offline', '--no-cache', 'pip', 'install', '--python', str(python),
                        '--no-index', '--find-links', str(wheelhouse), str(wheels[0]) + '[mcp]'], check=True)
        library = subprocess.check_output([str(python), '-I', '-c',
            'import sysconfig,importlib.util; assert importlib.util.find_spec("numpy") is None; '
            'assert importlib.util.find_spec("llama_cpp") is None; '
            'print(sysconfig.get_path("purelib"))'], text=True).strip()
        scenario = root / 'scenario'
        scenario.mkdir()
        result = subprocess.run([str(python), '-I',
            str(Path(__file__).with_name('test_mcp_isolation.py').resolve()),
            '--scenario', library, str(scenario)], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise SystemExit(result.stderr)
        assert json.loads(result.stdout) == {'status': 'verified', 'network_attempts': 0, 'tools': 3}
        print('Offline MCP wheel installation and isolated stdio scenario passed')
