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

    def test_cli_shorthand_rejection_explains_conflict_or_unsupported_without_launch(self):
        from unittest.mock import patch
        from contextlib import redirect_stdout
        from io import StringIO
        client=self.c.connect('omp')['client_id']
        for args,reason in ((['--model','ocs','--agy-swarm','off'],'conflict'),
                            (['--model','oct','--agy-swarm','off'],'conflict'),(['--model','ocss'],'Invalid AGY shorthand')):
            with self.subTest(args=args):
                output=StringIO()
                with patch.object(collab,'Collaboration',return_value=self.c),redirect_stdout(output):
                    rc=collab.main(['--db',str(self.db),'--client',client,'start','--agent','agy',
                                    '--cwd',self.tmp.name,'--prompt','never send',*args])
                self.assertEqual(rc,1)
                self.assertIn(reason,json.loads(output.getvalue())['message'])
        self.assertEqual(self.c.list(client),[])
        self.assertEqual(self.launches,[])

    def test_cli_teamwork_shorthand_is_supported(self):
        from unittest.mock import patch
        from contextlib import redirect_stdout
        from io import StringIO
        client=self.c.connect('omp')['client_id']
        with patch.object(collab,'Collaboration',return_value=self.c),redirect_stdout(StringIO()):
            rc=collab.main(['--db',str(self.db),'--client',client,'start','--agent','agy',
                           '--cwd',self.tmp.name,'--prompt','synthetic','--model','oct'])
        self.assertEqual(rc,0)
        self.assertEqual(len(self.launches),1)
        task=self.c.list(client)[0]
        self.assertEqual((task['model'],task['agy_mode'],task['agy_swarm']),
                         ('gemini-pro-orchestrator','teamwork','native'))

    def test_cli_shorthand_start_persists_resolved_selection(self):
        from unittest.mock import patch
        from contextlib import redirect_stdout
        from io import StringIO
        client=self.c.connect('omp')['client_id']
        for model,expected,mode in ((None,'gemini-pro-orchestrator','native'),
                ('oc','gemini-pro-orchestrator','off'),('oas','gemini-pro-orchestrator-argon','native'),
                ('4a','gemini-4-argon','off'),('4as','gemini-4-argon','native')):
            output=StringIO()
            args=[] if model is None else ['--model',model]
            with patch.object(collab,'Collaboration',return_value=self.c),redirect_stdout(output):
                rc=collab.main(['--db',str(self.db),'--client',client,'start','--agent','agy',
                                '--cwd',self.tmp.name,'--prompt','synthetic',*args])
            self.assertEqual(rc,0,output.getvalue())
            task=json.loads(output.getvalue())
            self.assertEqual((task['model'],task['agy_swarm']),(expected,mode))
            self.assertEqual((self.launches[-1]['model'],self.launches[-1]['agy_swarm']),(expected,mode))

    def test_cli_dialogue_uses_same_turn_without_launch_or_cancel(self):
        from unittest.mock import patch
        from contextlib import redirect_stdout
        from io import StringIO
        client = self.c.connect('omp')['client_id']
        task = self.c.start(client, agent='agy', prompt='owned', cwd='/project')
        self.c.store.update(task['id'], state='running', runner_pid=123)
        self.c.store.native_input_open(task['id'], 1, task['launch_id'], 1)
        output = StringIO()
        with patch.object(collab, 'Collaboration', return_value=self.c), redirect_stdout(output):
            rc = collab.main(['--db', str(self.db), '--client', client, 'dialogue', '--task', task['id'],
                '--prompt', 'revise', '--expected-turn', '1', '--command-id', 'd1'])
        self.assertEqual(rc, 0, output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual((result['state'], result['turn']), ('running', 1))
        self.assertEqual(result['commands']['d1']['status'], 'accepted')
        self.assertEqual((len(self.launches), self.controls), (1, []))
        sup_out = StringIO()
        with patch.object(collab, 'Collaboration', return_value=self.c), redirect_stdout(sup_out):
            rc = collab.main(['--db', str(self.db), '--client', client, 'dialogue', '--task', task['id'],
                '--prompt', 'revise 2', '--expected-turn', '1', '--command-id', 'd2', '--target-command-id', 'd1'])
        self.assertEqual(rc, 0, sup_out.getvalue())
        sup_res = json.loads(sup_out.getvalue())
        self.assertEqual(sup_res['commands']['d1']['status'], 'superseded')
        self.assertEqual(sup_res['commands']['d2']['status'], 'accepted')
        wd_out = StringIO()
        with patch.object(collab, 'Collaboration', return_value=self.c), redirect_stdout(wd_out):
            rc = collab.main(['--db', str(self.db), '--client', client, 'dialogue', '--task', task['id'],
                '--expected-turn', '1', '--command-id', 'w2', '--target-command-id', 'd2', '--withdraw'])
        self.assertEqual(rc, 0, wd_out.getvalue())
        self.assertEqual(json.loads(wd_out.getvalue())['commands']['d2']['status'], 'withdrawn')

    def test_new_tasks_without_budget_remain_cancellable_and_legacy_budgets_survive(self):
        client = self.c.connect('omp')['client_id']
        unbounded = self.c.start(client, prompt='inspect', cwd='/project')
        self.assertIsNone(unbounded.get('timeout'))
        self.c.store.update(unbounded['id'], state='running')
        self.assertEqual(self.c.cancel(client, unbounded['id'])['state'], 'cancelling')
        bounded = self.c.start(client, prompt='inspect', cwd='/project', timeout=30)
        self.c.store.event(bounded['id'], 'q', 'question', 'Continue?')
        self.c.store.update(bounded['id'], state='completed')
        resumed = self.c.reply(client, bounded['id'], 'q', 'continue')
        self.assertEqual(resumed['timeout'], 30)
        for invalid in (0, -1, float('nan'), float('inf'), True, 'none'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.c.start(client, prompt='inspect', cwd='/project', timeout=invalid)

    def test_cli_clear_preserves_question_contract_and_next_turn_budget(self):
        from unittest.mock import patch
        from contextlib import redirect_stdout
        from io import StringIO
        client = self.c.connect('omp')['client_id']
        task = self.c.start(client, prompt='inspect', cwd='/project', timeout=420)
        tid = task['id']
        self.c.store.event(tid, 'q1', 'question', 'Q1 format?')
        self.c.store.event(tid, 'q2', 'question', 'Q2 language?')
        output = StringIO()
        with patch.object(collab, 'Collaboration', return_value=self.c), redirect_stdout(output):
            rc = collab.main(['--db', str(self.db), '--client', client, 'reply',
                              '--task', tid, '--question-id', 'q1', '--prompt', 'JSON', '--clear-timeout'])
        self.assertEqual(rc, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['timeout'], 420)
        resumed = self.c.reply(client, tid, 'q2', 'English')
        self.assertEqual((resumed['turn'], resumed['timeout']), (2, None))
        self.c.store.update(tid, state='completed')
        with self.assertRaises(ValueError):
            self.c.reply(client, tid, '', 'unprompted', clear_timeout=True)
        revised = self.c.intervene(client, tid, 'new work', command_id='revision', clear_timeout=True)
        self.assertEqual((revised['turn'], revised['timeout']), (3, None))

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
        self.assertEqual(args[0], [sys.executable, str(Path(collab.__file__).with_name('runner.py')),
            '--db', str(self.db), '--task', task['id'], '--turn', str(task['turn']),
            '--launch-id', task['launch_id'], '--epoch', str(task['control_epoch'])])
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
        from unittest.mock import patch
        from fixtures.close_backend import CloseBackend
        self.c.store.update(tid,acpx_record_id='fixture-record')
        def close(policy):
            self.assertEqual(self.c.status(client,tid)['state'],'unknown')
            self.assertTrue(self.c.status(client,tid)['release_epoch'])
        backend = CloseBackend(on_close=close)
        with self.assertRaises(PermissionError): self.c.close(stranger,tid)
        with patch('release.NativeBackend',return_value=backend):
            self.assertEqual(self.c.close(client,tid)['state'],'closed')
        self.assertEqual((backend.closes,backend.probes),(1,1))

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

    def test_intervention_dispatch_failure_marks_target_unknown_without_retry(self):
        client = self.c.connect('omp')['client_id']
        task = self.c.start(client, prompt='initial', cwd='/project')
        self.c.store.update(task['id'], state='completed')
        attempts = []
        def fail(current):
            attempts.append(current['turn'])
            raise OSError('synthetic dispatch uncertainty')
        self.c.launch = fail
        result = self.c.intervene(client, task['id'], 'adjust', command_id='stable')
        self.assertEqual((result['state'], result['turn']), ('unknown', 2))
        self.assertEqual(result['commands']['stable']['status'], 'target_unknown')
        self.assertEqual(result['intervention']['status'], 'target_unknown')
        self.c.intervene(client, task['id'], 'adjust', command_id='stable')
        self.c.wait(client, task['id'], timeout=0)
        self.assertEqual(attempts, [2])

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
        self.assertEqual(__import__('test_dispatch').run_dispatched(c.store.path, tid), 0)
        observation = c.wait(client, tid, timeout=0)
        q = [e for e in observation['events'] if e['kind']=='question'][0]
        c.reply(client, tid, q['token'], 'BLOCK')
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(__import__('test_dispatch').run_dispatched, c.store.path, tid)
            deadline = time.monotonic()+3
            while c.status(client, tid)['state'] != 'running' and time.monotonic()<deadline:
                time.sleep(.01)
            pending = c.intervene(client, tid, 'human revision', command_id='human')
            self.assertEqual(pending['intervention']['status'], 'pending')
            self.assertEqual(running.result(timeout=5), 0)
        c.wait(client, tid, after=observation['after'], timeout=0)
        self.assertEqual(__import__('test_dispatch').run_dispatched(c.store.path, tid), 0)
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
        self.c.close(client, task['id'])
        self.assertEqual(self.c.status(client,task['id'])['state'],'unknown')
        self.assertEqual(self.controls, [])

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

    def test_standalone_group_origin_context_runtime_links_and_isolation(self):
        from unittest.mock import patch
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        a = self.c.connect('omp')['client_id']
        b = self.c.connect('pi')['client_id']
        t1 = self.c.start(a, prompt='one', cwd='/project', group=' alpha ')
        t2 = self.c.start(a, prompt='two', cwd='/project', group='beta')
        tb = self.c.start(b, prompt='three', cwd='/project', group='alpha')
        self.assertEqual(t1['group'], 'alpha')
        self.assertEqual(t1['origin_context']['surface'], 'standalone')
        self.assertEqual(t1['origin_context']['platform'], 'omp')
        self.assertEqual(t1['origin_context']['display_name'], 'unknown')
        self.assertEqual(t1['runtime_links']['task_id'], t1['id'])
        self.assertEqual([t['id'] for t in self.c.list(a, group='alpha')], [t1['id']])
        self.assertEqual(self.c.list(a, group='alpha')[0]['runtime_links']['task_id'], t1['id'])
        self.assertEqual([t['id'] for t in self.c.list(b, group='alpha')], [tb['id']])
        self.assertNotIn(tb['id'], [t['id'] for t in self.c.list(a, group='alpha')])
        self.assertEqual(self.c.status(a, t1['id'])['runtime_links']['session'], t1['session'])

        def run_cli(*argv):
            out = StringIO()
            with redirect_stdout(out), patch.object(collab, 'Collaboration', return_value=self.c):
                rc = collab.main(['--db', str(self.db), '--client', a, *argv])
            return rc, json.loads(out.getvalue())

        rc, cli_task = run_cli('start', '--cwd', '/project', '--prompt', 'via cli', '--group', 'cli-grp')
        self.assertEqual(rc, 0)
        self.assertEqual(cli_task['group'], 'cli-grp')
        self.assertEqual(cli_task['runtime_links']['task_id'], cli_task['id'])
        rc, cli_list = run_cli('list', '--group', 'cli-grp')
        self.assertEqual(rc, 0)
        self.assertEqual([t['id'] for t in cli_list], [cli_task['id']])
        with self.assertRaises(ValueError):
            self.c.list(a, group='')
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            collab.main(['--db', str(self.db), '--client', a, 'start', '--cwd', '/project', '--prompt', 'x', '--goal', 'bad'])

    def _run_cli(self, client, *argv):
        from unittest.mock import patch
        from contextlib import redirect_stdout
        from io import StringIO
        out = StringIO()
        with redirect_stdout(out), patch.object(collab, 'Collaboration', return_value=self.c):
            rc = collab.main(['--db', str(self.db), '--client', client, *argv])
        return rc, json.loads(out.getvalue())

    def _start_native_running(self, client, question_tokens, *, cwd='/project'):
        task = self.c.start(client, agent='agy', model='ocs', prompt='owned', cwd=cwd)
        tid = task['id']
        self.c.store.update(tid, state='running', agy_swarm='native', runner_pid=123)
        self.c.store.native_input_open(tid, 1, task['launch_id'], 1)
        for idx, token in enumerate(question_tokens, 1):
            self.c.store.event(tid, token, 'question', f'Q{idx}?', turn=1, interaction_seq=0)
        return self.c.status(client, tid)

    def test_dialogue_question_ids_api_cli_idempotency_withdraw_and_supersede(self):
        client = self.c.connect('omp')['client_id']
        task = self._start_native_running(client, ['q1', 'q2', 'q3'])
        tid = task['id']

        rc, res = self._run_cli(client, 'dialogue', '--task', tid, '--expected-turn', '1',
                                '--command-id', 'd1', '--question-ids', 'q1', 'q2', '--prompt', 'shared')
        self.assertEqual(rc, 0, res)
        self.assertEqual((res['state'], res['turn']), ('running', 1))
        self.assertEqual(res['commands']['d1']['status'], 'accepted')
        self.assertEqual(res['commands']['d1']['question_ids'], ['q1', 'q2'])
        self.assertIsNone(res['commands']['d1']['question_id'])
        self.assertEqual(res['questions'], ['q1', 'q2', 'q3'])
        self.assertEqual((len(self.launches), self.controls), (1, []))

        # Reordered question_ids with same command_id is idempotent.
        rc_idem, res_idem = self._run_cli(client, 'dialogue', '--task', tid, '--expected-turn', '1',
                                          '--command-id', 'd1', '--question-ids', 'q2', 'q1', '--prompt', 'shared')
        self.assertEqual(rc_idem, 0, res_idem)
        self.assertEqual(list(res_idem['commands']), ['d1'])
        self.assertEqual(res_idem['commands']['d1']['question_ids'], ['q1', 'q2'])

        # Conflicting command_id reuse or double-answering bound question fails cleanly.
        rc_diff, _ = self._run_cli(client, 'dialogue', '--task', tid, '--expected-turn', '1',
                                   '--command-id', 'd1', '--question-ids', 'q1', 'q3', '--prompt', 'shared')
        self.assertEqual(rc_diff, 1)
        rc_dup_q, _ = self._run_cli(client, 'dialogue', '--task', tid, '--expected-turn', '1',
                                    '--command-id', 'd_dup', '--question-ids', 'q2', 'q3', '--prompt', 'dup')
        self.assertEqual(rc_dup_q, 1)
        self.assertNotIn('d_dup', self.c.status(client, tid)['commands'])

        # Withdraw releases all bound IDs for re-answering.
        rc_wd, res_wd = self._run_cli(client, 'dialogue', '--task', tid, '--expected-turn', '1',
                                      '--command-id', 'w1', '--target-command-id', 'd1', '--withdraw')
        self.assertEqual(rc_wd, 0, res_wd)
        self.assertEqual(res_wd['commands']['d1']['status'], 'withdrawn')

        # Repeated --question-ids flag syntax re-answers released IDs.
        rc_rep, res_rep = self._run_cli(client, 'dialogue', '--task', tid, '--expected-turn', '1',
                                        '--command-id', 'd2', '--question-ids', 'q1', '--question-ids', 'q2',
                                        '--prompt', 'reanswered')
        self.assertEqual(rc_rep, 0, res_rep)
        self.assertEqual(res_rep['commands']['d2']['status'], 'accepted')
        self.assertEqual(res_rep['commands']['d2']['question_ids'], ['q1', 'q2'])

        # --target-command-id supersede releases previously bound IDs no longer in the replacement.
        rc_sup, res_sup = self._run_cli(client, 'dialogue', '--task', tid, '--expected-turn', '1',
                                        '--command-id', 'd3', '--target-command-id', 'd2',
                                        '--question-ids', 'q2', 'q3', '--prompt', 'shifted')
        self.assertEqual(rc_sup, 0, res_sup)
        self.assertEqual(res_sup['commands']['d2']['status'], 'superseded')
        self.assertEqual(res_sup['commands']['d3']['status'], 'accepted')
        self.assertEqual(res_sup['commands']['d3']['question_ids'], ['q2', 'q3'])

        rc_q1, res_q1 = self._run_cli(client, 'dialogue', '--task', tid, '--expected-turn', '1',
                                      '--command-id', 'd4', '--question-id', 'q1', '--prompt', 'first only')
        self.assertEqual(rc_q1, 0, res_q1)
        self.assertEqual(res_q1['commands']['d4']['status'], 'accepted')
        self.assertEqual(res_q1['commands']['d4']['question_id'], 'q1')

        # Python API coverage: question_ids, reordered idempotency, and unbound supersede release.
        api_task = self._start_native_running(client, ['a1', 'a2'], cwd='/project-api')
        atid = api_task['id']
        api_res = self.c.dialogue(client, atid, 'api shared', expected_turn=1, command_id='api-d1',
                                  question_ids=['a1', 'a2'])
        self.assertEqual(api_res['commands']['api-d1']['status'], 'accepted')
        self.assertEqual(api_res['commands']['api-d1']['question_ids'], ['a1', 'a2'])
        api_idem = self.c.dialogue(client, atid, 'api shared', expected_turn=1, command_id='api-d1',
                                   question_ids=['a2', 'a1'])
        self.assertEqual(list(api_idem['commands']), ['api-d1'])
        api_sup = self.c.dialogue(client, atid, 'api unbound', expected_turn=1, command_id='api-d2',
                                  target_command_id='api-d1')
        self.assertEqual(api_sup['commands']['api-d1']['status'], 'superseded')
        api_re = self.c.dialogue(client, atid, 'rebound', expected_turn=1, command_id='api-d3',
                                 question_ids=['a2', 'a1'])
        self.assertEqual(api_re['commands']['api-d3']['status'], 'accepted')

    def test_reply_question_ids_api_cli_partial_merge_and_clear_timeout(self):
        client = self.c.connect('codex')['client_id']

        # CLI multi-question partial -> final reply with delayed --clear-timeout and legacy merged prompt.
        task = self.c.start(client, prompt='inspect', cwd='/project-r1', timeout=180)
        tid = task['id']
        self.c.store.update(tid, state='waiting_reply', needs_session=False, acpx_created=True)
        for idx, qid in enumerate(['q1', 'q2', 'q3'], 1):
            self.c.store.event(tid, qid, 'question', f'Q{idx}?', turn=1)

        rc1, res1 = self._run_cli(client, 'reply', '--task', tid, '--question-ids', 'q1', 'q2',
                                  '--prompt', 'shared', '--clear-timeout', '--expected-turn', '1',
                                  '--command-id', 'r1')
        self.assertEqual(rc1, 0, res1)
        self.assertEqual((res1['turn'], res1['state'], res1['timeout']), (1, 'waiting_reply', 180))
        self.assertEqual(res1['questions'], ['q3'])
        self.assertEqual(res1['question_id'], 'q3')
        self.assertEqual(res1['answers'], {'q1': 'shared', 'q2': 'shared'})
        self.assertEqual(len(self.launches), 1)

        # Order-normalized command_id idempotency on reply.
        rc1_idem, res1_idem = self._run_cli(client, 'reply', '--task', tid, '--question-ids', 'q2', 'q1',
                                            '--prompt', 'shared', '--clear-timeout', '--expected-turn', '1',
                                            '--command-id', 'r1')
        self.assertEqual(rc1_idem, 0, res1_idem)
        self.assertEqual(res1_idem['questions'], ['q3'])

        # Answering final question advances to turn 2, clears timeout, and merges answers in legacy format.
        rc2, res2 = self._run_cli(client, 'reply', '--task', tid, '--question-ids', 'q3',
                                  '--prompt', 'final', '--expected-turn', '1', '--command-id', 'r2')
        self.assertEqual(rc2, 0, res2)
        self.assertEqual((res2['turn'], res2['state'], res2['timeout']), (2, 'queued', None))
        self.assertEqual(res2['prompt'], 'q1: shared\nq2: shared\nq3: final')
        self.assertEqual(res2['questions'], [])
        self.assertEqual(res2['answers'], {})
        self.assertEqual(len(self.launches), 2)

        # Replaying final reply command_id after turn advancement does not re-launch.
        rc2_idem, _ = self._run_cli(client, 'reply', '--task', tid, '--question-ids', 'q3',
                                    '--prompt', 'final', '--expected-turn', '1', '--command-id', 'r2')
        self.assertEqual(rc2_idem, 0)
        self.assertEqual(len(self.launches), 2)

        # CLI single-question --question-ids q1 produces unprefixed 'shared'.
        single = self.c.start(client, prompt='inspect single', cwd='/project-r2', timeout=60)
        stid = single['id']
        self.c.store.update(stid, state='waiting_reply', needs_session=False, acpx_created=True)
        self.c.store.event(stid, 'q1', 'question', 'Only?', turn=1)
        rc_single, res_single = self._run_cli(client, 'reply', '--task', stid, '--question-ids', 'q1',
                                              '--prompt', 'shared', '--clear-timeout')
        self.assertEqual(rc_single, 0, res_single)
        self.assertEqual((res_single['turn'], res_single['state'], res_single['timeout']), (2, 'queued', None))
        self.assertEqual(res_single['prompt'], 'shared')

        # Python API partial -> final reply with question_ids and clear_timeout.
        api_task = self.c.start(client, prompt='api inspect', cwd='/project-r3', timeout=90)
        atid = api_task['id']
        self.c.store.update(atid, state='waiting_reply', needs_session=False, acpx_created=True)
        for idx, qid in enumerate(['p1', 'p2', 'p3'], 1):
            self.c.store.event(atid, qid, 'question', f'P{idx}?', turn=1)
        part = self.c.reply(client, atid, prompt='batch', question_ids=['p1', 'p2'], clear_timeout=True,
                            expected_turn=1, command_id='api-r1')
        self.assertEqual((part['turn'], part['state'], part['timeout']), (1, 'waiting_reply', 90))
        part_idem = self.c.reply(client, atid, prompt='batch', question_ids=['p2', 'p1'], clear_timeout=True,
                                 expected_turn=1, command_id='api-r1')
        self.assertEqual(part_idem['questions'], ['p3'])
        done = self.c.reply(client, atid, 'p3', 'end')
        self.assertEqual((done['turn'], done['state'], done['timeout']), (2, 'queued', None))
        self.assertEqual(done['prompt'], 'p1: batch\np2: batch\np3: end')

    def test_question_ids_validation_rejections_and_raw_whitespace_preservation(self):
        client = self.c.connect('omp')['client_id']
        dlg_task = self._start_native_running(client, ['q1', 'q2', 'q_keep', ' 1:raw '], cwd='/project-rej-d')
        dtid = dlg_task['id']
        self.c.dialogue(client, dtid, 'keep me', expected_turn=1, command_id='d_keep', question_id='q_keep')

        rpl_task = self.c.start(client, prompt='settled', cwd='/project-rej-r', timeout=120)
        rtid = rpl_task['id']
        self.c.store.update(rtid, state='waiting_reply', needs_session=False, acpx_created=True)
        for idx, qid in enumerate(['q1', 'q2', ' 1:raw '], 1):
            self.c.store.event(rtid, qid, 'question', f'Q{idx}?', turn=1)

        dlg_before = self.c.status(client, dtid)
        rpl_before = self.c.status(client, rtid)

        bad_dlg_cases = [
            (['--question-id', 'q1', '--question-ids', 'q2'], 'mutual_exclusion'),
            (['--question-id', '', '--question-ids', 'q2'], 'empty_scalar_mutual_exclusion'),
            (['--question-ids', 'q1', 'q1'], 'duplicate_in_one_flag'),
            (['--question-ids', 'q1', '--question-ids', 'q1'], 'duplicate_across_flags'),
            (['--question-ids', ''], 'empty_element'),
            (['--question-ids', 'q1', ''], 'empty_second_element'),
            (['--question-id', 'q1,q2'], 'csv_scalar'),
            (['--question-ids', 'q1,q2'], 'csv_array_element'),
            (['--question-ids', '1:raw'], 'trimmed_mismatch_array'),
            (['--question-id', '1:raw'], 'trimmed_mismatch_scalar'),
            (['--withdraw', '--target-command-id', 'd_keep', '--question-ids', 'q1'], 'withdraw_with_question_ids'),
        ]
        for idx, (extra, label) in enumerate(bad_dlg_cases):
            with self.subTest(surface='cli_dialogue', case=label):
                cmd = ['dialogue', '--task', dtid, '--expected-turn', '1', '--command-id', f'bad-d-{idx}']
                if '--withdraw' not in extra:
                    cmd.extend(['--prompt', 'should fail'])
                cmd.extend(extra)
                rc, err = self._run_cli(client, *cmd)
                self.assertEqual(rc, 1, err)
                self.assertIn('error', err)
                self.assertEqual(self.c.status(client, dtid), dlg_before)

        bad_rpl_cases = [
            (['--question-id', 'q1', '--question-ids', 'q2'], 'mutual_exclusion'),
            (['--question-id', '', '--question-ids', 'q2'], 'empty_scalar_mutual_exclusion'),
            (['--question-ids', 'q1', 'q1'], 'duplicate_in_one_flag'),
            (['--question-ids', 'q1', '--question-ids', 'q1'], 'duplicate_across_flags'),
            (['--question-ids', ''], 'empty_element'),
            (['--question-ids', 'q1', ''], 'empty_second_element'),
            (['--question-id', 'q1,q2'], 'csv_scalar'),
            (['--question-ids', 'q1,q2'], 'csv_array_element'),
            (['--question-ids', '1:raw'], 'trimmed_mismatch_array'),
            (['--question-id', '1:raw'], 'trimmed_mismatch_scalar'),
        ]
        for idx, (extra, label) in enumerate(bad_rpl_cases):
            with self.subTest(surface='cli_reply', case=label):
                rc, err = self._run_cli(client, 'reply', '--task', rtid, '--prompt', 'should fail', *extra)
                self.assertEqual(rc, 1, err)
                self.assertIn('error', err)
                self.assertEqual(self.c.status(client, rtid), rpl_before)

        # Python API type and validation rejections without state mutation.
        for kw, label in [
            (dict(question_id='q1', question_ids=['q2']), 'mutual_exclusion'),
            (dict(question_id='', question_ids=['q2']), 'empty_scalar_mutual_exclusion'),
            (dict(question_ids=[]), 'empty_list'),
            (dict(question_ids='q1,q2'), 'csv_string'),
            (dict(question_ids=[1]), 'non_string_element'),
            (dict(question_ids=['']), 'empty_element'),
            (dict(question_ids=['q1', 'q1']), 'duplicate'),
            (dict(question_ids=['1:raw']), 'trimmed_mismatch'),
        ]:
            with self.subTest(surface='api_dialogue', case=label):
                with self.assertRaises(ValueError):
                    self.c.dialogue(client, dtid, 'bad', expected_turn=1, command_id=f'api-bad-d-{label}', **kw)
                self.assertEqual(self.c.status(client, dtid), dlg_before)
            with self.subTest(surface='api_reply', case=label):
                with self.assertRaises(ValueError):
                    self.c.reply(client, rtid, prompt='bad', **kw)
                self.assertEqual(self.c.status(client, rtid), rpl_before)

        with self.subTest(surface='api_dialogue', case='withdraw_with_question_ids'):
            with self.assertRaises(ValueError):
                self.c.dialogue(client, dtid, expected_turn=1, command_id='api-bad-wd',
                                target_command_id='d_keep', withdraw=True, question_ids=['q1'])
            self.assertEqual(self.c.status(client, dtid), dlg_before)

        # Exact raw-whitespace token ' 1:raw ' works without trimming in both dialogue and reply.
        rc_raw_d, res_raw_d = self._run_cli(client, 'dialogue', '--task', dtid, '--expected-turn', '1',
                                            '--command-id', 'd_raw', '--question-ids', ' 1:raw ',
                                            '--prompt', 'raw dialogue')
        self.assertEqual(rc_raw_d, 0, res_raw_d)
        self.assertEqual(res_raw_d['commands']['d_raw']['question_ids'], [' 1:raw '])

        rc_raw_r1, res_raw_r1 = self._run_cli(client, 'reply', '--task', rtid, '--question-ids', ' 1:raw ', 'q1',
                                              '--prompt', 'raw reply')
        self.assertEqual(rc_raw_r1, 0, res_raw_r1)
        self.assertEqual(res_raw_r1['answers'], {' 1:raw ': 'raw reply', 'q1': 'raw reply'})
        self.assertEqual(res_raw_r1['questions'], ['q2'])

        rc_raw_r2, res_raw_r2 = self._run_cli(client, 'reply', '--task', rtid, '--question-id', 'q2',
                                              '--prompt', 'last')
        self.assertEqual(rc_raw_r2, 0, res_raw_r2)
        self.assertEqual((res_raw_r2['turn'], res_raw_r2['state']), (2, 'queued'))
        self.assertEqual(res_raw_r2['prompt'], ' 1:raw : raw reply\nq1: raw reply\nq2: last')

    def test_cli_and_python_blocker_interaction_seq_validation_and_idempotency(self):
        client = self.c.connect('omp')['client_id']
        dt = self.c.start(client, prompt='inspect', cwd='/cli-blk', agent='agy', model='ocs')
        dtid = dt['id']
        self.c.store.update(dtid, state='running', agy_swarm='native', launch_id=dt['launch_id'], runner_pid=123,
                             native_observation=dict(
                                 turn=1, input_available=True, question_ids=[],
                                 question_blockers=[dict(interaction_seq=0, reason='incomplete_question',
                                                         available_actions=['observe', 'dialogue', 'cancel'], blocks_completion=True)],
                             ))
        self.c.store.native_input_open(dtid, 1, dt['launch_id'], 1)
        self.c.store.event(dtid, 'q1', 'question', 'Q1?', turn=1, interaction_seq=0)

        rt = self.c.start(client, prompt='reply', cwd='/cli-blk-rpl')
        rtid = rt['id']
        self.c.store.update(rtid, state='waiting_reply')
        self.c.store.event(rtid, 'q1', 'question', 'Q1?', turn=1)

        with self.assertRaises(SystemExit) as cm:
            collab.main(['--db', str(self.db), '--client', client, 'reply', '--task', rtid,
                         '--question-id', 'q1', '--prompt', 'ans', '--blocker-interaction-seq', '0'])
        self.assertNotEqual(cm.exception.code, 0)

        before_dlg = self.c.status(client, dtid)
        for extra in (
            ['--blocker-interaction-seq', '-1'],
            ['--blocker-interaction-seq', '1'],
            ['--blocker-interaction-seq', '0', '--question-id', 'q1'],
            ['--blocker-interaction-seq', '0', '--question-ids', 'q1'],
        ):
            rc, err = self._run_cli(client, 'dialogue', '--task', dtid, '--expected-turn', '1',
                                    '--command-id', 'cli-bad-blk', '--prompt', 'bad', *extra)
            self.assertEqual(rc, 1, err)
            self.assertEqual(self.c.status(client, dtid), before_dlg)

        for bad_py in (False, True, -1, '0'):
            with self.assertRaises(ValueError):
                self.c.dialogue(client, dtid, 'bad py', expected_turn=1, command_id='py-bad-blk',
                                blocker_interaction_seq=bad_py)

        rc0, res0 = self._run_cli(client, 'dialogue', '--task', dtid, '--expected-turn', '1',
                                  '--command-id', 'cli-b0', '--blocker-interaction-seq', '0',
                                  '--prompt', 'review 0')
        self.assertEqual(rc0, 0, res0)
        self.assertEqual(res0['commands']['cli-b0']['blocker_interaction_seq'], 0)

        rc_wd, err_wd = self._run_cli(client, 'dialogue', '--task', dtid, '--expected-turn', '1',
                                      '--command-id', 'cli-wb0-bad', '--target-command-id', 'cli-b0',
                                      '--withdraw', '--blocker-interaction-seq', '0')
        self.assertEqual(rc_wd, 1, err_wd)

        self.c.store.native_dialogue_claim(dtid, 1, dt['launch_id'], 1)
        self.c.store.native_dialogue_state(dtid, 1, dt['launch_id'], 1, 'cli-b0', 'send_started')
        self.c.store.native_dialogue_state(dtid, 1, dt['launch_id'], 1, 'cli-b0', 'returned')
        self.c.store.update(dtid, state='completed', questions=[], question_id='',
                            native_input=dict(accepting=False, settling=True, turn=1, launch_id=dt['launch_id'], epoch=1),
                            native_observation=dict(turn=1, input_available=False, question_ids=[], question_blockers=[]))
        rc_retry, res_retry = self._run_cli(client, 'dialogue', '--task', dtid, '--expected-turn', '1',
                                            '--command-id', 'cli-b0', '--blocker-interaction-seq', '0',
                                            '--prompt', 'review 0')
        self.assertEqual(rc_retry, 0, res_retry)
        self.assertEqual(res_retry['state'], 'completed')
        self.assertEqual(res_retry['commands']['cli-b0']['blocker_interaction_seq'], 0)

    def test_recovery_status_readonly_and_missing_db_never_initializes(self):
        c1 = self.c.connect('codex')['client_id']
        c2 = self.c.connect('omp')['client_id']
        task = self.c.start(c1, agent='agy', prompt='CANARY_PROMPT', cwd=self.tmp.name)
        tid = task['id']
        self.c.store.update(
            tid, state='unknown', runner_pid=0, settlement_note='CANARY_NOTE',
            native_observation={
                'turn': 1, 'history_degraded': True,
                'pending_tools': [{'id': 'CANARY_TID', 'name': 'CANARY_TOOL', 'kind': 'search', 'status': 'in_progress'}],
                'history_diagnostic': {'reason_code': 'dialogue_missing', 'call_site': 'section',
                                       'previous_len': 62, 'current_len': 25, 'extra_secret': 'CANARY_EXTRA'},
            },
        )
        before = self.db.read_bytes()
        res = self.c.recovery_status(c2, tid)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(res['resource_recovery']['task_id'], tid)
        self.assertEqual(res['diagnostic']['agent'], 'agy')
        self.assertEqual(res['diagnostic']['native_diagnostic']['pending_tool_kinds'], ['search'])
        self.assertEqual(res['diagnostic']['native_diagnostic']['history_diagnostic'], {
            'reason_code': 'dialogue_missing', 'call_site': 'section', 'previous_len': 62, 'current_len': 25,
        })
        for secret in ('CANARY_', c1, c2, self.tmp.name):
            self.assertNotIn(secret, json.dumps(res))

        rc, cli_res = self._run_cli(c2, 'recovery_status', '--task', tid)
        self.assertEqual(rc, 0, cli_res)
        self.assertEqual(cli_res, res)
        self.assertEqual(self.db.read_bytes(), before)

        missing_dir = Path(self.tmp.name) / 'no-such-dir'
        missing_db = missing_dir / 'tasks.sqlite3'
        for argv in (
            ['--db', str(missing_db), '--client', c1, 'list'],
            ['--db', str(missing_db), '--client', c1, 'status', '--task', tid],
            ['--db', str(missing_db), '--client', c1, 'events', '--task', tid],
            ['--db', str(missing_db), '--client', c1, 'recovery_status', '--task', tid],
        ):
            from io import StringIO
            from contextlib import redirect_stdout
            buf = StringIO()
            with redirect_stdout(buf):
                code = collab.main(argv)
            self.assertEqual(code, 1)
            self.assertFalse(missing_dir.exists())

        canary = 'CLI_NESTED_CANARY_/private/path'
        self.c.store.update(tid, state='unknown', resource_state={'secret': canary}, turn='bad-turn')
        with self.c.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS release_fences (task TEXT PRIMARY KEY, receipt TEXT)')
            db.execute(
                'INSERT OR REPLACE INTO release_fences (task, receipt) VALUES (?, ?)',
                (tid, json.dumps({
                    'state': 'released',
                    'operation': {'secret': canary},
                    'business_state': [canary],
                    'external_close_attempted': {'secret': canary},
                })),
            )
        rc_san, cli_san = self._run_cli(c2, 'recovery_status', '--task', tid)
        self.assertEqual(rc_san, 0, cli_san)
        self.assertNotIn(canary, json.dumps(cli_san))
        self.assertEqual(cli_san['resource_recovery'], {
            'task_id': tid,
            'turn': None,
            'business_state': 'unknown',
            'resource_state': 'released',
            'write': False,
            'receipt': {
                'state': 'released',
                'operation': 'unknown',
                'business_state': 'unknown',
                'external_close_attempted': None,
            },
        })
