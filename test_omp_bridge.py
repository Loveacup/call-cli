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

    def test_json_bridge_routes_teamwork_without_swarm_substitution(self):
        result=self.call('start',agent='agy',cwd=self.tmp.name,prompt='native request',model='oat')
        task=result['task']
        self.assertEqual((task['agy_mode'],task['model']),('teamwork','gemini-pro-orchestrator-argon'))
        self.assertEqual(self.launches[0]['agy_mode'],'teamwork')

    def test_omp_shorthand_resolves_and_rejects_before_launch(self):
        for model,mode,reason in (('ocs','off','conflict'),('4a','native','conflict'),
                                  ('ocss',None,'Invalid AGY shorthand')):
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
        sup = self.call('dialogue', task_id=task['id'], prompt='revise 2', command_id='d2',
                        expected_turn=1, target_command_id='d1')['task']
        self.assertEqual((sup['commands']['d1']['status'], sup['commands']['d2']['status']), ('superseded', 'accepted'))
        wd = self.call('dialogue', task_id=task['id'], command_id='w2', expected_turn=1,
                       target_command_id='d2', withdraw=True)['task']
        self.assertEqual(wd['commands']['d2']['status'], 'withdrawn')
        self.assertEqual(self.call('dialogue', task_id=task['id'], command_id='w2', expected_turn=1,
                                   target_command_id='d2', withdraw=True)['task'], wd)
        for bad in (dict(action='status', task_id=task['id'], target_command_id='d1'),
                    dict(action='status', task_id=task['id'], withdraw=True),
                    dict(action='dialogue', task_id=task['id'], command_id='bad', expected_turn=1,
                         target_command_id='d1', withdraw='yes')):
            with self.assertRaises(ValueError):
                self.call(bad.pop('action'), **bad)

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
        public_fields={'state','phase','stage','reason_code','external_close_attempted',
                       'resource_state','business_state','risk','arbitraryDetachedDescendantsProven','operation'}
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
                for index,packet in enumerate(packets):
                    projected=packet['task']['close_receipt'] if surface=='omp' else packet['close_receipt']
                    self.assertLessEqual(set(projected),public_fields)
                    if index < 3:
                        self.assertEqual(projected['operation'],'native-recovery')
                        self.assertEqual(projected['business_state'],'completed')
                        self.assertIs(projected['arbitraryDetachedDescendantsProven'],False)
                    else:
                        self.assertEqual(projected,dict(state='released',phase='verified',operation='close'))
                    for private in (*host.values(),client,'private prompt','fixture-record'):
                        self.assertNotIn(private,json.dumps(projected))
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
                for projected in (nested['events'][0]['close_receipt'],nested['task']['close_receipt']):
                    self.assertEqual(projected,{key:value for key,value in receipt.items() if key in public_fields})
                for private in ('target','owner','actor','attempts','proof',*host.values(),client,'fixture-record'):
                    self.assertNotIn(private,json.dumps(nested))


class GroupAndOriginContextPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = HostBridge(Path(self.tmp.name) / 'tasks.db', launch=lambda t: None)
        self.a = dict(session_id='secret-omp-sess-a', agent_id='secret-omp-agent-a')
        self.b = dict(session_id='secret-omp-sess-b', agent_id='secret-omp-agent-b')
        self.ka = self.bridge.bind(self.a)
        self.kb = self.bridge.bind(self.b)

    def test_host_context_group_runtime_links_and_privacy_scrubbing(self):
        res_a1 = self.bridge.handle(
            self.a, self.ka,
            dict(action='start', cwd=self.tmp.name, prompt='one', group=' grp '),
            host_context={'session_name': '\x1b[31mRefactor\x1b[0m Auth\u202e'},
        )
        task_a1 = res_a1['task']
        self.assertEqual(task_a1['group'], 'grp')
        self.assertEqual(task_a1['origin_context']['surface'], 'omp-extension')
        self.assertEqual(task_a1['origin_context']['display_name'], 'Refactor Auth')
        self.assertEqual(task_a1['runtime_links']['task_id'], task_a1['id'])
        self.assertNotIn('origin', task_a1)

        task_b1 = self.bridge.handle(
            self.b, self.kb,
            dict(action='start', cwd=self.tmp.name, prompt='other', group='grp'),
            host_context={'session_name': f"Leak {self.b['session_id']} {self.b['agent_id']} {self.kb}"},
        )['task']
        self.assertEqual(task_b1['origin_context']['session_name'], 'unknown')
        self.assertEqual(task_b1['origin_context']['display_name'], 'unknown')
        for secret in (self.b['session_id'], self.b['agent_id'], self.kb):
            self.assertNotIn(secret, json.dumps(task_b1))

        self.bridge.handle(self.a, self.ka, dict(action='start', cwd=self.tmp.name, prompt='two', group='other'))
        listed_a = self.bridge.handle(self.a, self.ka, dict(action='list', group='grp'))['tasks']
        self.assertEqual([t['id'] for t in listed_a], [task_a1['id']])
        listed_b = self.bridge.handle(self.b, self.kb, dict(action='list', group='grp'))['tasks']
        self.assertEqual([t['id'] for t in listed_b], [task_b1['id']])

        for bad_args in (dict(action='status', task_id=task_a1['id'], group='grp'),
                         dict(action='start', cwd=self.tmp.name, prompt='bad', group='   '),
                         dict(action='list', group='')):
            with self.assertRaises(ValueError):
                self.bridge.handle(self.a, self.ka, bad_args)

        from io import StringIO
        from unittest.mock import patch
        import omp_bridge
        req = dict(db=str(self.bridge.store.path), host=self.a, capability=self.ka,
                   host_context={'session_name': 'CLI Subprocess Title'},
                   args=dict(action='start', cwd=self.tmp.name, prompt='via main', group='sub'))
        out = StringIO()
        with patch.object(HostBridge, '_launch', lambda self, task: None), \
             patch('sys.stdin', StringIO(json.dumps(req))), \
             patch('sys.stdout', out):
            rc = omp_bridge.main()
        self.assertEqual(rc, 0, out.getvalue())
        started = json.loads(out.getvalue())
        self.assertEqual(started['task']['origin_context']['display_name'], 'CLI Subprocess Title')
        self.assertEqual(started['task']['group'], 'sub')
        self.assertEqual(started['task']['runtime_links']['task_id'], started['task']['id'])

        list_req = dict(db=str(self.bridge.store.path), host=self.a, capability=self.ka,
                        args=dict(action='list', group='sub'))
        proc = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('omp_bridge.py'))],
                              input=json.dumps(list_req), text=True, capture_output=True, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        listed_sub = json.loads(proc.stdout)['tasks']
        self.assertEqual([t['id'] for t in listed_sub], [started['task']['id']])
        self.assertEqual(listed_sub[0]['origin_context']['display_name'], 'CLI Subprocess Title')
        self.assertEqual(listed_sub[0]['runtime_links']['task_id'], started['task']['id'])
        for secret in (self.a['session_id'], self.a['agent_id'], self.ka):
            self.assertNotIn(secret, proc.stdout)

    def test_ansi_and_control_split_private_tokens_never_reassemble_in_public_context(self):
        host = dict(session_id='fixture-session-private', agent_id='fixture-agent-private')
        cap = self.bridge.bind(host)

        # Report §66 counterexample: ANSI CSI split inside private session_id
        res = self.bridge.handle(
            host, cap,
            dict(action='start', cwd=self.tmp.name, prompt='counterexample', group='sec'),
            host_context={'session_name': 'fixture-session-\x1b[0mprivate'},
        )
        task = res['task']
        self.assertEqual(task['origin_context']['session_name'], 'unknown')
        self.assertEqual(task['origin_context']['display_name'], 'unknown')
        self.assertNotIn('fixture-session-private', json.dumps(res))
        self.assertEqual(self.bridge.store.get(task['id'])['origin_context']['display_name'], 'unknown')

        # Bidi + NUL split agent_id
        bidi_res = self.bridge.handle(
            host, cap,
            dict(action='start', cwd=self.tmp.name, prompt='bidi-nul', group='sec'),
            host_context={'session_name': 'Room fixture-agent-\u202e\x00private'},
        )
        self.assertEqual(bidi_res['task']['origin_context']['session_name'], 'unknown')
        self.assertEqual(bidi_res['task']['origin_context']['display_name'], 'unknown')
        self.assertNotIn('fixture-agent-private', json.dumps(bidi_res))
        self.assertEqual(self.bridge.store.get(bidi_res['task']['id'])['origin_context']['display_name'], 'unknown')

        # OSC + C1 CSI split capability
        osc_res = self.bridge.handle(
            host, cap,
            dict(action='start', cwd=self.tmp.name, prompt='osc-csi', group='sec'),
            host_context={'session_name': f'Room {cap[:8]}\x1b]0;t\x07{cap[8:16]}\x9b31m{cap[16:]}'},
        )
        self.assertEqual(osc_res['task']['origin_context']['session_name'], 'unknown')
        self.assertEqual(osc_res['task']['origin_context']['display_name'], 'unknown')
        self.assertNotIn(cap, json.dumps(osc_res))
        self.assertEqual(self.bridge.store.get(osc_res['task']['id'])['origin_context']['display_name'], 'unknown')

        # 160-char truncation-straddling split session_id (no truncated prefix leaks)
        trunc_res = self.bridge.handle(
            host, cap,
            dict(action='start', cwd=self.tmp.name, prompt='trunc-split', group='sec'),
            host_context={'session_name': ('A' * 150) + ' fixture-session-\x1b[0mprivate'},
        )
        self.assertEqual(trunc_res['task']['origin_context']['session_name'], 'unknown')
        self.assertEqual(trunc_res['task']['origin_context']['display_name'], 'unknown')
        self.assertNotIn('fixture-session-private', json.dumps(trunc_res))
        self.assertNotIn('fixture-', json.dumps(trunc_res))
        self.assertEqual(self.bridge.store.get(trunc_res['task']['id'])['origin_context']['display_name'], 'unknown')

        # Positive preservation: plain and styled normal titles are preserved cleanly
        plain_res = self.bridge.handle(
            host, cap,
            dict(action='start', cwd=self.tmp.name, prompt='plain-title', group='norm'),
            host_context={'session_name': 'Normal Session Title'},
        )
        self.assertEqual(plain_res['task']['origin_context']['session_name'], 'Normal Session Title')
        self.assertEqual(plain_res['task']['origin_context']['display_name'], 'Normal Session Title')

        styled_res = self.bridge.handle(
            host, cap,
            dict(action='start', cwd=self.tmp.name, prompt='styled-title', group='norm'),
            host_context={'session_name': '\x1b[32mSprint\x1b[0m \u200eBoard'},
        )
        self.assertEqual(styled_res['task']['origin_context']['session_name'], 'Sprint Board')
        self.assertEqual(styled_res['task']['origin_context']['display_name'], 'Sprint Board')

        # Subprocess seam: start (with _launch stubbed) and list across ANSI-split secret and styled normal title
        bridge_dir = str(Path(__file__).resolve().parent)
        wrapper = (
            f'import sys; sys.path.insert(0, {bridge_dir!r}); '
            'import collab, omp_bridge; '
            'collab.Collaboration._launch = lambda self, task: None; '
            'raise SystemExit(omp_bridge.main())'
        )
        sub_split_req = dict(
            db=str(self.bridge.store.path), host=host, capability=cap,
            host_context={'session_name': 'fixture-session-\x1b[0mprivate'},
            args=dict(action='start', cwd=self.tmp.name, prompt='sub split', group='subsec'),
        )
        proc_split = subprocess.run(
            [sys.executable, '-B', '-c', wrapper],
            input=json.dumps(sub_split_req), text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(proc_split.returncode, 0, proc_split.stdout + proc_split.stderr)
        sub_split = json.loads(proc_split.stdout)
        self.assertEqual(sub_split['task']['origin_context']['session_name'], 'unknown')
        self.assertEqual(sub_split['task']['origin_context']['display_name'], 'unknown')
        for secret in (host['session_id'], host['agent_id'], cap):
            self.assertNotIn(secret, proc_split.stdout)

        sub_styled_req = dict(
            db=str(self.bridge.store.path), host=host, capability=cap,
            host_context={'session_name': '\x1b[32mSprint\x1b[0m \u200eBoard'},
            args=dict(action='start', cwd=self.tmp.name, prompt='sub styled', group='subnorm'),
        )
        proc_styled = subprocess.run(
            [sys.executable, '-B', '-c', wrapper],
            input=json.dumps(sub_styled_req), text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(proc_styled.returncode, 0, proc_styled.stdout + proc_styled.stderr)
        sub_styled = json.loads(proc_styled.stdout)
        self.assertEqual(sub_styled['task']['origin_context']['session_name'], 'Sprint Board')
        self.assertEqual(sub_styled['task']['origin_context']['display_name'], 'Sprint Board')

        for group_name, expected_id, expected_name in (
            ('subsec', sub_split['task']['id'], 'unknown'),
            ('subnorm', sub_styled['task']['id'], 'Sprint Board'),
        ):
            list_proc = subprocess.run(
                [sys.executable, '-B', str(Path(__file__).with_name('omp_bridge.py'))],
                input=json.dumps(dict(db=str(self.bridge.store.path), host=host, capability=cap,
                                      args=dict(action='list', group=group_name))),
                text=True, capture_output=True, timeout=5,
            )
            self.assertEqual(list_proc.returncode, 0, list_proc.stdout + list_proc.stderr)
            listed = json.loads(list_proc.stdout)['tasks']
            self.assertEqual([t['id'] for t in listed], [expected_id])
            self.assertEqual(listed[0]['origin_context']['session_name'], expected_name)
            self.assertEqual(listed[0]['origin_context']['display_name'], expected_name)
            for secret in (host['session_id'], host['agent_id'], cap):
                self.assertNotIn(secret, list_proc.stdout)


if __name__ == '__main__':
    unittest.main()
