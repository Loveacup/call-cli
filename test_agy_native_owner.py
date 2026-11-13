import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from collab import Collaboration
from runner import run_task


class NativeOwnerTests(unittest.TestCase):
    def test_first_end_turn_retains_writer_until_complete_replay_and_cleanup(self):
        self.exercise_native_owner()

    def test_recovered_result_without_raw_kill_receipts_settles_and_releases(self):
        self.exercise_native_owner(missing_receipts=True)

    def test_fresh_owner_missing_turn_result_is_saved_and_released(self):
        with patch.dict(os.environ, AGY_NATIVE_OMIT_TURN='1'):
            self.exercise_native_owner(missing_receipts=True)

    def exercise_native_owner(self, missing_receipts=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'agy'
            executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], AGY_NATIVE_FIXTURE=str(root),
                            AGY_NATIVE_NO_RECEIPTS='1' if missing_receipts else ''), patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), patch('updates.ensure_current', return_value={'decision':'fixture'}):
                c = Collaboration(root/'tasks.db', launch=lambda task: None)
                client = c.connect('omp')['client_id']
                task = c.start(client, agent='agy', cwd=directory, prompt='owned task', write=True)
                (root/'task-id').write_text(task['id'])
                errors = []
                def run():
                    try:
                        run_task(c.store.path, task['id'], turn=task['turn'], launch_id=task['launch_id'], epoch=1)
                    except BaseException as exc:
                        errors.append(exc)
                thread = threading.Thread(target=run)
                with contextlib.redirect_stdout(io.StringIO()):
                    thread.start()
                    try:
                        deadline = time.monotonic()+5
                        while not (root/'loaded').exists() and thread.is_alive() and time.monotonic() < deadline:
                            time.sleep(.02)
                        self.assertTrue((root/'loaded').exists(), errors)
                        self.assertEqual(c.status(client, task['id'])['state'], 'running')
                        with self.assertRaisesRegex(ValueError, 'already owned'):
                            c.start(client, agent='omp', cwd=directory, prompt='conflict', write=True)
                    finally:
                        (root/'final').touch()
                        thread.join(8)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                result = c.status(client, task['id'])
                self.assertEqual(result['state'], 'completed', result.get('settlement_note'))
                events = c.events(client, task['id'])
                self.assertEqual([e['text'] for e in events if e['kind']=='answer'].count('same legitimate answer'), 2)
                self.assertEqual(len([e for e in events if e['kind']=='stage' and e['text']=='workers launched']), 1)
                requests = [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
                self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests), 1)
                self.assertTrue((root/'prompt').read_text().startswith('/swarm\n'))
                self.assertEqual(result['native_result']['turn'], task['turn'])
                self.assertEqual(result['native_cleanup']['turn'], task['turn'])
                self.assertEqual(result['native_cleanup']['workers'], ['worker-a'])
                self.assertFalse(result['native_cleanup']['all_descendants_proven_stopped'])
                self.assertTrue(result['native_cleanup']['session_unloaded'])
                self.assertEqual(c.close(client, task['id'])['state'], 'closed')

    def test_live_replay_shape_differences_do_not_drop_new_results(self):
        from agy_native import first_replay, additions, ProtocolError
        def text(value):
            return dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=value))
        published = [dict(sessionUpdate='user_message_chunk', content=dict(type='text', text='echo')),
                     dict(sessionUpdate='tool_call', toolCallId='t1', title='live'), text('already published')]
        replay = [dict(sessionUpdate='tool_call', toolCallId='t1', title='persisted'),
                  text('already published'), text('new final')]
        self.assertEqual(first_replay(published, replay), [text('new final')])
        self.assertEqual(additions([text('partial')], [text('partial final')]), [text(' final')])
        with self.assertRaises(ProtocolError):
            additions([text('original')], [text('rewritten')])

    def test_native_unknown_cannot_release_using_an_acpx_record(self):
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']
            task = c.start(client, agent='agy', cwd=root, prompt='owned', write=True)
            c.store.update(task['id'], state='unknown', acpx_record_id='fake-record')
            with patch('release.NativeBackend') as backend:
                result = c.close(client, task['id'])
                backend.assert_not_called()
            self.assertEqual(result['close_receipt']['reason_code'], 'native_workers_unproven')
            self.assertEqual(result['state'], 'unknown')

    def test_cancel_before_native_creation_can_close_without_backend_control(self):
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']
            task = c.start(client, agent='agy', cwd=root, prompt='owned', write=True)
            c.cancel(client, task['id'])
            self.assertEqual(c.close(client, task['id'])['state'], 'closed')

    def test_foreground_and_background_cancel_retain_unknown_worker_occupancy(self):
        for foreground in (True, False):
            with self.subTest(foreground=foreground), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                executable = root/'agy'
                executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
                executable.chmod(0o700)
                with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'],
                                AGY_NATIVE_FIXTURE=str(root), AGY_NATIVE_BLOCK='1' if foreground else ''), patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), patch('updates.ensure_current', return_value={'decision':'fixture'}):
                    c = Collaboration(root/'tasks.db', launch=lambda task: None)
                    client = c.connect('omp')['client_id']
                    task = c.start(client, agent='agy', cwd=directory, prompt='owned', write=True)
                    thread = threading.Thread(target=lambda: run_task(c.store.path, task['id'],
                        turn=1, launch_id=task['launch_id'], epoch=1))
                    with contextlib.redirect_stdout(io.StringIO()):
                        thread.start()
                        gate = root/('prompt' if foreground else 'loaded')
                        deadline = time.monotonic()+5
                        while not gate.exists() and thread.is_alive() and time.monotonic()<deadline:
                            time.sleep(.02)
                        c.cancel(client, task['id'])
                        thread.join(8)
                    self.assertFalse(thread.is_alive())
                    result = c.status(client, task['id'])
                    self.assertEqual(result['state'], 'unknown')
                    self.assertEqual(result['native_cancel']['workers'], 'unknown')
                    self.assertTrue(result['native_cancel']['session_unloaded'])
                    self.assertEqual(c.close(client, task['id'])['close_receipt']['reason_code'], 'native_workers_unproven')
                    with self.assertRaisesRegex(ValueError, 'already owned'):
                        c.start(client, agent='omp', cwd=directory, prompt='conflict', write=True)

    def test_public_host_reads_hide_replay_snapshot_but_keep_result(self):
        from omp_bridge import HostBridge
        from test_plugin import load_plugin, Ctx
        with tempfile.TemporaryDirectory() as root:
            host = dict(session_id='owned-host', agent_id='main')
            bridge = HostBridge(Path(root)/'omp.db', launch=lambda task: None)
            key = bridge.bind(host)
            task = bridge.handle(host, key, dict(action='start', agent='agy', cwd=root, prompt='owned'))['task']
            bridge.store.update(task['id'], native_snapshot=[{'private_replay':'not-for-host'}],
                                native_result={'result':'public result'})
            for action in ('status', 'wait', 'list', 'events'):
                args = dict(action=action)
                if action != 'list':
                    args['task_id'] = task['id']
                if action == 'wait':
                    args['timeout'] = 0
                packet = bridge.handle(host, key, args)
                self.assertNotIn('native_snapshot', json.dumps(packet))
            self.assertEqual(bridge.handle(host, key, dict(action='status', task_id=task['id']))['task']['native_result'],
                             {'result':'public result'})
            self.assertIn('native_snapshot', bridge.store.get(task['id']))
            mod = load_plugin()
            env = {'HERMES_SESSION_ID':'owned', 'HERMES_SESSION_KEY':'owned-key'}
            coordinator = mod.Coordinator(Ctx(), Path(root), lambda k,d='':env.get(k,d))
            task = coordinator.handle(dict(action='start', agent='agy', cwd=root, prompt='owned'))['task']
            store = mod.Store(Path(root)/'call-cli/tasks.sqlite3')
            store.update(task['id'], native_snapshot=[{'private_replay':'not-for-host'}],
                         native_result={'result':'public result'})
            for action in ('status', 'events', 'list'):
                args = dict(action=action)
                if action != 'list':
                    args['task_id'] = task['id']
                self.assertNotIn('native_snapshot', json.dumps(coordinator.handle(args)))
            self.assertEqual(coordinator.handle(dict(action='status', task_id=task['id']))['task']['native_result'],
                             {'result':'public result'})

    def test_native_fatal_exit_and_missing_kill_notify_initiator_and_hold(self):
        cases = [('AGY_NATIVE_FATAL', 'foreground', 'fatal execution error'),
                 ('AGY_NATIVE_FATAL', 'background', 'fatal execution error'),
                 ('AGY_NATIVE_EXIT', 'foreground', 'disconnected'),
                 ('AGY_NATIVE_EXIT', 'background', 'disconnected'),
                 ('AGY_NATIVE_MISSING_KILL', '1', 'add/kill/model')]
        for variable, value, diagnosis in cases:
            with self.subTest(variable=variable, value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                executable = root/'agy'
                executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
                executable.chmod(0o700)
                env = dict(PATH=str(root)+os.pathsep+os.environ['PATH'], AGY_NATIVE_FIXTURE=str(root), **{variable:value})
                with patch.dict(os.environ, env), patch('updates.ensure_current', return_value={'decision':'fixture'}), patch('updates.DEFAULT_LOCK_ROOT', root/'locks'):
                    c = Collaboration(root/'tasks.db', launch=lambda task: None)
                    client = c.connect('omp')['client_id']
                    task = c.start(client, agent='agy', cwd=directory, prompt='owned', write=True)
                    (root/'task-id').write_text(task['id'])
                    (root/'final').touch()
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1), 1)
                    result = c.status(client, task['id'])
                    self.assertEqual(result['state'], 'unknown')
                    self.assertIn(diagnosis, result['settlement_note'])
                    event = c.events(client, task['id'])[-1]
                    self.assertTrue(event['attention_required'])
                    self.assertEqual(event['notification_scope'], 'originating-session')
                    self.assertIn('Do not replay', event['next_action'])
                    notice = [json.loads(line) for line in output.getvalue().splitlines()][-1]
                    self.assertEqual((notice['task_id'], notice['event'], notice['turn']), (task['id'], 'unknown', 1))
                    self.assertTrue(notice['attention_required'])
                    self.assertEqual(notice['text'], event['text'])
                    self.assertIn('needs initiator attention', event['text'])
                    with self.assertRaises(ValueError):
                        c.intervene(client, task['id'], 'do not replay', command_id='review')
                    self.assertEqual(c.close(client, task['id'])['state'], 'unknown')

    def test_successor_cannot_reuse_previous_result_cleanup_or_roster(self):
        from agy_native import completion, ProtocolError
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']
            task = c.start(client, agent='agy', cwd=root, prompt='owned')
            c.store.update(task['id'], state='completed', native_result={'turn':1},
                           native_cleanup={'turn':1,'workers':['old'],'session_unloaded':True},
                           native_roster=['old'], native_snapshot=[{'old':'history'}])
            successor = c.intervene(client, task['id'], 'next', command_id='next')
            for key in ('native_result','native_cleanup','native_roster','native_snapshot'):
                self.assertNotIn(key, successor)
            successor['agy_worker_model'] = 'gemini-4-hydrogen-medium'
            message = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_RESULT '+json.dumps(
                dict(task=task['id'], turn=1, status='completed', result='old', workers=['old']))))
            self.assertIsNone(completion([message], successor))
            message['content']['text'] = message['content']['text'].replace('"turn": 1','"turn": 2')
            with self.assertRaises(ProtocolError):
                completion([message], successor)

    def test_same_session_successor_uses_only_its_new_worker_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'agy'
            executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], AGY_NATIVE_FIXTURE=str(root),
                            AGY_NATIVE_NO_RECEIPTS='1'), patch('updates.ensure_current', return_value={'decision':'fixture'}), patch('updates.DEFAULT_LOCK_ROOT', root/'locks'):
                c = Collaboration(root/'tasks.db', launch=lambda task: None)
                client = c.connect('omp')['client_id']
                task = c.start(client, agent='agy', cwd=directory, prompt='owned')
                (root/'task-id').write_text(task['id'])
                (root/'final').touch()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1), 0)
                first_session = c.status(client, task['id'])['native_session_id']
                successor = c.intervene(client, task['id'], 'next', command_id='next')
                self.assertNotIn('native_result', successor)
                with patch.dict(os.environ, AGY_NATIVE_TURN='2'), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(run_task(c.store.path, task['id'], turn=2, launch_id=successor['launch_id'], epoch=1), 0)
                result = c.status(client, task['id'])
                self.assertEqual(result['native_session_id'], first_session)
                self.assertEqual(result['native_result']['turn'], 2)
                self.assertEqual(result['native_cleanup']['turn'], 2)
                self.assertEqual([e['text'] for e in c.events(client, task['id']) if e['kind']=='answer' and e['turn']==2].count('same legitimate answer'), 2)
                self.assertEqual(c.close(client, task['id'])['state'], 'closed')

    def test_missing_turn_requires_fresh_first_owner_and_never_applies_to_successor(self):
        from agy_native import completion
        from types import SimpleNamespace
        task = dict(id='owned', turn=1, native_session_id='owned-session', agy_worker_model='gemini-4-hydrogen-medium')
        history = [dict(sessionUpdate='tool_call', toolCallId='add', status='completed',
                        swarm=dict(action='add', name='worker', model=task['agy_worker_model'])),
                   dict(sessionUpdate='tool_call', toolCallId='kill', status='completed', swarm=dict(action='kill', name='worker')),
                   dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_RESULT '+json.dumps(
                       dict(task='owned', status='completed', result='actual result', workers=['worker']))))]
        owner = SimpleNamespace(task=task, session_id='owned-session', created_session=True, prompt_returned=True)
        self.assertIsNone(completion(history, task))
        recorded = []
        self.assertEqual(completion(history, task, recorded.append, owner=owner, baseline=[]), ['worker'])
        self.assertEqual(recorded[0]['original_envelope'], dict(task='owned', status='completed', result='actual result', workers=['worker']))
        self.assertEqual(recorded[0]['turn_attribution'], 'fresh-owner-first-submission')
        for changes in (dict(turn=2), dict(native_session_id='different'), dict(id='different')):
            self.assertIsNone(completion(history, dict(task, **changes), owner=owner, baseline=[]))
        for changes in (dict(created_session=False), dict(prompt_returned=False)):
            self.assertIsNone(completion(history, task, owner=SimpleNamespace(**dict(vars(owner), **changes)), baseline=[]))
        self.assertIsNone(completion(history, task, owner=owner, baseline=history))
