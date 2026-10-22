import json
import tempfile
import unittest
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
import collab


class CollaborationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name)/'tasks.sqlite3'
        self.launches = []
        self.controls = []
        self.c = collab.Collaboration(self.db, launch=lambda task: self.launches.append(task.copy()),
            control=lambda task, action, **kw: self.controls.append(action) or SimpleNamespace(
                returncode=0, stdout=b'{"action":"status_snapshot","status":"no-session","summary":"no active session"}'),
            lock=lambda task, **kw: nullcontext(True))

    def test_start_status_events_and_owner_isolation(self):
        a = self.c.connect('omp')['client_id']
        b = self.c.connect('pi')['client_id']
        task = self.c.start(a, prompt='inspect', cwd='/project', timeout=30)
        self.assertEqual(len(self.launches), 1)
        self.assertTrue(task['needs_session'])
        self.assertFalse(task['acpx_created'])
        self.assertEqual(task['timeout'], 30)
        self.assertEqual(self.c.status(a, task['id'])['state'], 'queued')
        self.c.store.event(task['id'], 'stage1', 'stage', 'checked')
        self.assertEqual(self.c.events(a, task['id'])[0]['text'], 'checked')
        self.assertEqual(self.c.list(b), [])
        self.assertEqual(len(self.c.list(a)), 1)
        with self.assertRaises(PermissionError): self.c.status(b, task['id'])
        with self.assertRaises(PermissionError): self.c.events(b, task['id'])
        with self.assertRaises(ValueError): self.c.start(a, prompt='x', cwd='/project', timeout=0)
        self.c.start(a, prompt='write', cwd='/project', write=True)
        with self.assertRaises(ValueError): self.c.start(b, prompt='write', cwd='/project/sub', write=True)

    def test_hermes_can_use_generic_endpoint_without_native_injection(self):
        try:client=self.c.connect('hermes')
        except ValueError:self.fail('Generic endpoint must accept the actual Hermes caller without native injection')
        task=self.c.start(client['client_id'],prompt='isolated task',cwd='/project')
        self.assertEqual(task['origin']['platform'],'hermes')
        self.assertEqual(task['origin']['profile'],'standalone')
        self.assertEqual(task['origin']['chat_id'],'')

    def test_invalid_execution_paths_are_rejected_before_launch(self):
        client=self.c.connect('pi')['client_id']
        for cwd in ['relative/project','C:/Program Files/Git/Users/example/project']:
            with self.assertRaises(ValueError):self.c.start(client,prompt='x',cwd=cwd)
        self.assertEqual(self.launches,[])

    def test_default_worker_launch_is_bounded_runner_without_logs(self):
        from unittest.mock import patch
        import subprocess
        import sys
        c = collab.Collaboration(self.db)
        client = c.connect('pi')['client_id']
        with patch('subprocess.Popen') as popen:
            task = c.start(client, prompt='inspect', cwd='/project', timeout=15)
        args, kw = popen.call_args
        self.assertEqual(args[0], [sys.executable, str(Path(collab.__file__).with_name('runner.py')), '--db', str(self.db), '--task', task['id']])
        self.assertEqual(kw['stdout'], subprocess.DEVNULL)
        self.assertEqual(kw['stderr'], subprocess.DEVNULL)
        self.assertEqual(kw['stdin'], subprocess.DEVNULL)
        self.assertTrue(kw.get('start_new_session',False))

    def test_intervention_cancels_then_wait_resumes_same_session_once(self):
        client = self.c.connect('omp')['client_id']
        task = self.c.start(client, prompt='old', cwd='/project')
        tid = task['id']
        self.c.store.update(tid, state='running', acpx_created=True, needs_session=False)
        result = self.c.intervene(client, tid, 'change direction', command_id='human-1')
        self.assertEqual(result['intervention']['status'], 'pending')
        self.assertEqual(result['state'], 'cancelling')
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.c.wait(client, tid, timeout=0)['task']['state'], 'cancelling')
        self.c.store.finish_turn(tid, 1, 'cancelled', 0, 'cancelled')
        result = self.c.wait(client, tid, timeout=0)['task']
        self.assertEqual((result['turn'], result['state'], result['prompt']), (2, 'queued', 'change direction'))
        self.assertEqual(result['session'], task['session'])
        self.assertFalse(result['needs_session'])
        self.assertEqual(len(self.launches), 2)
        self.c.intervene(client, tid, 'change direction', command_id='human-1')
        self.c.wait(client, tid, timeout=0)
        self.assertEqual(len(self.launches), 2)
        with self.assertRaises(ValueError): self.c.intervene(client, tid, 'different', command_id='human-1')

    def test_queued_intervention_preserves_first_turn_and_unknown_is_not_replayed(self):
        client = self.c.connect('pi')['client_id']
        task = self.c.start(client, prompt='old', cwd='/project')
        result = self.c.intervene(client, task['id'], 'new', command_id='1')
        self.assertEqual((result['turn'], result['prompt']), (1, 'new'))
        self.assertEqual(result['intervention']['status'], 'queued')
        self.assertTrue(result['needs_session'])
        self.assertEqual(len(self.launches), 1)
        self.c.store.update(task['id'], state='unknown')
        with self.assertRaises(ValueError): self.c.intervene(client, task['id'], 'x', command_id='2')
        self.c.wait(client, task['id'], timeout=0)
        self.assertEqual(len(self.launches), 1)

    def test_wait_cursor_and_five_second_cap(self):
        from unittest.mock import patch
        client = self.c.connect('pi')['client_id']
        task = self.c.start(client, prompt='old', cwd='/project')
        self.c.store.event(task['id'], 's', 'stage', 'real stage')
        result = self.c.wait(client, task['id'], timeout=5)
        self.assertEqual(result['events'][0]['text'], 'real stage')
        with patch('time.monotonic', side_effect=[0, 0, 6]), patch('time.sleep'):
            result = self.c.wait(client, task['id'], after=result['after'], timeout=500)
        self.assertEqual(result['events'], [])

    def test_reply_requires_pending_question_and_resumes_only_after_all_answers(self):
        client = self.c.connect('codex')['client_id']
        task = self.c.start(client, prompt='inspect', cwd='/project')
        tid = task['id']
        self.c.store.update(tid, state='completed', needs_session=False, acpx_created=True)
        with self.assertRaises(ValueError): self.c.reply(client, tid, 'invented', 'answer')
        self.c.store.event(tid, 'q1', 'question', 'first?')
        self.c.store.event(tid, 'q2', 'question', 'second?')
        with self.assertRaises(ValueError): self.c.reply(client, tid, '', 'answer')
        result = self.c.reply(client, tid, 'q1', 'one')
        self.assertEqual(result['turn'], 1)
        with self.assertRaises(ValueError): self.c.reply(client, tid, 'q1', 'duplicate')
        result = self.c.reply(client, tid, 'q2', 'two')
        self.assertEqual((result['turn'], result['state']), (2, 'queued'))
        self.assertIn('q1: one', result['prompt'])
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(result['session'], task['session'])

    def test_cancel_withdraws_pending_intervention_and_close_uses_shared_lock(self):
        from contextlib import contextmanager
        client = self.c.connect('omp')['client_id']
        stranger = self.c.connect('pi')['client_id']
        task = self.c.start(client, prompt='old', cwd='/project')
        tid = task['id']
        with self.assertRaises(PermissionError): self.c.cancel(stranger, tid)
        self.c.store.update(tid, state='running', acpx_created=True, needs_session=False)
        self.c.intervene(client, tid, 'new', command_id='1')
        result = self.c.cancel(client, tid)
        self.assertEqual(result['intervention']['status'], 'withdrawn')
        self.c.store.finish_turn(tid, 1, 'cancelled', 0, 'cancelled')
        self.c.wait(client, tid, timeout=0)
        self.assertEqual(len(self.launches), 1)
        locked = []
        @contextmanager
        def lock(task, **kw):
            self.assertFalse(kw.get('exclusive', False))
            locked.append(True)
            yield True
            locked.pop()
        def control(task, action, **kw):
            self.assertTrue(locked)
            self.assertEqual(self.c.status(client, tid)['state'], 'closing')
            self.assertIn(action, {'close', 'status'})
            self.controls.append(action)
            return SimpleNamespace(returncode=0, stdout=b'{"action":"status_snapshot","status":"no-session","summary":"no active session"}')
        self.c.lock, self.c.control = lock, control
        with self.assertRaises(PermissionError): self.c.close(stranger, tid)
        self.assertEqual(self.c.close(client, tid)['state'], 'closed')
        self.assertEqual(self.controls, ['close', 'status'])

    def test_close_without_created_session_skips_control_and_failed_close_is_unknown(self):
        client = self.c.connect('pi')['client_id']
        task = self.c.start(client, prompt='x', cwd='/project')
        with self.assertRaises(ValueError): self.c.close(client, task['id'])
        self.c.cancel(client, task['id'])
        self.assertEqual(self.c.close(client, task['id'])['state'], 'closed')
        self.assertEqual(self.controls, [])
        other = self.c.start(client, prompt='x', cwd='/project')
        self.c.store.update(other['id'], state='completed', acpx_created=True)
        self.c.control = lambda *a, **kw: SimpleNamespace(returncode=1)
        self.assertEqual(self.c.close(client, other['id'])['state'], 'unknown')

    def test_json_cli_dispatches_all_actions_without_external_processes(self):
        from unittest.mock import patch
        from contextlib import redirect_stdout
        from io import StringIO
        def invoke(*argv):
            out = StringIO()
            with redirect_stdout(out), patch.object(collab, 'Collaboration', return_value=self.c):
                rc = collab.main(['--db', str(self.db), *argv])
            return rc, json.loads(out.getvalue())
        rc, client = invoke('connect', '--runtime', 'omp')
        self.assertEqual(rc, 0)
        cid = client['client_id']
        rc, task = invoke('--client', cid, 'start', '--cwd', '/project', '--prompt', 'inspect', '--agent', 'pi', '--timeout', '20')
        tid = task['id']
        self.assertEqual(task['agent'], 'pi')
        for action in ('status', 'events', 'wait'):
            args = ['--timeout', '0'] if action == 'wait' else []
            self.assertEqual(invoke('--client', cid, action, '--task', tid, *args)[0], 0)
        self.assertEqual(invoke('--client', cid, 'list')[0], 0)
        self.assertEqual(invoke('--client', cid, 'intervene', '--task', tid, '--prompt', 'new', '--command-id', 'human')[0], 0)
        self.c.store.event(tid, 'q', 'question', 'Question?')
        self.c.store.update(tid, state='waiting_reply')
        self.assertEqual(invoke('--client', cid, 'reply', '--task', tid, '--question-id', 'q', '--prompt', 'answer')[0], 0)
        self.assertEqual(invoke('--client', cid, 'cancel', '--task', tid)[0], 0)
        self.assertEqual(invoke('--client', cid, 'close', '--task', tid)[0], 0)
        rc, err = invoke('list')
        self.assertEqual(rc, 1)
        self.assertIn('error', err)

    def test_cli_subprocess_connect_and_list(self):
        import subprocess
        import sys
        cmd = [sys.executable, '-B', str(Path(collab.__file__)), '--db', str(self.db)]
        result = subprocess.run(cmd+['connect', '--runtime', 'codex'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        result = subprocess.run(cmd+['--client', data['client_id'], 'list'], capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), [])

    def test_wait_does_not_block_behind_sqlite_writer(self):
        import threading
        import time
        client = self.c.connect('pi')['client_id']
        task = self.c.start(client, prompt='x', cwd='/project')
        ready = threading.Event()
        def hold():
            with self.c.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                ready.set()
                time.sleep(.35)
        thread = threading.Thread(target=hold)
        thread.start()
        ready.wait()
        start = time.monotonic()
        try:
            result = self.c.wait(client, task['id'], timeout=0)
            self.assertLess(time.monotonic()-start, .2)
            self.assertEqual(result['task']['id'], task['id'])
        finally:
            thread.join()

    def test_worker_launch_failure_is_explicit_not_stuck_queued(self):
        client = self.c.connect('pi')['client_id']
        def fail(task): raise OSError('sensitive launch detail')
        self.c.launch = fail
        result = self.c.start(client, prompt='x', cwd='/project')
        self.assertEqual(result['state'], 'unknown')
        self.assertNotIn('sensitive', json.dumps(result))
        self.assertEqual(result['launch_error'], 'OSError')

    def test_concurrent_wait_and_duplicate_intervention_launch_only_one_turn(self):
        from concurrent.futures import ThreadPoolExecutor
        client = self.c.connect('omp')['client_id']
        task = self.c.start(client, prompt='x', cwd='/project')
        tid = task['id']
        self.c.store.update(tid, state='running')
        self.c.intervene(client, tid, 'human instruction', command_id='same')
        self.c.store.finish_turn(tid, 1, 'cancelled', 0, 'cancelled')
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: self.c.wait(client, tid, timeout=0), range(12)))
            list(pool.map(lambda _: self.c.intervene(client, tid, 'human instruction', command_id='same'), range(12)))
        # All zero-time concurrent probes may legitimately report SQLite busy.
        # One uncontended bounded follow-up must advance, without replaying work.
        followup = self.c.wait(client, tid, timeout=1)
        self.assertFalse(followup['busy'])
        result = self.c.status(client, tid)
        self.assertEqual(result['turn'], 2)
        self.assertTrue(result['needs_session'])
        self.assertEqual(len(self.launches), 2)

    def test_resuming_writer_checks_other_client_and_all_mutators_enforce_owner(self):
        a = self.c.connect('omp')['client_id']
        b = self.c.connect('pi')['client_id']
        task = self.c.start(a, prompt='x', cwd='/project', write=True)
        self.c.cancel(a, task['id'])
        self.c.start(b, prompt='x', cwd='/project', write=True)
        with self.assertRaises(ValueError): self.c.intervene(a, task['id'], 'resume')
        for call in (lambda: self.c.intervene(b, task['id'], 'x'),
                     lambda: self.c.reply(b, task['id'], 'q', 'x'),
                     lambda: self.c.wait(b, task['id'], timeout=0)):
            with self.assertRaises(PermissionError): call()
        self.assertEqual(self.c.status(a, task['id'])['turn'], 1)

    def test_fake_transport_online_question_intervention_and_close(self):
        from test_runner import RunnerBehaviorTests
        from runner import run_task
        from concurrent.futures import ThreadPoolExecutor
        import time
        fixture = RunnerBehaviorTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        c = collab.Collaboration(fixture.store.path, launch=lambda task: None)
        client = c.connect('omp')['client_id']
        task = c.start(client, prompt='ASK', cwd=str(fixture.root), agent='omp', timeout=10)
        tid = task['id']
        self.assertEqual(run_task(c.store.path, tid), 0)
        observation = c.wait(client, tid, timeout=0)
        q = [e for e in observation['events'] if e['kind']=='question'][0]
        c.reply(client, tid, q['token'], 'BLOCK')
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(run_task, c.store.path, tid)
            deadline = time.monotonic()+3
            while c.status(client, tid)['state'] != 'running' and time.monotonic()<deadline:
                time.sleep(.01)
            pending = c.intervene(client, tid, 'human revision', command_id='human')
            self.assertEqual(pending['intervention']['status'], 'pending')
            self.assertEqual(running.result(timeout=5), 0)
        c.wait(client, tid, after=observation['after'], timeout=0)
        self.assertEqual(run_task(c.store.path, tid), 0)
        result = c.wait(client, tid, timeout=0)
        self.assertEqual(result['task']['state'], 'completed')
        self.assertTrue(any(e['kind']=='stage' for e in result['events']))
        self.assertEqual(result['task']['session'], task['session'])
        self.assertEqual(c.close(client, tid)['state'], 'closed')
        calls = [json.loads(line) for line in (fixture.root/'calls.jsonl').read_text().splitlines()]
        self.assertEqual(sum('new' in call for call in calls), 1)
        self.assertEqual(sum('close' in call for call in calls), 1)

    def test_close_does_not_skip_uncertain_session_creation(self):
        client = self.c.connect('pi')['client_id']
        task = self.c.start(client, prompt='x', cwd='/project')
        self.c.store.update(task['id'], state='completed', acpx_created=False, runner_pid=0)
        original = self.c.store.begin_close
        def changed(task_id, origin):
            self.c.store.update(task_id, state='unknown')
            return original(task_id, origin)
        self.c.store.begin_close = changed
        self.c.close(client, task['id'])
        self.assertEqual(self.controls, ['close', 'status'])

    def test_connect_persists_declared_identity_in_same_database(self):
        a = self.c.connect('omp')
        b = self.c.connect('codex')
        self.assertNotEqual(a['client_id'], b['client_id'])
        self.assertEqual(a['declared_runtime'], 'omp')
        restored = collab.Collaboration(self.db).origin(a['client_id'])
        self.assertEqual((restored.profile, restored.session_id, restored.session_key, restored.platform),
                         ('standalone', a['client_id'], a['client_id'], 'omp'))
        with self.c.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM clients').fetchone()[0], 2)
        with self.assertRaises(ValueError): self.c.connect('unsupported-runtime')
        with self.assertRaises(PermissionError): self.c.origin('missing')
