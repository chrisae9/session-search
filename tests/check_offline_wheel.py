"""Install the built core wheel offline, then exercise it without optional packages."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile


if __name__ == '__main__':
    wheels = list(Path(sys.argv[1]).resolve().glob('*.whl'))
    if len(wheels) != 1:
        raise SystemExit('provide a distribution directory containing exactly one wheel')
    with tempfile.TemporaryDirectory(prefix='session-search-offline-wheel-') as temporary:
        root = Path(temporary)
        environment = root / 'environment'
        subprocess.run(['uv', '--offline', 'venv', '--python', sys.executable, str(environment)], check=True)
        python = environment / 'bin' / 'python'
        subprocess.run(['uv', '--offline', 'pip', 'install', '--python', str(python),
                        '--no-index', str(wheels[0])], check=True)
        library = subprocess.check_output(
            [str(python), '-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True,
        ).strip()
        scenario = root / 'scenario'
        scenario.mkdir()
        result = subprocess.run(
            [str(python), '-I', '-S', '-B', str(Path(__file__).with_name('test_local_isolation.py').resolve()),
             library, str(scenario)], capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise SystemExit(result.stderr)
        assert json.loads(result.stdout) == {'status': 'verified', 'network_attempts': 0}
        print('Offline core wheel installation and isolated CLI scenario passed')
