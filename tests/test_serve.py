"""Interactive repo scans run locally. HTTP tests use loopback only."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from serve import DemoServer, ScanManager, repo_name


class DemoServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'viewer.html').write_text('<html>viewer</html>')
        (self.root / '.env').write_text('private')
        self.scans = ScanManager(self.root)
        self.server = DemoServer(('127.0.0.1', 0), self.scans)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        request_headers = {'Content-Type': 'application/json', **(headers or {})}
        connection.request(method, path, json.dumps(body) if body is not None else None, request_headers)
        response = connection.getresponse()
        content = response.read()
        connection.close()
        return response.status, content

    def test_idle_and_static_viewer(self):
        status, content = self.request('GET', '/api/scan')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content), {'state': 'idle'})
        self.assertEqual(self.request('GET', '/')[1], b'<html>viewer</html>')

    def test_private_files_and_parent_paths_are_hidden(self):
        for path in ('/.env', '/%2eenv', '/repo_twin.py', '/../.env', '/fixtures/../../.env'):
            self.assertEqual(self.request('GET', path)[0], 404, path)

    def test_cross_origin_and_wrong_hosts_cannot_start_scan(self):
        body = {'repo': 'pallets/flask'}
        self.assertEqual(self.request('POST', '/api/scan', body, {'Origin': 'https://attacker.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/scan', body, {'Host': 'attacker.example'})[0], 403)
        self.assertEqual(self.scans.snapshot()['state'], 'idle')

    def test_invalid_input_cannot_start_scan(self):
        for body in ({'repo': 'owner/repo;echo hi'}, {'repo': 'x/y', 'mode': 'other'},
                     {'repo': 'x/y', 'offline': 'yes'}, [], {}):
            self.assertEqual(self.request('POST', '/api/scan', body)[0], 400, body)
        self.assertEqual(self.scans.snapshot()['state'], 'idle')

    def test_duplicate_scan_is_rejected(self):
        with self.scans.lock:
            self.scans.job = {'id': 'busy', 'state': 'running'}
        self.assertEqual(self.request('POST', '/api/scan', {'repo': 'pallets/flask'})[0], 409)

    def test_scan_command_completes_and_publishes_progress(self):
        script = '''import json, os, sys
from pathlib import Path
repo = sys.argv[sys.argv.index('--repo') + 1]
assert '--repo-offline' in sys.argv
assert os.environ['USE_ARCHIL'] == '0'
assert os.environ['AGENT_BRAIN'] == 'offline'
print('REPO: ' + repo + ' (fixture)', flush=True)
print('ROUND 0 [EXPLORE]', flush=True)
Path('results.json').write_text(json.dumps({'repo': {'name': repo, 'source': 'fixture'}, 'mode': 'local'}))
'''
        (self.root / 'run_airlock.py').write_text(script)
        status, content = self.request('POST', '/api/scan', {'repo': 'https://github.com/pallets/flask', 'offline': True})
        self.assertEqual(status, 202)
        job_id = json.loads(content)['id']
        deadline = time.monotonic() + 5
        while self.scans.snapshot()['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.02)
        final = self.scans.snapshot()
        self.assertEqual(final['id'], job_id)
        self.assertEqual(final['state'], 'complete', final)
        self.assertEqual(final['result_mode'], 'local')
        self.assertEqual(final['repo_info']['source'], 'fixture')
        self.assertIn('ROUND 0 [EXPLORE]', final['log'])
        self.assertEqual(self.request('GET', '/results.json')[0], 200)

    def test_failed_scan_preserves_previous_result(self):
        original = '{"previous": true}'
        (self.root / 'results.json').write_text(original)
        (self.root / 'run_airlock.py').write_text('raise SystemExit(1)\n')
        self.scans.start('missing/repo', 'local', False)
        deadline = time.monotonic() + 5
        while self.scans.snapshot()['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(self.scans.snapshot()['state'], 'failed')
        self.assertEqual((self.root / 'results.json').read_text(), original)

    def test_repo_name_accepts_urls_and_rejects_command_injection(self):
        for value in ('pallets/flask', ' https://github.com/Pallets/Flask.git '):
            self.assertEqual(repo_name(value), 'pallets/flask')
        for value in ('https://evil.example/a/b', 'x/y;ls', '../repo', 'a/..', 'a/b?key=x'):
            with self.assertRaises(ValueError):
                repo_name(value)


if __name__ == '__main__':
    unittest.main()
