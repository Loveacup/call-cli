"""Zero-model history race regressions via the existing owned ACP fixture."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from collab import Collaboration
from omp_bridge import HostBridge
from runner import run_task
import test_plugin
import test_swarm_thin


class HistoryRaceTests(unittest.TestCase):
    workspace = test_swarm_thin.SwarmThinTests.workspace

    def exercise(self, race):
        with patch.dict(os.environ, AGY_NATIVE_HISTORY_RACE=race):
            with self.workspace('question') as (root, c, client, task, thread):
                deadline = time.monotonic()+4
                while not c.status(client, task['id']).get('questions') and time.monotonic()<deadline:
                    time.sleep(.01)
                question = c.status(client, task['id'])['questions'][0]
                c.dialogue(client, task['id'], 'revised', expected_turn=1,
                           command_id='revision', question_id=question)
                thread.join(5)
                self.assertFalse(thread.is_alive())
                state = c.status(client, task['id'])
                self.assertEqual(state['state'], 'completed', state.get('settlement_note'))
                self.assertEqual(state['native_result']['result'], 'Revised answer')
                self.assertEqual(state['native_result']['interaction_seq'], 1)
                events = c.events(client, task['id'])
                self.assertEqual([(e['text'], e['interaction_seq']) for e in events if e['kind']=='answer'],
                                 [('Revised answer', 1), ('Late background answer', 0)])
                self.assertEqual(len([e for e in events if e['kind']=='question']), 1)
                self.assertEqual(state['questions'], [])
                self.assertTrue((root/'closed-session').exists())

    def test_tampering_never_settles_or_unloads(self):
        for race in ('rewrite', 'delete', 'live-rewrite', 'missing-boundary'):
            with self.subTest(race=race), patch.dict(os.environ, AGY_NATIVE_HISTORY_RACE=race):
                with self.workspace('question') as (root, c, client, task, thread):
                    deadline = time.monotonic()+4
                    while not c.status(client, task['id']).get('questions') and time.monotonic()<deadline:
                        time.sleep(.01)
                    q = c.status(client, task['id'])['questions'][0]
                    c.dialogue(client, task['id'], 'revised', expected_turn=1, command_id='revision', question_id=q)
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                    state = c.status(client, task['id'])
                    self.assertEqual(state['state'], 'unknown')
                    self.assertNotIn('native_result', state)
                    self.assertNotIn('native_cleanup', state)
                    self.assertFalse((root/'closed-session').exists())
                    self.assertEqual(len([e for e in c.events(client, task['id']) if e['kind']=='question']), 1)

    def test_two_dialogues_preserve_older_background_interaction(self):
        with patch.dict(os.environ, AGY_NATIVE_HISTORY_RACE='multi'):
            with self.workspace('question') as (root, c, client, task, thread):
                for seq in (1, 2):
                    deadline = time.monotonic()+4
                    while time.monotonic()<deadline:
                        state = c.status(client, task['id'])
                        if state.get('questions'): break
                        time.sleep(.01)
                    q = state['questions'][0]
                    c.dialogue(client, task['id'], 'revised', expected_turn=1,
                        command_id='revision'+str(seq), question_id=q)
                    deadline = time.monotonic()+4
                    while c.status(client, task['id'])['commands']['revision'+str(seq)]['status'] != 'returned' and time.monotonic()<deadline:
                        time.sleep(.01)
                thread.join(5)
                state = c.status(client, task['id'])
                self.assertEqual(state['state'], 'completed', state.get('settlement_note'))
                self.assertEqual(state['native_result']['interaction_seq'], 2)
                events = c.events(client, task['id'])
                self.assertEqual([(e['text'],e['interaction_seq']) for e in events if e['kind']=='question'],
                    [('Q1 Which label?',0),('Q2 Confirm revision?',1)])
                self.assertEqual([e['interaction_seq'] for e in events if e['text']=='Older interaction update'], [0])
                self.assertEqual([e['interaction_seq'] for e in events if e['text']=='Revised answer'], [1,2])
                self.assertTrue((root/'closed-session').exists())

    def test_teamwork_background_growth_keeps_original_interaction(self):
        original = test_swarm_thin.Collaboration.start
        def start(collaboration, *args, **kwargs):
            kwargs['model'] = 'oct'
            return original(collaboration, *args, **kwargs)
        with patch.object(test_swarm_thin.Collaboration, 'start', start):
            self.exercise('tail')

    def test_background_growth_before_dialogue_live_replay(self):
        self.exercise('tail')

    def test_background_growth_in_non_tail_assistant_slot(self):
        self.exercise('non-tail')

    def test_reused_baseline_non_tail_growth_completes_and_rewrites_fail_closed(self):
        for tail_mode in ('non-tail', 'non-tail-rewrite'):
            with self.subTest(tail_mode=tail_mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                c = Collaboration(root/'tasks.db', launch=lambda task: None)
                client = c.connect('omp')['client_id']
                task = c.start(client, agent='agy', cwd=directory, prompt='owned', write=True)
                c.store.update(task['id'], native_session_id='11111111-1111-4111-8111-111111111111')
                (root/'task-id').write_text(task['id'])
                executable = root/'agy'
                executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
                executable.chmod(0o700)
                with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'],
                                AGY_NATIVE_FIXTURE=str(root), AGY_NATIVE_TAIL_GROWTH=tail_mode), \
                        patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), \
                        patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    code = run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)
                result = c.status(client, task['id'])
                events = c.events(client, task['id'])
                if tail_mode == 'non-tail':
                    self.assertEqual(code, 0)
                    self.assertEqual(result['state'], 'completed', result.get('settlement_note'))
                    self.assertIn('tail result', result['native_result']['result'])
                    self.assertNotIn('old result', '\n'.join(e['text'] for e in events))
                    self.assertEqual(c.close(client, task['id'])['state'], 'closed')
                else:
                    self.assertEqual(result['state'], 'unknown')
                    self.assertIn('Native history prefix changed; reconciliation required', result['settlement_note'])
                    self.assertNotIn('native_result', result)
                    self.assertNotIn('native_cleanup', result)
                    self.assertFalse((root/'closed-session').exists())
                    diag = result.get('native_observation', {}).get('history_diagnostic')
                    self.assertIsInstance(diag, dict)
                    self.assertEqual(diag['call_site'], 'baseline')
                    self.assertEqual(diag['interaction_seq'], 0)
                    self.assertEqual(diag['turn'], 1)
                    self.assertEqual(diag['reason_code'], 'prefix_changed')
                    self.assertEqual(diag['index'], 0)
                    self.assertFalse(diag['is_tail'])
                    self.assertFalse(diag['is_prefix'])

    def test_sparse_tool_enrichment_across_loads_completes_and_conflicts_fail_closed(self):
        from agy_native import additions, ProtocolError
        with self.workspace('sparse-load-enrich') as (root, c, client, task, thread):
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                state = c.status(client, task['id'])
                if state.get('native_observation', {}).get('pending_tools'):
                    break
                time.sleep(.01)
            self.assertTrue(c.status(client, task['id']).get('native_observation', {}).get('pending_tools'))
            (root/'finish').touch()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            state = c.status(client, task['id'])
            self.assertEqual(state['state'], 'completed', state.get('settlement_note'))
            self.assertEqual((root/'result.txt').read_text(), 'initial')
            self.assertEqual([e['text'] for e in c.events(client, task['id']) if e['kind']=='answer'], ['Native final initial'])
            self.assertTrue((root/'closed-session').exists())
            self.assertEqual(c.close(client, task['id'])['state'], 'closed')

        def tool(tid, **extra):
            return dict(sessionUpdate='tool_call', toolCallId=tid, **extra)

        read_changed = additions([tool('r1', title='read_file', status='in_progress')],
                                 [tool('r1', title='read_file', kind='read', status='completed')],
                                 background_growth=True)
        self.assertEqual(len(read_changed), 1)
        self.assertEqual(read_changed[0]['kind'], 'read')

        swarm_added = additions([tool('s1', title='swarm', status='in_progress')],
                                [tool('s1', title='swarm', kind='other', status='completed', swarm={'action': 'status'})],
                                background_growth=True)
        self.assertEqual(swarm_added[0]['swarm'], {'action': 'status'})

        swarm_extended = additions([tool('s2', title='swarm', kind='other', status='in_progress', swarm={'action': 'add'})],
                                   [tool('s2', title='swarm', kind='other', status='completed',
                                         swarm={'action': 'add', 'name': 'worker-a', 'model': 'gemini-4-hydrogen-medium'})],
                                   background_growth=True)
        self.assertEqual(swarm_extended[0]['swarm']['name'], 'worker-a')

        negative_pairs = [
            ('kind_mutated',
             [tool('t1', title='read_file', kind='read', status='in_progress')],
             [tool('t1', title='read_file', kind='edit', status='completed')]),
            ('kind_deleted',
             [tool('t2', title='read_file', kind='read', status='in_progress')],
             [tool('t2', title='read_file', status='completed')]),
            ('swarm_mutated',
             [tool('t3', title='swarm', kind='other', status='in_progress', swarm={'action': 'add', 'name': 'worker-a'})],
             [tool('t3', title='swarm', kind='other', status='completed', swarm={'action': 'add', 'name': 'other'})]),
            ('swarm_deleted',
             [tool('t4', title='swarm', kind='other', status='in_progress', swarm={'action': 'status'})],
             [tool('t4', title='swarm', kind='other', status='completed')]),
            ('non_swarm_gained_swarm',
             [tool('t5', title='read_file', kind='read', status='in_progress')],
             [tool('t5', title='read_file', kind='read', status='completed', swarm={'action': 'status'})]),
        ]
        for label, prev, curr in negative_pairs:
            with self.subTest(label=label):
                with self.assertRaises(ProtocolError) as ctx:
                    additions(prev, curr, background_growth=True, call_site='section', interaction_seq=0)
                self.assertIn('Native tool history changed', str(ctx.exception))
                self.assertIsNotNone(ctx.exception.history_diagnostic)
                self.assertEqual(ctx.exception.history_diagnostic['reason_code'], 'tool_changed')

    def test_content_free_history_diagnostics_distinguish_shapes_without_canary_leaks(self):
        from agy_native import additions, snapshot, ProtocolError
        CANARY = 'SYNTHETIC_PRIVATE_HISTORY_CANARY_9f8e7d'
        CANARY_SHA = hashlib.sha256(CANARY.encode()).hexdigest()

        def text(value):
            return dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=value))

        def user(value):
            return dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=value))

        tool = dict(sessionUpdate='tool_call', toolCallId='id-'+CANARY, title='tool-'+CANARY, status='pending')

        cases = [
            ('rechunk_shrank',
             snapshot([text('Hello '+CANARY+' '), tool, text('world\n')]),
             snapshot([text('Hello '+CANARY+' world\n'), tool]),
             dict(reason_code='history_shrank', previous_len=3, current_len=2,
                  concat_agent_is_prefix=True, same_tool_id_prefix=True, same_user_prefix=True)),
            ('rechunk_same_len_order_swap',
             snapshot([text('Hello '+CANARY+'\n'), tool]),
             snapshot([tool, text('Hello '+CANARY+'\n')]),
             dict(reason_code='prefix_changed', index=0, old_type='agent_message_chunk', new_type='tool_call',
                  is_prefix=None, concat_agent_is_prefix=True, same_tool_id_prefix=True, same_user_prefix=True)),
            ('assistant_rewrite',
             [text('Old '+CANARY)],
             [text('New '+CANARY)],
             dict(reason_code='prefix_changed', index=0, old_type='agent_message_chunk', new_type='agent_message_chunk',
                  is_prefix=False, concat_agent_is_prefix=False)),
            ('tool_id_swap',
             [dict(sessionUpdate='tool_call', toolCallId='a-'+CANARY, status='pending')],
             [dict(sessionUpdate='tool_call', toolCallId='b-'+CANARY, status='pending')],
             dict(reason_code='prefix_changed', index=0, old_type='tool_call', new_type='tool_call',
                  same_tool_id=False, same_tool_id_prefix=False)),
            ('non_tail_user_growth',
             [user('u-'+CANARY), text('a')],
             [user('u-'+CANARY+'-more'), text('a')],
             dict(reason_code='prefix_changed', index=0, old_type='user_message_chunk', new_type='user_message_chunk',
                  is_tail=False, is_prefix=True, same_user_prefix=False)),
        ]
        for label, prev, curr, expected in cases:
            with self.subTest(label=label):
                with self.assertRaises(ProtocolError) as ctx:
                    additions(prev, curr, background_growth=True, call_site='section', interaction_seq=0)
                diag = ctx.exception.history_diagnostic
                self.assertIsInstance(diag, dict)
                for key, val in expected.items():
                    self.assertEqual(diag[key], val)
                for blob in (str(ctx.exception), json.dumps(diag)):
                    self.assertNotIn(CANARY, blob)
                    self.assertNotIn(CANARY_SHA, blob)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'agy'
            executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            stdout_buf = io.StringIO()
            with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'],
                            AGY_NATIVE_FIXTURE=directory, AGY_SWARM_THIN='canary-section-prefix-change',
                            CALLCLI_TEST_CANARY=CANARY), \
                    patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), \
                    patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                    contextlib.redirect_stdout(stdout_buf):
                c = Collaboration(root/'tasks.db', launch=lambda task: None)
                client = c.connect('hermes')['client_id']
                task = c.start(client, agent='agy', model='ocs', cwd=directory, prompt='Private '+CANARY, write=True)
                import threading
                thread = threading.Thread(target=run_task, args=(c.store.path, task['id']),
                                          kwargs=dict(turn=1, launch_id=task['launch_id'], epoch=1))
                thread.start()
                try:
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline:
                        if c.status(client, task['id']).get('native_observation', {}).get('pending_tools'):
                            break
                        time.sleep(.01)
                    self.assertTrue(c.status(client, task['id']).get('native_observation', {}).get('pending_tools'))
                    (root/'tamper').touch()
                    thread.join(5)
                finally:
                    if thread.is_alive():
                        c.cancel(client, task['id'])
                        thread.join(5)
                    self.assertFalse(thread.is_alive())
            state = c.status(client, task['id'])
            self.assertEqual(state['state'], 'unknown')
            diag = state.get('native_observation', {}).get('history_diagnostic')
            self.assertIsInstance(diag, dict)
            self.assertEqual(diag['call_site'], 'section')
            self.assertEqual(diag['interaction_seq'], 0)
            self.assertEqual(diag['turn'], 1)
            self.assertEqual(diag['reason_code'], 'prefix_changed')
            self.assertEqual(diag['index'], 1)
            self.assertFalse(diag['is_tail'])
            self.assertEqual(diag['old_type'], 'agent_message_chunk')
            self.assertEqual(diag['new_type'], 'agent_message_chunk')
            self.assertFalse(diag['is_prefix'])
            self.assertTrue(state['settlement_note'].startswith('Native history prefix changed; reconciliation required ['))
            self.assertIn('site=section', state['settlement_note'])
            self.assertIn('seq=0', state['settlement_note'])
            self.assertIn('reason=prefix_changed', state['settlement_note'])
            raw_task = c.store.get(task['id'])
            hermes_pub = test_plugin.load_plugin().Coordinator.public(raw_task)
            omp_pub = HostBridge.public(raw_task)
            for proj in (hermes_pub, omp_pub):
                self.assertEqual(proj['native_observation']['history_diagnostic'], diag)
                self.assertEqual(proj['settlement_note'], state['settlement_note'])
            for blob in (json.dumps(state), json.dumps(c.events(client, task['id'])),
                         json.dumps(hermes_pub), json.dumps(omp_pub), stdout_buf.getvalue()):
                self.assertNotIn(CANARY, blob)
                self.assertNotIn(CANARY_SHA, blob)
