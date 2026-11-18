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


class QuestionIdsSeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.launches = []
        self.bridge = HostBridge(Path(self.tmp.name) / 'tasks.db', launch=lambda t: self.launches.append(t))
        self.host = dict(session_id='native-qids', agent_id='main-qids')
        self.cap = self.bridge.bind(self.host)

    def call(self, action, **args):
        return self.bridge.handle(self.host, self.cap, dict(action=action, **args))

    def test_dialogue_question_ids_raw_bytes_idempotency_supersede_and_withdraw(self):
        task = self.call('start', agent='agy', prompt='owned', cwd=self.tmp.name)['task']
        tid = task['id']
        self.bridge.store.update(tid, state='running', runner_pid=123)
        self.bridge.store.native_input_open(tid, 1, task['launch_id'], 1)
        self.bridge.store.event(tid, ' 1:raw ', 'question', 'Q1 raw?', turn=1)
        self.bridge.store.event(tid, '1:2', 'question', 'Q2 second?', turn=1)
        self.bridge.store.event(tid, '1:3', 'question', 'Q3 third?', turn=1)

        with self.assertRaisesRegex(ValueError, 'exact raw bytes'):
            self.call('dialogue', task_id=tid, prompt='trimmed', command_id='bad-trim',
                      expected_turn=1, question_ids=['1:raw'])

        res = self.call('dialogue', task_id=tid, prompt='multi answer', command_id='dq1',
                        expected_turn=1, question_ids=[' 1:raw ', '1:2'])['task']
        cmd = res['commands']['dq1']
        self.assertEqual(cmd['status'], 'accepted')
        self.assertEqual(cmd['question_ids'], [' 1:raw ', '1:2'])
        self.assertIsNone(cmd['question_id'])
        self.assertEqual([q['id'] for q in cmd['questions']], [' 1:raw ', '1:2'])
        self.assertEqual(res['questions'], [' 1:raw ', '1:2', '1:3'])
        self.assertEqual(len(self.launches), 1)

        # Idempotent replay with reordered question_ids succeeds cleanly.
        replayed = self.call('dialogue', task_id=tid, prompt='multi answer', command_id='dq1',
                             expected_turn=1, question_ids=['1:2', ' 1:raw '])['task']
        self.assertEqual(replayed, res)

        # Reusing command_id with a different question_ids set fails.
        with self.assertRaises(ValueError):
            self.call('dialogue', task_id=tid, prompt='multi answer', command_id='dq1',
                      expected_turn=1, question_ids=['1:2'])

        # Duplicate binding against an already-accepted dialogue is rejected.
        with self.assertRaisesRegex(ValueError, 'admitted answer'):
            self.call('dialogue', task_id=tid, prompt='overlap', command_id='dq-overlap',
                      expected_turn=1, question_ids=['1:2', '1:3'])

        # Supersede releases all previously bound IDs (' 1:raw ' and '1:2').
        sup = self.call('dialogue', task_id=tid, prompt='answer q3', command_id='dq2',
                        expected_turn=1, target_command_id='dq1', question_ids=['1:3'])['task']
        self.assertEqual((sup['commands']['dq1']['status'], sup['commands']['dq2']['status']),
                         ('superseded', 'accepted'))

        rebound = self.call('dialogue', task_id=tid, prompt='rebound', command_id='dq3',
                            expected_turn=1, question_ids=[' 1:raw ', '1:2'])['task']
        self.assertEqual(rebound['commands']['dq3']['status'], 'accepted')

        # Withdraw releases all bound IDs for subsequent re-answering.
        wd = self.call('dialogue', task_id=tid, command_id='wq3', expected_turn=1,
                       target_command_id='dq3', withdraw=True)['task']
        self.assertEqual(wd['commands']['dq3']['status'], 'withdrawn')

        rebound_after_wd = self.call('dialogue', task_id=tid, prompt='after withdraw', command_id='dq4',
                                     expected_turn=1, question_ids=['1:2', ' 1:raw '])['task']
        self.assertEqual(rebound_after_wd['commands']['dq4']['status'], 'accepted')
        self.assertEqual(rebound_after_wd['commands']['dq4']['question_ids'], ['1:2', ' 1:raw '])

    def test_reply_question_ids_subset_d4_merge_single_count_clear_timeout_and_idempotency(self):
        task = self.call('start', agent='codex', prompt='inspect', cwd=self.tmp.name)['task']
        tid = task['id']
        self.bridge.store.update(tid, state='waiting_reply', timeout=300)
        self.bridge.store.event(tid, 'q1', 'question', 'Q1 first?', turn=1)
        self.bridge.store.event(tid, 'q2', 'question', 'Q2 second?', turn=1)
        self.bridge.store.event(tid, 'q3', 'question', 'Q3 third?', turn=1)

        subset = self.call('reply', task_id=tid, question_ids=['q1', 'q2'], prompt='shared',
                           expected_turn=1, command_id='r1', clear_timeout=True)['task']
        self.assertEqual((subset['state'], subset['turn']), ('waiting_reply', 1))
        self.assertEqual(subset['questions'], ['q3'])
        self.assertEqual(subset['question_id'], 'q3')
        self.assertEqual(subset['answers'], {'q1': 'shared', 'q2': 'shared'})
        self.assertTrue(subset['clear_timeout_pending'])
        self.assertEqual(subset['timeout'], 300)
        self.assertEqual(len(self.launches), 1)

        # Reordered question_ids with same command_id is idempotent.
        self.assertEqual(
            self.call('reply', task_id=tid, question_ids=['q2', 'q1'], prompt='shared',
                      expected_turn=1, command_id='r1', clear_timeout=True)['task'],
            subset,
        )
        with self.assertRaises(ValueError):
            self.call('reply', task_id=tid, question_ids=['q1'], prompt='shared',
                      expected_turn=1, command_id='r1', clear_timeout=True)

        final = self.call('reply', task_id=tid, question_ids=['q3'], prompt='last',
                          expected_turn=1, command_id='r2')['task']
        self.assertEqual((final['state'], final['turn']), ('queued', 2))
        self.assertIsNone(final['timeout'])
        self.assertEqual(final['questions'], [])
        self.assertEqual(final['answers'], {})
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(self.launches[-1]['prompt'], 'q1: shared\nq2: shared\nq3: last')

        # Single-question reply via question_ids uses unprefixed prompt and preserves raw bytes.
        single = self.call('start', agent='codex', prompt='single', cwd=self.tmp.name)['task']
        sid = single['id']
        self.bridge.store.update(sid, state='waiting_reply')
        self.bridge.store.event(sid, ' 1:raw ', 'question', 'Q1 raw?', turn=1)
        with self.assertRaisesRegex(ValueError, 'exact raw bytes'):
            self.call('reply', task_id=sid, question_ids=['1:raw'], prompt='only', expected_turn=1, command_id='rs-bad')
        replied_single = self.call('reply', task_id=sid, question_ids=[' 1:raw '], prompt='only',
                                   expected_turn=1, command_id='rs1')['task']
        self.assertEqual((replied_single['state'], replied_single['turn']), ('queued', 2))
        self.assertEqual(self.launches[-1]['prompt'], 'only')

    def test_question_ids_validation_and_rejection_across_actions(self):
        dlg = self.call('start', agent='agy', prompt='owned', cwd=self.tmp.name)['task']
        self.bridge.store.update(dlg['id'], state='running', runner_pid=123)
        self.bridge.store.native_input_open(dlg['id'], 1, dlg['launch_id'], 1)
        self.bridge.store.event(dlg['id'], '1:2', 'question', 'Q1?', turn=1)
        self.bridge.store.event(dlg['id'], '1:3', 'question', 'Q2?', turn=1)

        rep = self.call('start', agent='codex', prompt='reply-host', cwd=self.tmp.name)['task']
        self.bridge.store.update(rep['id'], state='waiting_reply')
        self.bridge.store.event(rep['id'], '1:2', 'question', 'Q1?', turn=1)
        self.bridge.store.event(rep['id'], '1:3', 'question', 'Q2?', turn=1)
        launches_before = len(self.launches)

        invalid_values = [None, [], '1:2,1:3', [''], [1], ['1:2', '1:2'], ['1:2,1:3']]
        for bad_qids in invalid_values:
            with self.subTest(action='dialogue', bad_qids=bad_qids), self.assertRaises(ValueError):
                self.call('dialogue', task_id=dlg['id'], prompt='bad', command_id='d-bad',
                          expected_turn=1, question_ids=bad_qids)
            with self.subTest(action='reply', bad_qids=bad_qids), self.assertRaises(ValueError):
                self.call('reply', task_id=rep['id'], prompt='bad', expected_turn=1,
                          command_id='r-bad', question_ids=bad_qids)

        # Mutual exclusion including empty scalar question_id=''
        for qid in ('1:2', ''):
            with self.assertRaises(ValueError):
                self.call('dialogue', task_id=dlg['id'], prompt='bad', command_id='d-mix',
                          expected_turn=1, question_id=qid, question_ids=['1:3'])
            with self.assertRaises(ValueError):
                self.call('reply', task_id=rep['id'], prompt='bad', expected_turn=1,
                          command_id='r-mix', question_id=qid, question_ids=['1:3'])

        # Non-string question_id and withdraw combined with question_ids/question_id
        with self.assertRaises(ValueError):
            self.call('dialogue', task_id=dlg['id'], prompt='bad', command_id='d-int', expected_turn=1, question_id=123)
        with self.assertRaises(ValueError):
            self.call('reply', task_id=rep['id'], prompt='bad', expected_turn=1, question_id=123)

        self.call('dialogue', task_id=dlg['id'], prompt='ok', command_id='d-ok', expected_turn=1, question_ids=['1:2'])
        with self.assertRaises(ValueError):
            self.call('dialogue', task_id=dlg['id'], command_id='w-bad', expected_turn=1,
                      target_command_id='d-ok', withdraw=True, question_ids=['1:2'])

        # Unsupported actions reject question_ids
        for other_action, extra in (
            ('status', dict(task_id=dlg['id'])),
            ('intervene', dict(task_id=dlg['id'], prompt='rev', command_id='int-bad')),
            ('list', dict()),
            ('cancel', dict(task_id=dlg['id'])),
            ('close', dict(task_id=dlg['id'])),
        ):
            with self.subTest(other_action=other_action), self.assertRaises(ValueError):
                self.call(other_action, question_ids=['1:2'], **extra)

        self.assertEqual(len(self.launches), launches_before)
        self.assertEqual(self.call('status', task_id=rep['id'])['task']['questions'], ['1:2', '1:3'])

    def test_blocker_interaction_seq_forwarding_validation_and_idempotency(self):
        dlg = self.call('start', agent='agy', prompt='owned', cwd=self.tmp.name)['task']
        self.bridge.store.update(dlg['id'], state='running', runner_pid=123)
        self.bridge.store.native_input_open(dlg['id'], 1, dlg['launch_id'], 1)
        self.bridge.store.update(dlg['id'], native_observation=dict(
            turn=1, input_available=True, question_ids=[],
            question_blockers=[
                dict(interaction_seq=0, reason='incomplete_question', available_actions=['observe', 'dialogue', 'cancel'], blocks_completion=True),
                dict(interaction_seq=2, reason='unresolved_question', available_actions=['inspect_origin', 'reconcile_hold'], blocks_completion=True),
            ],
        ))
        self.bridge.store.event(dlg['id'], '1:2', 'question', 'Q1?', turn=1)

        rep = self.call('start', agent='codex', prompt='reply-host', cwd=self.tmp.name)['task']
        self.bridge.store.update(rep['id'], state='waiting_reply')
        self.bridge.store.event(rep['id'], '1:2', 'question', 'Q1?', turn=1)

        for action, extra in (
            ('reply', dict(task_id=rep['id'], prompt='ans', question_id='1:2', expected_turn=1, command_id='r-blk')),
            ('status', dict(task_id=dlg['id'])),
            ('intervene', dict(task_id=dlg['id'], prompt='rev', command_id='i-blk')),
            ('list', dict()),
            ('cancel', dict(task_id=dlg['id'])),
            ('close', dict(task_id=dlg['id'])),
        ):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.call(action, blocker_interaction_seq=0, **extra)

        for bad_val in (False, True, None, -1, 1.5, '0', [0]):
            with self.subTest(bad_val=bad_val), self.assertRaises(ValueError):
                self.call('dialogue', task_id=dlg['id'], prompt='bad', command_id='d-bad',
                          expected_turn=1, blocker_interaction_seq=bad_val)

        for mix in (
            dict(question_id='1:2', blocker_interaction_seq=0),
            dict(question_id='', blocker_interaction_seq=0),
            dict(question_ids=['1:2'], blocker_interaction_seq=0),
        ):
            with self.subTest(mix=mix), self.assertRaises(ValueError):
                self.call('dialogue', task_id=dlg['id'], prompt='mix', command_id='d-mix',
                          expected_turn=1, **mix)

        for bad_seq in (1, 2):
            with self.assertRaisesRegex(ValueError, 'incomplete_question'):
                self.call('dialogue', task_id=dlg['id'], prompt='miss', command_id=f'd-miss-{bad_seq}',
                          expected_turn=1, blocker_interaction_seq=bad_seq)

        res0 = self.call('dialogue', task_id=dlg['id'], prompt='review 0', command_id='d0',
                         expected_turn=1, blocker_interaction_seq=0)['task']
        self.assertEqual(res0['commands']['d0']['blocker_interaction_seq'], 0)
        with self.assertRaises(ValueError):
            self.call('dialogue', task_id=dlg['id'], command_id='w-bad', expected_turn=1,
                      target_command_id='d0', withdraw=True, blocker_interaction_seq=0)
        self.call('dialogue', task_id=dlg['id'], command_id='w0', expected_turn=1,
                  target_command_id='d0', withdraw=True)
        res1 = self.call('dialogue', task_id=dlg['id'], prompt='review 0 v2', command_id='d1',
                         expected_turn=1, blocker_interaction_seq=0)['task']
        self.assertEqual(res1['commands']['d1']['blocker_interaction_seq'], 0)

        self.bridge.store.native_dialogue_claim(dlg['id'], 1, dlg['launch_id'], 1)
        self.bridge.store.native_dialogue_state(dlg['id'], 1, dlg['launch_id'], 1, 'd1', 'send_started')
        self.bridge.store.native_dialogue_state(dlg['id'], 1, dlg['launch_id'], 1, 'd1', 'returned')
        self.bridge.store.update(dlg['id'], state='completed', questions=[], question_id='',
                                 native_input=dict(accepting=False, settling=True, turn=1, launch_id=dlg['launch_id'], epoch=1),
                                 native_observation=dict(turn=1, input_available=False, question_ids=[], question_blockers=[]))
        retried = self.call('dialogue', task_id=dlg['id'], prompt='review 0 v2', command_id='d1',
                            expected_turn=1, blocker_interaction_seq=0)['task']
        self.assertEqual(retried['state'], 'completed')
        self.assertEqual(retried['commands']['d1']['blocker_interaction_seq'], 0)


class OmpReadOnlyAndRecoveryStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'tasks.db'
        self.bridge = HostBridge(self.db, launch=lambda t: None)
        self.a = dict(session_id='omp-a', agent_id='agent-a')
        self.b = dict(session_id='omp-b', agent_id='agent-b')
        self.ka = self.bridge.bind(self.a)
        self.kb = self.bridge.bind(self.b)

    def test_recovery_status_audited_diagnostic_readonly_and_owned_flag(self):
        task = self.bridge.handle(
            self.a, self.ka, dict(action='start', agent='agy', cwd=self.tmp.name, prompt='CANARY_PROMPT')
        )['task']
        tid = task['id']
        self.assertIs(task['owned_by_bound_host'], True)
        self.bridge.store.update(
            tid, state='unknown', runner_pid=0, settlement_note='CANARY_NOTE', error='CANARY_ERR',
            questions=['1:2'], answers={'1:1': 'CANARY_ANS'},
            native_observation={
                'turn': 1, 'history_degraded': True, 'response_pending_reason': 'board_navigation_only',
                'pending_tools': [{'id': 'CANARY_TID', 'name': 'CANARY_TOOL', 'kind': 'edit', 'status': 'in_progress'}],
                'history_diagnostic': {'reason_code': 'dialogue_missing', 'call_site': 'section',
                                       'previous_len': 62, 'current_len': 25, 'extra_secret': 'CANARY_EXTRA'},
            },
        )
        before = self.db.read_bytes()
        res = self.bridge.handle(self.b, self.kb, dict(action='recovery_status', task_id=tid))
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(res['resource_recovery']['task_id'], tid)
        self.assertEqual(res['diagnostic']['agent'], 'agy')
        self.assertEqual(res['diagnostic']['pending_question_count'], 1)
        self.assertEqual(res['diagnostic']['native_diagnostic']['pending_tool_kinds'], ['edit'])
        self.assertEqual(res['diagnostic']['native_diagnostic']['history_diagnostic'], {
            'reason_code': 'dialogue_missing', 'call_site': 'section', 'previous_len': 62, 'current_len': 25,
        })
        dumped = json.dumps(res)
        for secret in ('CANARY_', 'omp-a', 'omp-b', self.ka, self.kb, self.tmp.name):
            self.assertNotIn(secret, dumped)
        for bad in (dict(action='recovery_status', task_id=''), dict(action='recovery_status', task_id=tid, group='g')):
            with self.assertRaises(ValueError):
                self.bridge.handle(self.b, self.kb, bad)

    def test_readonly_bridge_and_main_do_not_initialize_missing_db(self):
        missing = Path(self.tmp.name) / 'missing' / 'tasks.db'
        ro = HostBridge(missing, read_only=True, launch=lambda t: None)
        self.assertFalse(missing.parent.exists())
        with self.assertRaises(PermissionError):
            ro.handle(self.a, self.ka, dict(action='list'))
        self.assertEqual(ro.observe(self.a, dict(action='list')), {'tasks': []})
        with self.assertRaises(KeyError):
            ro.observe(self.a, dict(action='recovery_status', task_id='t1'))
        self.assertFalse(missing.parent.exists())
        for action in ('list', 'status', 'events', 'recovery_status'):
            args = dict(action=action) if action == 'list' else dict(action=action, task_id='t1')
            proc = subprocess.run(
                [sys.executable, '-B', str(Path(__file__).with_name('omp_bridge.py'))],
                input=json.dumps(dict(db=str(missing), host=self.a, capability=self.ka, args=args)),
                text=True, capture_output=True, timeout=5,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(missing.parent.exists())
        for args, expected_rc in ((dict(action='list'), 0), (dict(action='recovery_status', task_id='t1'), 1)):
            proc = subprocess.run(
                [sys.executable, '-B', str(Path(__file__).with_name('omp_bridge.py'))],
                input=json.dumps(dict(db=str(missing), action='observe', host=self.a, args=args)),
                text=True, capture_output=True, timeout=5,
            )
            self.assertEqual(proc.returncode, expected_rc)
            self.assertFalse(missing.parent.exists())

    def test_observe_subprocess_requires_valid_host_and_never_writes_existing_db(self):
        task = self.bridge.handle(
            self.a, self.ka, dict(action='start', agent='agy', cwd=self.tmp.name, prompt='CANARY_OBS')
        )['task']
        tid = task['id']
        before = self.db.read_bytes()
        for host, args, rc in (
            (self.b, dict(action='list'), 0),
            (self.b, dict(action='recovery_status', task_id=tid), 0),
            (self.b, dict(action='status', task_id=tid), 1),
            ({}, dict(action='list'), 1),
        ):
            proc = subprocess.run(
                [sys.executable, '-B', str(Path(__file__).with_name('omp_bridge.py'))],
                input=json.dumps(dict(db=str(self.db), action='observe', host=host, args=args)),
                text=True, capture_output=True, timeout=5,
            )
            self.assertEqual(proc.returncode, rc, proc.stdout + proc.stderr)
            self.assertEqual(self.db.read_bytes(), before)
            self.assertNotIn('CANARY_OBS', proc.stdout)


if __name__ == '__main__':
    unittest.main()
