import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def test_remote_cli_read_timeout_is_configurable(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(0.25)
            payload = json.dumps({'version': 1, 'status': 'ok', 'server_role': 'primary'}).encode()
            try:
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    token = tmp_path / 'token'
    token.write_text('synthetic')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    command = [sys.executable, '-m', 'session_search.interfaces.cli',
               '--data-dir', str(tmp_path / 'client'),
               '--primary', f'http://127.0.0.1:{server.server_port}', '--token-file', str(token)]
    try:
        short = subprocess.run([*command, '--read-timeout', '0.05', 'status'],
                               capture_output=True, text=True, timeout=5)
        assert json.loads(short.stdout)['status'] == 'unavailable'
        longer = subprocess.run([*command, '--read-timeout', '2', 'status'],
                                capture_output=True, text=True, timeout=5)
        assert longer.returncode == 0
        assert json.loads(longer.stdout)['server_role'] == 'primary'
        invalid = subprocess.run([*command, '--read-timeout', '0', 'status'],
                                 capture_output=True, text=True, timeout=5)
        assert invalid.returncode != 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
