import json
import sqlite3
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from omp_bridge import HostBridge


class HostBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.launches = []
        self.bridge = HostBridge(Path(self.tmp.name) / 'tasks.db', launch=lambda t: self.launches.append(t))
        self.a = dict(session_id='native-a', agent_id='main-a')
        self.b = dict(session_id='native-b', agent_id='main-b')
        self.key = self.bridge.bind(self.a)

    def call(self, action, **args):
        return self.bridge.handle(self.a, self.key, dict(action=action, **args))

    def test_json_bridge_explains_unsupported_teamwork_without_launch(self):
        import omp_bridge
        from unittest.mock import patch
        from contextlib import redirect_stdout
        from io import StringIO
        request=dict(db=str(self.bridge.store.path),host=self.a,capability=self.key,
                     args=dict(action='start',agent='agy',cwd=self.tmp.name,prompt='never send',model='oat'))
        output=StringIO()
        with patch.object(omp_bridge,'HostBridge',return_value=self.bridge), \
                patch.object(omp_bridge.sys,'stdin',StringIO(json.dumps(request))),redirect_stdout(output):
            rc=omp_bridge.main()
        self.assertEqual(rc,1)
        self.assertIn('Teamwork',json.loads(output.getvalue())['message'])
        self.assertEqual(self.call('list')['tasks'],[])
        self.assertEqual(self.launches,[])

    def test_omp_shorthand_resolves_and_rejects_before_launch(self):
        for model,mode,reason in (('ocs','off','conflict'),('4a','native','conflict'),
                                  ('4ct',None,'unsupported'),('ocss',None,'Invalid AGY shorthand')):
            with self.assertRaisesRegex(ValueError,reason):
                self.call('start',agent='agy',cwd=self.tmp.name,prompt='never send',model=model,agy_swarm=mode)
        self.assertEqual(self.call('list')['tasks'],[])
        self.assertEqual(self.launches,[])
        for model,expected,mode in ((None,'gemini-pro-orchestrator','native'),
                ('oc','gemini-pro-orchestrator','off'),('oas','gemini-pro-orchestrator-argon','native'),
                ('4a','gemini-4-argon','off'),('4as','gemini-4-argon','native')):
            task=self.call('start',agent='agy',cwd=self.tmp.name,prompt='synthetic',model=model)['task']
            self.assertEqual((task['model'],task['agy_swarm']),(expected,mode))
            self.assertEqual((self.launches[-1]['model'],self.launches[-1]['agy_swarm']),(expected,mode))

    def test_dialogue_queues_on_bound_owner_without_cancel_or_relaunch(self):
        task = self.call('start', agent='agy', prompt='owned', cwd=self.tmp.name)['task']
        self.bridge.store.update(task['id'], state='running', runner_pid=123)
        self.bridge.store.native_input_open(task['id'], 1, task['launch_id'], 1)
        args = dict(task_id=task['id'], prompt='revise', command_id='d1', expected_turn=1)
        result = self.call('dialogue', **args)['task']
        self.assertEqual((result['state'], result['turn'], result['commands']['d1']['status']), ('running', 1, 'accepted'))
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.call('dialogue', **args)['task'], result)
        with self.assertRaises(ValueError):
            self.call('dialogue', **dict(args, expected_turn=2, command_id='stale'))

    def test_model_limits_rejected_but_legacy_budget_can_be_cleared(self):
        for value in (300, None):
            with self.assertRaisesRegex(ValueError, 'timeout'):
                self.call('start', prompt='inspect', cwd='/project', timeout=value)
        self.assertEqual(self.call('list')['tasks'], [])
        task = self.call('start', prompt='inspect', cwd='/project')['task']
        tid = task['id']
        self.bridge.store.update(tid, timeout=420, state='running')
        self.bridge.store.event(tid, 'q1', 'question', 'Q1 format?')
        with self.assertRaises(ValueError):
            self.call('status', task_id=tid, timeout=300)
        self.assertEqual(self.call('wait', task_id=tid, timeout=0)['task']['state'], 'running')
        pending = self.call('intervene', task_id=tid, prompt='revise', command_id='clear', clear_timeout=True)['task']
        self.assertEqual((pending['state'], pending['timeout']), ('cancelling', 420))
        self.bridge.store.finish_turn(tid, 1, 'cancelled', 0, 'cancelled')
        resumed = self.call('wait', task_id=tid, timeout=0)['task']
        self.assertEqual((resumed['turn'], resumed['timeout']), (2, None))
        self.bridge.store.update(tid, timeout=300, state='waiting_reply')
        self.bridge.store.event(tid, 'q2', 'question', 'Q1 continue?')
        replied = self.call('reply', task_id=tid, question_id='q2', prompt='yes', clear_timeout=True)['task']
        self.assertEqual((replied['turn'], replied['timeout']), (3, None))

    def test_native_binding_rejects_other_session_and_model_origin(self):
        task = self.call('start', prompt='inspect', cwd='/project', agent='omp')['task']
        self.assertEqual(task['host_binding'], 'omp-extension')
        self.assertNotIn('origin', task)
        self.assertNotIn(self.key, str(task))
        self.assertNotIn(self.a['session_id'], json.dumps(task))
        self.assertNotIn(self.a['agent_id'], json.dumps(task))
        with self.assertRaises(PermissionError):
            self.bridge.handle(self.b, self.key, dict(action='status', task_id=task['id']))
        with self.assertRaises(ValueError):
            self.call('start', prompt='inspect', cwd='/project', origin=self.a)

    def test_progress_question_cancel_resume_keep_exact_task_and_cursor(self):
        task = self.call('start', prompt='inspect', cwd='/project', agent='omp')['task']
        tid = task['id']
        self.bridge.store.update(tid, state='running')
        self.bridge.store.event(tid, '1:2', 'stage', 'real progress', turn=1)
        self.bridge.store.event(tid, '1:3', 'question', 'Q1 format?', turn=1)
        packet = self.call('wait', task_id=tid, after=0, timeout=0)
        self.assertEqual([e['kind'] for e in packet['events']], ['stage', 'question'])
        self.assertEqual(self.call('wait', task_id=tid, after=packet['after'], timeout=0)['events'], [])
        self.call('intervene', task_id=tid, prompt='revised', command_id='stable')
        self.assertEqual(self.call('status', task_id=tid)['task']['state'], 'cancelling')
        self.bridge.store.finish_turn(tid, 1, 'cancelled', 0, 'cancelled')
        resumed = self.call('wait', task_id=tid, after=packet['after'], timeout=0)['task']
        self.assertEqual((resumed['turn'], resumed['session']), (2, task['session']))
        self.assertEqual(len(self.launches), 2)
        self.call('intervene', task_id=tid, prompt='revised', command_id='stable')
        self.assertEqual(len(self.launches), 2)

    def test_distinct_valid_host_binding_cannot_control_other_task_or_bypass_writer(self):
        task = self.call('start', prompt='write', cwd='/project', agent='omp', write=True)['task']
        other = self.bridge.bind(self.b)
        with self.assertRaises(PermissionError):
            self.bridge.handle(self.b, other, dict(action='cancel', task_id=task['id']))
        with self.assertRaises(ValueError):
            self.bridge.handle(self.b, other, dict(action='start', prompt='write', cwd='/project/sub', agent='omp', write=True))
        self.assertEqual(self.call('status', task_id=task['id'])['task']['state'], 'queued')

    def test_one_shot_wait_locked_initialization_is_busy_and_preserves_task(self):
        task = self.call('start', prompt='inspect', cwd='/project', agent='omp')['task']
        request = dict(db=str(self.bridge.store.path), host=self.a, capability=self.key,
                       args=dict(action='wait', task_id=task['id'], after=17, timeout=0))
        with closing(sqlite3.connect(self.bridge.store.path)) as lock:
            lock.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            result = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('omp_bridge.py'))],
                                    input=json.dumps(request), text=True, capture_output=True, timeout=2)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), dict(task=None, events=[], after=17, busy=True))
        self.assertEqual(self.call('status', task_id=task['id'])['task'], task)
        packet = self.call('wait', task_id=task['id'], after=17, timeout=0)
        self.assertFalse(packet['busy'])
        self.assertEqual(packet['task']['state'], 'queued')


class CloseReceiptPrivacyTests(unittest.TestCase):
    def test_cli_and_omp_receipts_hide_nested_identity_on_success_failure_and_retry(self):
        from unittest.mock import patch
        from fixtures.close_backend import CloseBackend
        from collab import Collaboration
        for surface in ('cli','omp'):
            with self.subTest(surface=surface), tempfile.TemporaryDirectory() as td:
                bridge = (HostBridge if surface=='omp' else Collaboration)(Path(td)/'tasks.db',launch=lambda task:None)
                host = dict(session_id='PRIVATE_SESSION',agent_id='PRIVATE_AGENT')
                if surface=='omp':
                    client=bridge.bind(host)
                    task=bridge.handle(host,client,dict(action='start',cwd=td,prompt='private prompt'))['task']
                    close=lambda:bridge.handle(host,client,dict(action='close',task_id=task['id']))
                else:
                    client=bridge.connect('pi')['client_id']
                    task=bridge.start(client,cwd=td,prompt='private prompt')
                    close=lambda:bridge.close(client,task['id'])
                bridge.store.update(task['id'],state='completed',acpx_record_id='fixture-record')
                backend=CloseBackend(verified=False)
                with patch('release.NativeBackend',return_value=backend):
                    packets=[close(),close()]
                    backend.verified=True
                    packets.extend([close(),close()])
                self.assertEqual(backend.closes,1)
                for packet in packets:
                    projected=packet['task']['close_receipt'] if surface=='omp' else packet['close_receipt']
                    self.assertLessEqual(set(projected),{'state','phase','stage','reason_code','external_close_attempted'})
                self.assertEqual([p['task']['state'] if surface=='omp' else p['state'] for p in packets],
                                 ['unknown','unknown','closed','closed'])
                card=packets[0]['task']['recovery'] if surface=='omp' else packets[0]['recovery']
                self.assertTrue(card['capabilities']['exact_record_reconcile'])
                self.assertIn('close (probe-only)',card['next_action'])
                if surface=='omp':
                    for packet in packets:
                        raw=json.dumps(packet)
                        for private in (*host.values(),client): self.assertNotIn(private,raw)
                with bridge.store.connect() as db:
                    receipt=json.loads(db.execute('SELECT receipt FROM release_fences WHERE task=?',(task['id'],)).fetchone()[0])
                self.assertEqual(receipt['target']['owner'],bridge.store.get(task['id'])['origin'])
                self.assertGreaterEqual(len(receipt['attempts']),3)
                nested=HostBridge.public({'events':[{'close_receipt':receipt}],'task':{'close_receipt':receipt}})
                self.assertLessEqual(set(nested['events'][0]['close_receipt']),
                                     {'state','phase','stage','reason_code','external_close_attempted'})
                self.assertNotIn('target',json.dumps(nested))


if __name__ == '__main__':
    unittest.main()
