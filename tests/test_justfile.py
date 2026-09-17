"""Exercise laptop recipes against disposable HTTP and cluster stand-ins."""
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('just'), 'just is not installed')
class LaptopRecipes(unittest.TestCase):
    def test_requests_keep_arguments_literal_and_reject_invalid_input(self):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.path)
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            env = dict(os.environ, COGBENCH_CONTROL_URL='http://127.0.0.1:%d' % server.server_port)
            def run(*args):
                return subprocess.run(['just', '--justfile', str(ROOT / 'justfile'), *args],
                                      env=env, capture_output=True, text=True, timeout=10)
            try:
                name = 'model with spaces; $(not-a-command) & "quotes"'
                self.assertEqual(run('model', name).returncode, 0)
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(requests[-1]).query)
                self.assertEqual(query, {'model': [name]})
                self.assertEqual(run('start', '200', 'heuristic').returncode, 0)
                self.assertIn('decisions=200', requests[-1])
                self.assertEqual(run('loop', 'off').returncode, 0)
                self.assertIn('loop=false', requests[-1])
                count = len(requests)
                self.assertNotEqual(run('start', '-1').returncode, 0)
                self.assertNotEqual(run('loop', 'maybe').returncode, 0)
                self.assertEqual(len(requests), count)
            finally:
                server.shutdown()
                thread.join(timeout=2)

    def test_build_and_rollout_use_separate_explicit_contexts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'k8s/image').mkdir(parents=True)
            shutil.copy(ROOT / 'k8s/build.sh', root / 'k8s/build.sh')
            for source in ROOT.glob('*.py'):
                (root / source.name).touch()
            (root / 'actions.json').write_text('{}')
            (root / 'stream').mkdir()
            (root / 'stream/watch.html').touch()
            dex = root / 'cog-minder/src/json'
            dex.mkdir(parents=True)
            for name in ('bots.json', 'items.json', 'machine_hacks.json'):
                (dex / name).write_text('{}')
            (root / 'k8s/deployment.yaml').write_text('  image: example/old:tag\n')
            binary = root / 'bin'
            binary.mkdir()
            stub = '''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['CALL_LOG'], 'a') as f:
    f.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + '\\n')
if 'get' in sys.argv:
    print(json.dumps({'items':[{'metadata':{'name':'builder-current'},'status':{'conditions':[{'type':'Ready','status':'True'}]}}]}))
'''
            for name in ('kubectl', 'buildctl'):
                path = binary / name
                path.write_text(stub)
                path.chmod(0o755)
            log = root / 'calls'
            env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                       CALL_LOG=str(log), COGBENCH_CONTEXT='workload-cluster',
                       COGBENCH_BUILD_CONTEXT='builder-cluster', COGBENCH_NAMESPACE='cogbench',
                       COGBENCH_IMAGE='example/cogbench', COGBENCH_BUILD_NAMESPACE='buildkit',
                       COGBENCH_BUILDKIT='')
            for rollout in ('1', '0'):
                log.write_text('')
                env['COGBENCH_NO_ROLLOUT'] = rollout
                result = subprocess.run(['sh', str(root / 'k8s/build.sh'), 'test-tag'],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(calls[0][1:3], ['--context', 'builder-cluster'])
                build = next(c for c in calls if c[0] == 'buildctl')
                self.assertIn('context=builder-cluster', build[2])
                self.assertIn('builder-current', build[2])
                applies = [c for c in calls if 'apply' in c]
                self.assertEqual(len(applies), 0 if rollout == '1' else 1)
                if applies:
                    self.assertEqual(applies[0][1:3], ['--context', 'workload-cluster'])
