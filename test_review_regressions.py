"""Real coordinator/runner entrypoints, isolated executable transport only."""
import json
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import collab
import runner
import test_runner


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runner.RunnerBehaviorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.launches = []
        self.c = collab.Collaboration(self.fixture.store.path,
            launch=lambda t: self.launches.append(t.copy()))
        self.client = self.c.connect('pi')['client_id']

    def start(self, prompt='BLOCK'):
        return self.c.start(self.client, prompt=prompt, cwd=str(self.fixture.root), timeout=10, write=True)

    def test_main_wait_deadline_includes_real_constructor(self):
        from contextlib import redirect_stdout
        from io import StringIO
        task = self.start('x')
        original = collab.Collaboration
        def slow_constructor(*args, **kwargs):
            instance = original(*args, **kwargs)
            time.sleep(.3)
            return instance
        output = StringIO()
        started = time.monotonic()
        with patch.object(collab, 'Collaboration', side_effect=slow_constructor), redirect_stdout(output):
            rc = collab.main(['--db', str(self.c.store.path), '--client', self.client,
                'wait', '--task', task['id'], '--timeout', '.25'])
        self.assertEqual(rc, 0, output.getvalue())
        self.assertLess(time.monotonic()-started, .5)
        self.assertEqual(json.loads(output.getvalue())['task']['state'], 'queued')

    def test_missing_creation_evidence_cannot_skip_close(self):
        from types import SimpleNamespace
        task = self.start('x')
        self.c.cancel(self.client, task['id'])
        with self.c.store.connect() as db:
            current = self.c.store._owned(db, task['id'], self.c.origin(self.client))
            del current['session_creation_attempted']
            self.c.store._save(db, current)
        calls = []
        self.c.control = lambda t, a, **kw: calls.append(a) or SimpleNamespace(returncode=1)
        self.assertEqual(self.c.close(self.client, task['id'])['state'], 'unknown')
        self.assertEqual(calls, ['close'])

    def test_close_requires_status_readback_no_session(self):
        for mode in ('running', 'transport', 'malformed', 'no-session'):
            with self.subTest(mode=mode), patch.dict(os.environ, ACPX_STATUS_MODE=mode):
                task = self.c.start(self.client, prompt='done', cwd=str(self.fixture.root/mode), timeout=10)
                self.assertEqual(runner.run_task(self.c.store.path, task['id']), 0)
                result = self.c.close(self.client, task['id'])
                self.assertEqual(result['state'], 'closed' if mode == 'no-session' else 'unknown')
                calls = list(map(json.loads, (self.fixture.root/'calls.jsonl').read_text().splitlines()))
                owned = [c for c in calls if task['session'] in c]
                self.assertIn('close', owned[-2])
                self.assertIn('status', owned[-1])

    def test_cancel_during_version_check_can_close_without_provider(self):
        task = self.start('x')
        def version(store, current):
            self.c.cancel(self.client, task['id'])
            return {'decision': 'current'}
        with patch('updates.ensure_current', side_effect=version):
            self.assertEqual(runner.run_task(self.c.store.path, task['id']), 0)
        result = self.c.close(self.client, task['id'])
        self.assertEqual(result['state'], 'closed')
        self.assertFalse((self.fixture.root/'calls.jsonl').exists())
        self.assertIs(result['session_creation_attempted'], False)

    def test_creation_attempt_is_durable_before_transport_and_uncertainty_not_skipped(self):
        task = self.start('x')
        observed = []
        def uncertain(current, action, **kw):
            observed.append(self.c.status(self.client, task['id']).get('session_creation_attempted'))
            raise TimeoutError('creation result lost')
        with patch('runner.control', side_effect=uncertain):
            self.assertEqual(runner.run_task(self.c.store.path, task['id']), 1)
        self.assertEqual(observed, [True])
        self.assertEqual(self.c.status(self.client, task['id'])['state'], 'unknown')
        from types import SimpleNamespace
        calls = []
        self.c.control = lambda t, a, **kw: calls.append(a) or SimpleNamespace(returncode=1)
        self.assertEqual(self.c.close(self.client, task['id'])['state'], 'unknown')
        self.assertEqual(calls, ['close'])

    def test_main_wait_exclusive_lock_is_nonblocking_from_initialization(self):
        import subprocess
        import sys
        import threading
        task = self.start('x')
        ready = threading.Event()
        release = threading.Event()
        def hold():
            with self.c.store.connect() as db:
                db.execute('BEGIN EXCLUSIVE')
                ready.set()
                release.wait(2)
        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(ready.wait(2))
        try:
            started = time.monotonic()
            result = subprocess.run([sys.executable, '-B', collab.__file__, '--db', str(self.c.store.path),
                '--client', self.client, 'wait', '--task', task['id'], '--after', '37', '--timeout', '0'],
                capture_output=True, text=True, timeout=4)
            elapsed = time.monotonic()-started
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertLess(elapsed, .8)
            self.assertEqual(json.loads(result.stdout), dict(task=None, events=[], after=37, busy=True))
        finally:
            release.set()
            thread.join()

    def test_cancel_acceptance_requires_valid_prompt_settlement(self):
        for mode in ('exit255', 'eof', 'failed', 'cancelled', 'end_turn'):
            with self.subTest(mode=mode), patch.dict(os.environ, ACPX_CANCEL_MODE=mode):
                task = self.c.start(self.client, prompt='BLOCK', cwd=str(self.fixture.root/mode), timeout=10, write=True)
                tid = task['id']
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(runner.run_task, self.c.store.path, tid)
                    deadline = time.monotonic()+3
                    while time.monotonic()<deadline:
                        calls = self.fixture.root/'calls.jsonl'
                        if calls.exists() and any('prompt' in c and task['session'] in c for c in map(json.loads, calls.read_text().splitlines())):
                            break
                        time.sleep(.01)
                    else:
                        self.c.cancel(self.client, tid)
                        self.fail('fake prompt did not start')
                    self.c.intervene(self.client, tid, 'next', command_id='human')
                    rc = future.result(timeout=5)
                valid = mode in {'cancelled', 'end_turn'}
                self.assertEqual(rc, 0 if valid else 1)
                launches = len(self.launches)
                result = self.c.wait(self.client, tid, timeout=0)['task']
                self.assertEqual(result['turn'], 2 if valid else 1)
                self.assertEqual(result['state'], 'queued' if valid else 'unknown')
                self.assertEqual(len(self.launches), launches + int(valid))
                if not valid:
                    self.assertEqual(result['intervention']['status'], 'pending')
                    with self.assertRaises(ValueError):
                        self.c.start(self.client, prompt='conflict', cwd=task['cwd'], write=True)
