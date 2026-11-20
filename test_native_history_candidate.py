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

    def test_non_safety_history_races_degrade_and_complete_without_prompt_replay(self):
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
                    self.assertEqual(state['state'], 'completed', state.get('settlement_note'))
                    self.assertTrue(state['native_observation'].get('history_degraded'))
                    self.assertIsInstance(state['native_observation'].get('history_diagnostic'), dict)
                    self.assertEqual(state['native_result']['interaction_seq'], 1)
                    self.assertIn(state['native_result']['result'], {'Revised answer', 'Tampered answer', 'Late background answer\nRevised answer'})
                    self.assertEqual(state['questions'], [])
                    self.assertEqual(len([e for e in c.events(client, task['id']) if e['kind']=='question']), 1)
                    self.assertTrue((root/'closed-session').exists())
                    requests = [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
                    self.assertEqual(sum(r.get('method') == 'session/prompt' for r in requests), 2)

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
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        if c.status(client, task['id']).get('native_observation', {}).get('pending_tools'):
                            break
                        time.sleep(.01)
                    self.assertTrue(c.status(client, task['id']).get('native_observation', {}).get('pending_tools'))
                    (root/'tamper').touch()
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        if c.status(client, task['id']).get('native_observation', {}).get('history_degraded'):
                            break
                        time.sleep(.01)
                    self.assertTrue(c.status(client, task['id']).get('native_observation', {}).get('history_degraded'))
                finally:
                    if thread.is_alive():
                        c.cancel(client, task['id'])
                        thread.join(5)
                    self.assertFalse(thread.is_alive())
            state = c.status(client, task['id'])
            self.assertEqual(state['state'], 'cancelled')
            self.assertTrue(state['native_observation'].get('history_degraded'))
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
            raw_task = c.store.get(task['id'])
            hermes_pub = test_plugin.load_plugin().Coordinator.public(raw_task)
            omp_pub = HostBridge.public(raw_task)
            for proj in (hermes_pub, omp_pub):
                self.assertEqual(proj['native_observation']['history_diagnostic'], diag)
                self.assertTrue(proj['native_observation']['history_degraded'])
            for blob in (json.dumps(diag), json.dumps(state), json.dumps(hermes_pub), json.dumps(omp_pub)):
                self.assertNotIn(CANARY, blob)
                self.assertNotIn(CANARY_SHA, blob)
            for ev in c.events(client, task['id']):
                if ev['kind'] != 'answer':
                    self.assertNotIn(CANARY, json.dumps(ev))
                    self.assertNotIn(CANARY_SHA, json.dumps(ev))


_DRAIN_SHIM_SCRIPT = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

scenario = json.loads(Path(os.environ['AGY_DRAIN_SCENARIO']).read_text())
mode = scenario['mode']
SID = '33333333-3333-4333-8333-333333333333'
model = 'gemini-pro-orchestrator'
prompted = False
closed = False
loads = 0
saved = []

def send(v):
    sys.stdout.write(json.dumps(dict(jsonrpc='2.0', **v)) + '\n')
    sys.stdout.flush()

def emit(u):
    send(dict(method='session/update', params=dict(sessionId=SID, update=u)))

def meta():
    standalone = ('gemini-pro-orchestrator', 'gemini-pro-orchestrator-argon')
    return dict(
        sessionId=SID,
        models=dict(currentModelId=model, availableModels=[dict(modelId=x) for x in standalone]),
        configOptions=[dict(id='model', currentValue=model, options=[dict(value=x, name=x) for x in standalone])],
    )

def build_50(prompt_text):
    out = [dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=prompt_text))]
    for i in range(1, 25):
        out.append(dict(sessionUpdate='tool_call', toolCallId=f't{i}', name='read_file', kind='read', status='completed'))
        out.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=f'CALLCLI_STAGE step {i}\n')))
    out.append(dict(sessionUpdate='tool_call', toolCallId='t25', name='read_board', kind='read', status='in_progress'))
    return out

for line in sys.stdin:
    msg = json.loads(line)
    m = msg.get('method')
    if m == 'initialize':
        send(dict(id=msg['id'], result=dict(protocolVersion=1, agentCapabilities=dict(loadSession=True, sessionCapabilities=dict(close={})))))
    elif m == 'session/new':
        send(dict(id=msg['id'], result=meta()))
        emit(dict(sessionUpdate='available_commands_update', availableCommands=[dict(name='swarm')]))
    elif m == 'session/set_config_option':
        if closed:
            send(dict(id=msg['id'], error=dict(code=-32602, message='Session not found')))
        else:
            model = msg['params']['value']
            send(dict(id=msg['id'], result=meta()))
    elif m == 'session/prompt':
        body = msg['params']['prompt'][0]['text']
        if not prompted:
            prompted = True
            if mode == 'ocs_load2':
                saved = [
                    dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)),
                    dict(sessionUpdate='tool_call', toolCallId='add1', name='swarm', kind='other', status='completed', rawInput=dict(action='add', name='worker-1')),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Run /swarm board\n')),
                ]
            elif mode == 'oas_load1':
                saved = build_50(body)
            elif mode in {'transient_once', 'persistent_inversion'}:
                saved = [
                    dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)),
                    dict(sessionUpdate='tool_call', toolCallId='t1', name='read_file', kind='read', status='in_progress'),
                ]
            elif mode in {'question_preserved', 'question_omitted', 'fatal_omitted'}:
                saved = [
                    dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Run /swarm board\n')),
                ]
            elif mode in {'tool_missing', 'tool_regressed'}:
                saved = [
                    dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)),
                    dict(sessionUpdate='tool_call', toolCallId='t0', name='read_file', kind='read', status='in_progress'),
                ]
            for e in saved:
                emit(e)
            send(dict(id=msg['id'], result=dict(stopReason='end_turn')))
            if mode == 'oas_load1':
                emit(dict(sessionUpdate='tool_call_update', toolCallId='t25', name='read_board', kind='read', status='in_progress'))
            elif mode == 'tool_missing':
                emit(dict(sessionUpdate='tool_call', toolCallId='vanished', name='read_file', kind='read', status='failed'))
            elif mode == 'tool_regressed':
                emit(dict(sessionUpdate='tool_call', toolCallId='vanished', name='read_file', kind='read', status='failed'))
        else:
            saved.append(dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)))
            ans = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Approved v2 deliverable\n'))
            saved.append(ans)
            emit(ans)
            send(dict(id=msg['id'], result=dict(stopReason='end_turn')))
    elif m == 'session/load':
        if closed:
            send(dict(id=msg['id'], error=dict(code=-32602, message='Session not found')))
            continue
        if not prompted:
            send(dict(id=msg['id'], result=meta()))
            continue
        loads += 1
        if mode == 'ocs_load2' and loads == 2:
            saved.extend([
                dict(sessionUpdate='tool_call', toolCallId='board-read', name='read_board', kind='read', status='completed'),
                dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Final OCS deliverable\n')),
            ])
        elif mode == 'oas_load1' and loads >= 2 and len(saved) == 50:
            saved[49]['status'] = 'completed'
            saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Final OAS review deliverable\n')))
        elif mode == 'transient_once':
            if loads == 1:
                emit(dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file', kind='read', status='in_progress'))
            else:
                saved[1]['status'] = 'completed'
                if len(saved) == 2:
                    saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Final after retry\n')))
        elif mode == 'persistent_inversion':
            emit(dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file', kind='read',
                      status='completed' if loads >= 3 else 'in_progress'))
            if loads >= 3:
                saved[1]['status'] = 'completed'
                if len(saved) == 2:
                    saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Final after persistent inversion\n')))
        elif mode == 'question_preserved' and loads == 2:
            saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q1 Approve v2?\n')))
        elif mode == 'question_omitted' and loads == 2:
            saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Final answer without question\n')))
        elif mode == 'fatal_omitted' and loads == 2:
            saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Final answer\n')))
        elif mode == 'tool_regressed' and loads >= 1 and len(saved) == 2:
            saved.append(dict(sessionUpdate='tool_call', toolCallId='vanished', name='read_file', kind='read', status='completed'))
        for e in saved:
            emit(e)
        send(dict(id=msg['id'], result=meta()))
        if mode == 'ocs_load2' and loads == 1:
            emit(dict(sessionUpdate='tool_call_update', toolCallId='board-read', name='read_board', kind='read', status='completed'))
        elif mode == 'question_preserved' and loads == 1:
            emit(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q1 Approve v2?\n')))
        elif mode == 'question_omitted' and loads == 1:
            emit(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q1 Secretly dropped?\n')))
        elif mode == 'fatal_omitted' and loads == 1:
            emit(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='\u26a0 Agent execution terminated due to error.\n')))
    elif m == 'session/close':
        closed = True
        send(dict(id=msg['id'], result={}))
    else:
        send(dict(id=msg.get('id'), error=dict(code=-32601, message='Unsupported')))
"""


class NativeBetweenRpcDrainTests(unittest.TestCase):
    def _run_shim(self, mode, model='ocs', prompt_len=None, *, answer_question=False):
        import agy
        import agy_native
        import backends
        import threading
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = root / 'agy'
            shim.write_text(_DRAIN_SHIM_SCRIPT)
            shim.chmod(0o700)
            scenario = root / 'scenario.json'
            scenario.write_text(json.dumps(dict(mode=mode)))
            if prompt_len is None:
                prompt = 'Coordinate OCS swarm deliverables.'
            else:
                fields = backends.prepare_start('agy', model)
                base_task = dict(id='size-probe', write=False, prompt='', **fields)
                overhead = len(agy_native.brief(base_task, agy.prompt_guidance(base_task)))
                prompt = 'P' * (prompt_len - overhead)
            with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ.get('PATH', ''), AGY_DRAIN_SCENARIO=str(scenario)), \
                    patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                    patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                client = c.connect('hermes')['client_id']
                task = c.start(client, agent='agy', model=model, cwd=directory, prompt=prompt, write=False)
                if not answer_question:
                    code = run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)
                else:
                    outcome = []
                    thread = threading.Thread(target=lambda: outcome.append(
                        run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)))
                    thread.start()
                    try:
                        deadline = time.monotonic() + 5
                        while not c.status(client, task['id']).get('questions') and time.monotonic() < deadline:
                            time.sleep(.01)
                        questions = c.status(client, task['id']).get('questions', [])
                        self.assertEqual(len(questions), 1)
                        c.dialogue(client, task['id'], 'yes proceed', expected_turn=1, command_id='answer-q1', question_id=questions[0])
                        thread.join(5)
                        self.assertFalse(thread.is_alive())
                        code = outcome[0]
                    finally:
                        if thread.is_alive():
                            c.cancel(client, task['id'])
                            thread.join(5)
                status = c.status(client, task['id'])
                closed = c.close(client, task['id'])
                events = c.events(client, task['id'])
                return code, status, closed, events

    def test_queued_live_updates_between_loads_reconcile_and_complete(self):
        code, status, closed, _ = self._run_shim('ocs_load2', model='ocs')
        self.assertEqual(code, 0, status.get('settlement_note'))
        self.assertEqual(status['state'], 'completed', status.get('settlement_note'))
        self.assertEqual(status['native_result']['result'], 'Final OCS deliverable')
        self.assertEqual(closed['state'], 'closed')

        code, status, closed, _ = self._run_shim('oas_load1', model='oas', prompt_len=2149)
        self.assertEqual(code, 0, status.get('settlement_note'))
        self.assertEqual(status['state'], 'completed', status.get('settlement_note'))
        self.assertEqual(status['native_result']['result'], 'Final OAS review deliverable')
        self.assertEqual(closed['state'], 'closed')

    def test_transient_inflight_inverted_tool_replay_retries_once_and_persistent_inversion_degrades(self):
        code, status, closed, _ = self._run_shim('transient_once', model='ocs')
        self.assertEqual(code, 0, status.get('settlement_note'))
        self.assertEqual(status['state'], 'completed', status.get('settlement_note'))
        self.assertEqual(status['native_result']['result'], 'Final after retry')
        self.assertEqual(closed['state'], 'closed')

        code, status, closed, _ = self._run_shim('persistent_inversion', model='ocs')
        self.assertEqual(code, 0, status.get('settlement_note'))
        self.assertEqual(status['state'], 'completed', status.get('settlement_note'))
        self.assertEqual(status['native_result']['result'], 'Final after persistent inversion')
        self.assertIs(status.get('native_observation', {}).get('history_degraded'), True)
        diag = status.get('native_observation', {}).get('history_diagnostic', {})
        self.assertEqual(diag.get('reason_code'), 'inverted_tool_replay')
        self.assertIs(diag.get('load_retried'), True)
        self.assertEqual(closed['state'], 'closed')

    def test_drained_safety_signals_never_swallowed(self):
        code, status, closed, events = self._run_shim('question_preserved', model='ocs', answer_question=True)
        self.assertEqual(code, 0, status.get('settlement_note'))
        self.assertEqual(status['state'], 'completed', status.get('settlement_note'))
        self.assertEqual(closed['state'], 'closed')
        self.assertEqual([e['text'] for e in events if e['kind'] == 'question'], ['Q1 Approve v2?'])
        self.assertEqual(status['native_result']['result'], 'Approved v2 deliverable')

        code, status, closed, events = self._run_shim('question_omitted', model='ocs', answer_question=True)
        self.assertEqual(code, 0, status.get('settlement_note'))
        self.assertEqual(status['state'], 'completed', status.get('settlement_note'))
        self.assertEqual([e['text'] for e in events if e['kind'] == 'question'], ['Q1 Secretly dropped?'])
        self.assertEqual(status['native_result']['result'], 'Approved v2 deliverable')
        self.assertIs(status.get('native_observation', {}).get('history_degraded'), True)
        self.assertEqual(closed['state'], 'closed')

        for mode in ('fatal_omitted', 'tool_missing', 'tool_regressed'):
            with self.subTest(mode=mode):
                code, status, closed, _ = self._run_shim(mode, model='ocs')
                self.assertEqual(code, 1)
                self.assertEqual(status['state'], 'unknown')
                self.assertNotIn('native_result', status)
                self.assertNotIn('native_cleanup', status)
                self.assertEqual(closed['state'], 'unknown')

        import selectors
        from types import SimpleNamespace
        from agy_native import NativeOwner, ProtocolError

        def make_pipe_owner(*, task=None):
            r_out, w_out = os.pipe()
            r_in, w_in = os.pipe()
            owner = object.__new__(NativeOwner)
            owner.task = task or dict(agy_approval='approve-all')
            owner.session_id = 'owned'
            owner.serial = 0
            owner.buffer = b''
            owner.permission_decisions = []
            owner.review = None
            owner.review_blocked = False
            owner.available_commands = set()
            owner.permission_handler = None
            owner.workflow_identity = {}
            owner.approval_wait_seconds = 0.0
            owner.pending_drained = []
            owner.drain_workflow_calls = set()
            owner.drained_public_text = ''
            owner.proc = SimpleNamespace(
                stdout=os.fdopen(r_out, 'rb', buffering=0),
                stdin=os.fdopen(w_in, 'wb', buffering=0),
            )
            owner.poller = selectors.DefaultSelector()
            owner.poller.register(owner.proc.stdout, selectors.EVENT_READ)
            reader = os.fdopen(r_in, 'rb', buffering=0)
            writer = os.fdopen(w_out, 'wb', buffering=0)
            def cleanup():
                owner.poller.close()
                for h in (owner.proc.stdout, owner.proc.stdin, reader, writer):
                    try:
                        h.close()
                    except OSError:
                        pass
            return owner, reader, writer, cleanup

        # Owned permission callback during drain
        owner, reader, writer, cleanup = make_pipe_owner()
        try:
            writer.write((json.dumps(dict(jsonrpc='2.0', id=11, method='session/request_permission', params=dict(
                sessionId='owned', toolCall=dict(toolCallId='t1', kind='edit'),
                options=[dict(optionId='opt-allow', kind='allow_once')],
            ))) + '\n').encode())
            writer.flush()
            self.assertEqual(owner.drain_pending(), [])
            reply = json.loads(reader.readline().decode())
            self.assertEqual(reply['result']['outcome'], dict(outcome='selected', optionId='opt-allow'))
            self.assertEqual(len(owner.permission_decisions), 1)
            self.assertEqual(owner.permission_decisions[0]['reason'], 'selected')
        finally:
            cleanup()

        # Foreign permission callback during drain
        owner, reader, writer, cleanup = make_pipe_owner()
        try:
            writer.write((json.dumps(dict(jsonrpc='2.0', id=12, method='session/request_permission', params=dict(
                sessionId='foreign-child', toolCall=dict(toolCallId='t2', kind='read'),
                options=[dict(optionId='opt-allow', kind='allow_once')],
            ))) + '\n').encode())
            writer.flush()
            with self.assertRaisesRegex(ProtocolError, 'Permission session mismatch'):
                owner.drain_pending()
            reply = json.loads(reader.readline().decode())
            self.assertEqual(reply['result']['outcome'], dict(outcome='cancelled'))
        finally:
            cleanup()

        # Foreign session/update, unexpected RPC response, EOF disconnect, dynamic unload & ScriptPath guard
        cases = [
            ('foreign_update', None, False, dict(method='session/update', params=dict(sessionId='other', update=dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='x')))), 'Native update session mismatch'),
            ('unexpected_reply', None, False, dict(id=99, result={}), 'Unexpected native ACP response'),
            ('disconnect_eof', None, True, None, 'Native ACP disconnected'),
            ('dynamic_unload_activity', dict(agy_mode='dynamic'), False, dict(method='session/update', params=dict(sessionId='owned', update=dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='x')))), 'Dynamic activity during unload'),
            ('dynamic_script_path', dict(agy_mode='dynamic'), False, dict(method='session/update', params=dict(sessionId='owned', update=dict(sessionUpdate='tool_call', toolCallId='wf1', name='run_workflow', rawInput=dict(ScriptPath='/tmp/x.py')))), 'Dynamic ScriptPath workflow unsupported'),
        ]
        for label, task_cfg, close_writer, frame, regex in cases:
            with self.subTest(label=label):
                owner, _, writer, cleanup = make_pipe_owner(task=task_cfg)
                try:
                    if label == 'dynamic_unload_activity':
                        owner.close_attempted = True
                    if frame is not None:
                        writer.write((json.dumps(dict(jsonrpc='2.0', **frame)) + '\n').encode())
                        writer.flush()
                    if close_writer:
                        writer.close()
                    with self.assertRaisesRegex(ProtocolError, regex):
                        owner.drain_pending()
                finally:
                    cleanup()


_REWORK_SHIM_SCRIPT = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

scenario = json.loads(Path(os.environ['AGY_R1_REWORK_SCENARIO']).read_text())
mode = scenario['mode']
rpc_log = Path(scenario['rpc_log'])
SID = '55555555-5555-4555-8555-555555555555'
model = 'gemini-pro-orchestrator'
prompted = False
prompt_count = 0
closed = False
loads = 0
post_dlg_loads = 0
saved = []


def send(v):
    sys.stdout.write(json.dumps(dict(jsonrpc='2.0', **v)) + '\n')
    sys.stdout.flush()


def emit(u):
    send(dict(method='session/update', params=dict(sessionId=SID, update=u)))


def meta():
    standalone = ('gemini-pro-orchestrator', 'gemini-pro-orchestrator-argon')
    return dict(
        sessionId=SID,
        models=dict(currentModelId=model, availableModels=[dict(modelId=x) for x in standalone]),
        configOptions=[dict(id='model', currentValue=model, options=[dict(value=x, name=x) for x in standalone])],
    )


for line in sys.stdin:
    msg = json.loads(line)
    m = msg.get('method')
    with rpc_log.open('a') as f:
        f.write(json.dumps(dict(method=m)) + '\n')
    if m == 'initialize':
        send(dict(id=msg['id'], result=dict(
            protocolVersion=1,
            agentCapabilities=dict(loadSession=True, sessionCapabilities=dict(close={})),
        )))
    elif m == 'session/new':
        send(dict(id=msg['id'], result=meta()))
        emit(dict(sessionUpdate='available_commands_update', availableCommands=[dict(name='swarm')]))
    elif m == 'session/set_config_option':
        if closed:
            send(dict(id=msg['id'], error=dict(code=-32602, message='Session not found')))
        else:
            model = msg['params']['value']
            send(dict(id=msg['id'], result=meta()))
    elif m == 'session/prompt':
        body = msg['params']['prompt'][0]['text']
        prompt_count += 1
        if not prompted:
            prompted = True
            if mode in {'b2b_sparse_swarm_in_drain', 'b2b_sparse_swarm_in_retry'}:
                saved = [
                    dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)),
                    dict(sessionUpdate='tool_call', toolCallId='add1', name='swarm', kind='other',
                         status='completed', rawInput=dict(action='add', name='worker-1')),
                    dict(sessionUpdate='tool_call', toolCallId='t1', name='read_file', kind='read', status='in_progress'),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Run /swarm board\n')),
                ]
            elif mode == 'normal_sparse_swarm_in_drain':
                saved = [
                    dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)),
                    dict(sessionUpdate='tool_call', toolCallId='add1', name='swarm', kind='other',
                         status='in_progress', rawInput=dict(action='add', name='worker-1')),
                    dict(sessionUpdate='tool_call', toolCallId='t1', name='read_file', kind='read', status='in_progress'),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Run /swarm board\n')),
                ]
            elif mode == 'm1_dialogue_inversion':
                saved = [
                    dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)),
                    dict(sessionUpdate='agent_message_chunk',
                         content=dict(type='text', text='CALLCLI_QUESTION Q1 Confirm step?\n')),
                ]
            elif mode == 'non_tail_slot_growth_across_retry':
                saved = [
                    dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)),
                    dict(sessionUpdate='tool_call', toolCallId='t1', name='read_file', kind='read', status='in_progress'),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Run /swarm board\n')),
                ]
            else:
                saved = [
                    dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)),
                    dict(sessionUpdate='tool_call', toolCallId='t1', name='read_file', kind='read', status='in_progress'),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Run /swarm board\n')),
                ]
            for e in saved:
                emit(e)
            send(dict(id=msg['id'], result=dict(stopReason='end_turn')))
        else:
            saved.append(dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)))
            if mode == 'm1_dialogue_inversion':
                t_call = dict(sessionUpdate='tool_call', toolCallId='t2', name='read_file',
                              kind='read', status='in_progress')
                saved.append(t_call)
                emit(t_call)
            elif mode == 'non_tail_slot_growth_across_retry' and prompt_count == 2:
                ack = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Ack dialogue\n'))
                saved.append(ack)
                emit(ack)
                send(dict(id=msg['id'], result=dict(stopReason='end_turn')))
                emit(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=' question')))
                continue
            elif mode == 'non_tail_slot_growth_across_retry' and prompt_count == 3:
                ans = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Resolved after Q1\n'))
                saved.append(ans)
                emit(ans)
            else:
                ans = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Done\n'))
                saved.append(ans)
                emit(ans)
            send(dict(id=msg['id'], result=dict(stopReason='end_turn')))
    elif m == 'session/load':
        if closed:
            send(dict(id=msg['id'], error=dict(code=-32602, message='Session not found')))
            continue
        if not prompted:
            send(dict(id=msg['id'], result=meta()))
            continue
        loads += 1
        post_updates = []
        if mode == 'b1a_retry_question_omitted':
            if loads == 1:
                emit(dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                          kind='read', status='in_progress'))
                emit(dict(sessionUpdate='agent_message_chunk',
                          content=dict(type='text', text='CALLCLI_QUESTION Q1 Dropped?\n')))
            else:
                saved[1]['status'] = 'completed'
                if len(saved) == 3:
                    saved.append(dict(sessionUpdate='agent_message_chunk',
                                      content=dict(type='text', text='Final answer\n')))
        elif mode == 'b1b_retry_missing_failed_tool':
            if loads == 1:
                emit(dict(sessionUpdate='tool_call_update', toolCallId='vanished', name='read_file',
                          kind='read', status='failed'))
            else:
                saved[1]['status'] = 'completed'
                if len(saved) == 3:
                    saved.append(dict(sessionUpdate='agent_message_chunk',
                                      content=dict(type='text', text='Final answer\n')))
        elif mode == 'b1c_retry_tool_status_regressed':
            if loads == 1:
                emit(dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                          kind='read', status='failed'))
            else:
                saved[1]['status'] = 'completed'
                if len(saved) == 3:
                    saved.append(dict(sessionUpdate='agent_message_chunk',
                                      content=dict(type='text', text='Final answer\n')))
        elif mode == 'b1d_retry_tool_kind_conflict':
            if loads == 1:
                emit(dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                          kind='edit', status='in_progress'))
            else:
                saved[1]['status'] = 'completed'
                if len(saved) == 3:
                    saved.append(dict(sessionUpdate='agent_message_chunk',
                                      content=dict(type='text', text='Final answer\n')))
        elif mode in {'b2a_within_drain_completed', 'b2a_within_drain_failed'}:
            if loads >= 2 and len(saved) == 3:
                saved[1]['status'] = 'completed' if mode == 'b2a_within_drain_completed' else 'failed'
                saved.append(dict(sessionUpdate='agent_message_chunk',
                                  content=dict(type='text', text='Final after within-drain\n')))
            if loads == 1:
                post_updates = [
                    dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                         kind='read', status='failed'),
                    dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                         kind='read', status='in_progress'),
                ]
        elif mode == 'b2b_sparse_swarm_in_drain':
            if loads >= 2 and len(saved) == 4:
                saved[2]['status'] = 'completed'
                saved.append(dict(sessionUpdate='agent_message_chunk',
                                  content=dict(type='text', text='Final after swarm drain\n')))
            if loads == 1:
                post_updates = [
                    dict(sessionUpdate='tool_call_update', toolCallId='add1',
                         status='completed', rawInput=dict(action='add', name='tampered-worker')),
                ]
        elif mode == 'b2b_sparse_swarm_in_retry':
            if loads == 1:
                emit(dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                          kind='read', status='in_progress'))
                emit(dict(sessionUpdate='tool_call_update', toolCallId='add1',
                          status='completed', rawInput=dict(action='add', name='tampered-worker')))
            elif len(saved) == 4:
                saved[2]['status'] = 'completed'
                saved.append(dict(sessionUpdate='agent_message_chunk',
                                  content=dict(type='text', text='Final after swarm retry\n')))
        elif mode == 'm1_initial_inversion':
            emit(dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                      kind='read', status='completed' if loads >= 3 else 'in_progress'))
            if loads >= 3:
                saved[1]['status'] = 'completed'
                if len(saved) == 3:
                    saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Inverted complete\n')))
        elif mode == 'm1_dialogue_inversion':
            if any(e.get('sessionUpdate') == 'user_message_chunk'
                   and 'CALLCLI_DIALOGUE ' in e.get('content', {}).get('text', '') for e in saved):
                post_dlg_loads += 1
                emit(dict(sessionUpdate='tool_call_update', toolCallId='t2', name='read_file',
                          kind='read', status='completed' if post_dlg_loads >= 3 else 'in_progress'))
                if post_dlg_loads >= 3:
                    for e in saved:
                        if e.get('toolCallId') == 't2':
                            e['status'] = 'completed'
                    if not any('Dialogue inverted complete' in e.get('content', {}).get('text', '')
                               for e in saved if e.get('sessionUpdate') == 'agent_message_chunk'):
                        saved.append(dict(sessionUpdate='agent_message_chunk',
                                          content=dict(type='text', text='Dialogue inverted complete\n')))
        elif mode in {'masked_fatal_dropped', 'masked_fatal_preserved'}:
            if loads == 1:
                emit(dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                          kind='read', status='in_progress'))
                emit(dict(sessionUpdate='agent_message_chunk',
                          content=dict(type='text', text='\u26a0 Agent execution terminated due to error.\n')))
            else:
                saved[1]['status'] = 'completed'
                if len(saved) == 3:
                    tail_text = (
                        'Final answer\n' if mode == 'masked_fatal_dropped'
                        else '\u26a0 Agent execution terminated due to error.\n'
                    )
                    saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=tail_text)))
        elif mode == 'inflight_completed_before_stale_replay':
            if loads == 1:
                emit(dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                          kind='read', status='completed'))
            elif loads == 2 and len(saved) == 3:
                saved[1]['status'] = 'completed'
                saved.append(dict(sessionUpdate='agent_message_chunk',
                                  content=dict(type='text', text='Final answer\n')))
        elif mode in {'inflight_question_preserved_stale_replay', 'inflight_question_preserved_persisted_replay'}:
            if loads == 1:
                emit(dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                          kind='read', status='in_progress'))
                emit(dict(sessionUpdate='agent_message_chunk',
                          content=dict(type='text', text='CALLCLI_QUESTION Q1 Real question?\n')))
                if mode == 'inflight_question_preserved_persisted_replay':
                    saved.append(dict(sessionUpdate='agent_message_chunk',
                                      content=dict(type='text', text='CALLCLI_QUESTION Q1 Real question?\n')))
            elif loads == 2:
                saved[1]['status'] = 'completed'
                if len(saved) == 3:
                    saved.append(dict(sessionUpdate='agent_message_chunk',
                                      content=dict(type='text', text='CALLCLI_QUESTION Q1 Real question?\n')))
        elif mode == 'normal_sparse_swarm_in_drain':
            if loads >= 2 and len(saved) == 4:
                saved[1]['status'] = 'completed'
                saved[2]['status'] = 'completed'
                saved.append(dict(sessionUpdate='agent_message_chunk',
                                  content=dict(type='text', text='Swarm normal complete\n')))
            if loads == 1:
                post_updates = [
                    dict(sessionUpdate='tool_call_update', toolCallId='add1', status='completed'),
                ]
        elif mode in {'multi_slice_d1_retry_d2_valid', 'multi_slice_d1_retry_d2_dropped'}:
            if loads == 1:
                post_updates = [
                    dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                         kind='read', status='in_progress'),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Step D1\n')),
                ]
            elif loads == 2:
                emit(dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                          kind='read', status='in_progress'))
                emit(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Step Pre\n')))
                saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Step D1\n')))
                post_updates = [
                    dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                         kind='read', status='completed'),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Step D2\n')),
                ]
            elif loads == 3:
                saved[1]['status'] = 'completed'
                saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Step Pre\n')))
                if mode == 'multi_slice_d1_retry_d2_valid':
                    saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Step D2\n')))
                else:
                    saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Wrong tail\n')))
        elif mode in {'interleaved_post_saved_valid', 'interleaved_post_saved_dropped'}:
            if loads == 1:
                emit(dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                          kind='read', status='in_progress'))
                emit(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Step Pre\n')))
                for e in saved:
                    emit(e)
                emit(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Step Post\n')))
                send(dict(id=msg['id'], result=meta()))
                emit(dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                          kind='read', status='completed'))
                emit(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Final after post\n')))
                continue
            elif loads == 2:
                saved[1]['status'] = 'completed'
                saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Step Pre\n')))
                if mode == 'interleaved_post_saved_valid':
                    saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Step Post\n')))
                saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Final after post\n')))
        elif mode == 'non_tail_slot_growth_across_retry':
            dlg_seen = any(
                e.get('sessionUpdate') == 'user_message_chunk'
                and 'CALLCLI_DIALOGUE ' in e.get('content', {}).get('text', '')
                for e in saved
            )
            if not dlg_seen:
                if loads == 1:
                    saved.append(dict(sessionUpdate='tool_call', toolCallId='t2', name='read_file',
                                      kind='read', status='in_progress'))
                    saved.append(dict(sessionUpdate='agent_message_chunk',
                                      content=dict(type='text', text='CALLCLI_QUESTION Q1 Partial')))
            else:
                post_dlg_loads += 1
                if post_dlg_loads == 1:
                    emit(dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file',
                              kind='read', status='in_progress'))
                    emit(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='?')))
                    saved[4]['content']['text'] += ' question'
                    post_updates = [dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='\n'))]
                elif post_dlg_loads == 2:
                    saved[1]['status'] = 'completed'
                    saved[3]['status'] = 'completed'
                    saved[4]['content']['text'] += '?\n'

        for e in saved:
            emit(e)
        send(dict(id=msg['id'], result=meta()))
        for e in post_updates:
            emit(e)
    elif m == 'session/close':
        closed = True
        send(dict(id=msg['id'], result={}))
    elif m == 'session/cancel':
        pass
    else:
        send(dict(id=msg.get('id'), error=dict(code=-32601, message='Unsupported')))
"""


class Round1ReworkTests(unittest.TestCase):
    def _run_rework_shim(self, mode, *, answers=None, pre_question_dialogue=None):
        import threading
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = root / 'agy'
            shim.write_text(_REWORK_SHIM_SCRIPT)
            shim.chmod(0o700)
            rpc_log = root / 'rpcs.jsonl'
            scenario = root / 'scenario.json'
            scenario.write_text(json.dumps(dict(mode=mode, rpc_log=str(rpc_log))))
            with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ.get('PATH', ''),
                            AGY_R1_REWORK_SCENARIO=str(scenario)), \
                    patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                    patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                client = c.connect('hermes')['client_id']
                task = c.start(client, agent='agy', model='ocs', cwd=directory,
                               prompt='Verify round 1 rework invariants', write=False)
                if answers is None and pre_question_dialogue is None:
                    code = run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)
                else:
                    outcome = []
                    thread = threading.Thread(
                        target=lambda: outcome.append(
                            run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)
                        )
                    )
                    thread.start()
                    try:
                        if pre_question_dialogue is not None:
                            deadline = time.monotonic() + 5
                            while (not c.status(client, task['id']).get('native_observation', {}).get('question_blockers')
                                   and time.monotonic() < deadline):
                                time.sleep(0.01)
                            c.dialogue(client, task['id'], pre_question_dialogue, expected_turn=1, command_id='dlg-pre')
                        for idx, reply in enumerate(answers or (), start=1):
                            deadline = time.monotonic() + 5
                            while not c.status(client, task['id']).get('questions') and time.monotonic() < deadline:
                                time.sleep(0.01)
                            questions = c.status(client, task['id']).get('questions', [])
                            self.assertEqual(len(questions), 1, c.status(client, task['id']))
                            c.dialogue(client, task['id'], reply, expected_turn=1,
                                       command_id=f'answer-q{idx}', question_id=questions[0])
                        thread.join(5)
                        self.assertFalse(thread.is_alive())
                        code = outcome[0]
                    finally:
                        if thread.is_alive():
                            c.cancel(client, task['id'])
                            thread.join(5)
                status = c.status(client, task['id'])
                closed = c.close(client, task['id'])
                events = c.events(client, task['id'])
                rpcs = [json.loads(line)['method'] for line in rpc_log.read_text().splitlines() if line.strip()]
                return code, status, closed, events, rpcs

    def test_r1_counterexamples_and_variants_fail_closed(self):
        fail_modes = [
            ('b1b_retry_missing_failed_tool', 'drained_tool_missing', None),
            ('b1c_retry_tool_status_regressed', 'drained_tool_status_regressed', None),
            ('b1d_retry_tool_kind_conflict', 'drained_tool_conflict', None),
            ('b2a_within_drain_completed', 'drained_tool_status_regressed', None),
            ('b2a_within_drain_failed', 'drained_tool_status_regressed', None),
            ('b2b_sparse_swarm_in_drain', 'drained_tool_conflict', None),
            ('b2b_sparse_swarm_in_retry', 'drained_tool_conflict', None),
        ]
        for mode, expected_reason, expected_seq in fail_modes:
            with self.subTest(mode=mode):
                code, status, closed, _, _ = self._run_rework_shim(mode)
                self.assertEqual(code, 1)
                self.assertEqual(status['state'], 'unknown')
                self.assertNotIn('native_result', status)
                self.assertNotIn('native_cleanup', status)
                self.assertEqual(closed['state'], 'unknown')
                diag = status.get('native_observation', {}).get('history_diagnostic', {})
                if expected_reason is not None:
                    self.assertEqual(diag.get('reason_code'), expected_reason)
                if expected_seq is not None:
                    self.assertEqual(diag.get('interaction_seq'), expected_seq)

        # Degraded recovery for live-question omission in retry and persistent inverted tool replays
        code, status, closed, events, rpcs = self._run_rework_shim('b1a_retry_question_omitted', answers=['yes'])
        self.assertEqual(code, 0, status.get('settlement_note'))
        self.assertEqual(status['state'], 'completed')
        self.assertEqual([e['text'] for e in events if e['kind'] == 'question'], ['Q1 Dropped?'])
        self.assertEqual(status['native_result']['result'], 'Done')
        self.assertIs(status['native_observation'].get('history_degraded'), True)
        self.assertEqual(rpcs.count('session/prompt'), 2)
        self.assertEqual(closed['state'], 'closed')

        for mode, expected_seq, kwargs, expected_prompts in (
            ('m1_initial_inversion', 0, {}, 1),
            ('m1_dialogue_inversion', 1, {'answers': ['proceed']}, 2),
        ):
            with self.subTest(mode=mode):
                code, status, closed, _, rpcs = self._run_rework_shim(mode, **kwargs)
                self.assertEqual(code, 0, status.get('settlement_note'))
                self.assertEqual(status['state'], 'completed')
                self.assertIs(status['native_observation'].get('history_degraded'), True)
                diag = status['native_observation']['history_diagnostic']
                self.assertEqual(diag.get('reason_code'), 'inverted_tool_replay')
                self.assertEqual(diag.get('interaction_seq'), expected_seq)
                self.assertEqual(rpcs.count('session/prompt'), expected_prompts)
                self.assertEqual(closed['state'], 'closed')

    def test_masked_fatal_in_attempt1_always_fails_closed(self):
        code, status, closed, _, _ = self._run_rework_shim('masked_fatal_dropped')
        self.assertEqual(code, 1)
        self.assertEqual(status['state'], 'unknown')
        self.assertNotIn('native_result', status)
        self.assertNotIn('native_cleanup', status)
        self.assertEqual(closed['state'], 'unknown')

        code, status, closed, _, _ = self._run_rework_shim('masked_fatal_preserved')
        self.assertEqual(code, 1)
        self.assertEqual(status['state'], 'unknown')
        self.assertIn('AGY reported a fatal execution error', status.get('settlement_note', ''))
        self.assertNotIn('native_result', status)
        self.assertNotIn('native_cleanup', status)
        self.assertEqual(closed['state'], 'unknown')

    def test_valid_retry_updates_sparse_swarm_and_multi_slice_preserve_single_prompt(self):
        code, status, closed, _, rpcs = self._run_rework_shim('inflight_completed_before_stale_replay')
        self.assertEqual(code, 0, status.get('settlement_note'))
        self.assertEqual(status['state'], 'completed', status.get('settlement_note'))
        self.assertEqual(status['native_result']['result'], 'Run /swarm board\nFinal answer')
        self.assertEqual(closed['state'], 'closed')
        self.assertEqual(rpcs.count('session/prompt'), 1)

        for mode in ('inflight_question_preserved_stale_replay', 'inflight_question_preserved_persisted_replay'):
            with self.subTest(mode=mode):
                code, status, closed, events, rpcs = self._run_rework_shim(mode, answers=['yes'])
                self.assertEqual(code, 0, status.get('settlement_note'))
                self.assertEqual(status['state'], 'completed', status.get('settlement_note'))
                self.assertEqual(closed['state'], 'closed')
                self.assertEqual([e['text'] for e in events if e['kind'] == 'question'], ['Q1 Real question?'])
                self.assertEqual(status['native_result']['result'], 'Done')
                self.assertEqual(rpcs.count('session/prompt'), 2)

        code, status, closed, _, rpcs = self._run_rework_shim('normal_sparse_swarm_in_drain')
        self.assertEqual(code, 0, status.get('settlement_note'))
        self.assertEqual(status['state'], 'completed', status.get('settlement_note'))
        self.assertEqual(status['native_result']['result'], 'Run /swarm board\nSwarm normal complete')
        self.assertEqual(closed['state'], 'closed')
        self.assertEqual(rpcs.count('session/prompt'), 1)

        code, status, closed, _, rpcs = self._run_rework_shim('multi_slice_d1_retry_d2_valid')
        self.assertEqual(code, 0, status.get('settlement_note'))
        self.assertEqual(status['state'], 'completed', status.get('settlement_note'))
        self.assertEqual(closed['state'], 'closed')
        self.assertEqual(rpcs.count('session/prompt'), 1)

        code, status, closed, events, rpcs = self._run_rework_shim('multi_slice_d1_retry_d2_dropped')
        self.assertEqual(code, 0, status.get('settlement_note'))
        self.assertEqual(status['state'], 'completed')
        self.assertIs(status['native_observation'].get('history_degraded'), True)
        self.assertTrue(any(e['kind'] == 'answer' and 'Step D2' in e['text'] for e in events))
        self.assertIn('Step D2', status['native_result']['result'])
        self.assertNotIn('Wrong tail', status['native_result']['result'])
        self.assertEqual(rpcs.count('session/prompt'), 1)
        self.assertEqual(closed['state'], 'closed')

        code, status, closed, _, rpcs = self._run_rework_shim('interleaved_post_saved_valid')
        self.assertEqual(code, 0, status.get('settlement_note'))
        self.assertEqual(status['state'], 'completed', status.get('settlement_note'))
        self.assertEqual(closed['state'], 'closed')
        self.assertEqual(rpcs.count('session/prompt'), 1)

        code, status, closed, events, rpcs = self._run_rework_shim('interleaved_post_saved_dropped')
        self.assertEqual(code, 0, status.get('settlement_note'))
        self.assertEqual(status['state'], 'completed')
        self.assertIs(status['native_observation'].get('history_degraded'), True)
        self.assertTrue(any(e['kind'] == 'answer' and 'Step Post' in e['text'] for e in events))
        self.assertIn('Step Post', status['native_result']['result'])
        self.assertEqual(rpcs.count('session/prompt'), 1)
        self.assertEqual(closed['state'], 'closed')

        code, status, closed, events, rpcs = self._run_rework_shim(
            'non_tail_slot_growth_across_retry', answers=['resolved'], pre_question_dialogue='continue',
        )
        self.assertEqual(code, 0, status.get('settlement_note'))
        self.assertEqual(status['state'], 'completed', status.get('settlement_note'))
        self.assertEqual(closed['state'], 'closed')
        self.assertEqual([e['text'] for e in events if e['kind'] == 'question'], ['Q1 Partial question?'])
        self.assertEqual(status['native_result']['result'], 'Resolved after Q1')
        self.assertEqual(rpcs.count('session/prompt'), 3)

    def test_b2_b3_b5_controls_and_interaction_scoped_dedupe(self):
        import threading

        def run_scenario_fixture(scenario):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shim = root / 'agy'
                shim.write_text('#!' + sys.executable + '\n' + (Path(__file__).parent / 'fixtures/agy_native_agent.py').read_text())
                shim.chmod(0o700)
                (root / 'scenario.json').write_text(json.dumps(scenario))
                with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ.get('PATH', ''),
                                AGY_NATIVE_FIXTURE=str(root)), \
                        patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                        patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                    client = c.connect('hermes')['client_id']
                    task = c.start(client, agent='agy', model='ocs', cwd=directory, prompt='b2 controls', write=False)
                    code = run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)
                    return code, c.status(client, task['id']), c.events(client, task['id'])

        # B2: split fatal across chunks + later prose -> unknown; quoted/non-standalone controls -> completed
        split_updates = [
            dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='\u26a0 Agent execution ')),
            dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='terminated due to error.\n')),
            dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Later ordinary prose.\n')),
        ]
        code, st, _ = run_scenario_fixture(dict(foreground=split_updates, replays=[split_updates]))
        self.assertEqual((code, st['state']), (1, 'unknown'))
        self.assertNotIn('native_result', st)

        for ok_line in (
            'The old log showed "\u26a0 Agent execution terminated due to error." and is fixed.\n',
            '\u26a0 Agent execution terminated due to error. Retried successfully.\n',
        ):
            ok_updates = [dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=ok_line))]
            with self.subTest(ok_line=ok_line):
                code, st, _ = run_scenario_fixture(dict(foreground=ok_updates, replays=[ok_updates]))
                self.assertEqual((code, st['state']), (0, 'completed'), st.get('settlement_note'))

        # B3: degraded replay dropping failed tool and expanding pre-failure draft stays HOLD;
        #     explicit live usable answer after failed tool recovers and completes.
        b3_shim = r"""#!/usr/bin/env python3
import json, os, sys
SID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
mode = os.environ['B3_B5_MODE']
loads = 0
closed = False
saved = []
def send(v):
    sys.stdout.write(json.dumps(dict(jsonrpc='2.0', **v)) + '\n')
    sys.stdout.flush()
def emit(u):
    send(dict(method='session/update', params=dict(sessionId=SID, update=u)))
def meta():
    s = ('gemini-pro-orchestrator',)
    return dict(sessionId=SID, models=dict(currentModelId=s[0], availableModels=[dict(modelId=s[0])]),
                configOptions=[dict(id='model', currentValue=s[0], options=[dict(value=s[0], name=s[0])])])
for line in sys.stdin:
    msg = json.loads(line)
    m = msg.get('method')
    if m == 'initialize':
        send(dict(id=msg['id'], result=dict(protocolVersion=1, agentCapabilities=dict(loadSession=True, sessionCapabilities=dict(close={}, resume={}))) ))
    elif m == 'session/new':
        send(dict(id=msg['id'], result=meta()))
        emit(dict(sessionUpdate='available_commands_update', availableCommands=[dict(name='swarm')]))
    elif m == 'session/set_config_option':
        if closed:
            send(dict(id=msg['id'], error=dict(code=-32602, message='Session not found')))
        else:
            send(dict(id=msg['id'], result=meta()))
    elif m == 'session/prompt':
        body = msg['params']['prompt'][0]['text']
        if not body.startswith('CALLCLI_DIALOGUE '):
            if mode == 'b3_dropped_expanded_draft':
                saved = [
                    dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Prior draft.\n')),
                    dict(sessionUpdate='tool_call', toolCallId='write1', name='execute', kind='execute', status='failed'),
                ]
                for e in saved:
                    emit(e)
            elif mode == 'b3_recovered_after_fail':
                saved = [
                    dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)),
                    dict(sessionUpdate='tool_call', toolCallId='write1', name='execute', kind='execute', status='failed'),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Recovered verified result.\n')),
                ]
                for e in saved:
                    emit(e)
            elif mode == 'b5_multi_interaction_dedupe':
                saved = [
                    dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)),
                    dict(sessionUpdate='tool_call', toolCallId='t0', name='read_file', kind='read', status='in_progress'),
                ]
                for e in saved:
                    emit(e)
        else:
            saved.append(dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)))
            saved.append(dict(sessionUpdate='tool_call', toolCallId='t1', name='read_file', kind='read', status='in_progress'))
            emit(saved[-1])
        send(dict(id=msg['id'], result=dict(stopReason='end_turn')))
    elif m == 'session/load':
        if not saved:
            send(dict(id=msg['id'], result=meta()))
            continue
        loads += 1
        post = []
        if mode == 'b3_dropped_expanded_draft':
            replay = [
                saved[0],
                dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Rewritten line 1.\nRewritten line 2.\n')),
            ]
        elif mode == 'b3_recovered_after_fail':
            replay = list(saved)
        elif mode == 'b5_multi_interaction_dedupe':
            if loads == 1:
                saved[1]['status'] = 'completed'
                replay = list(saved)
                post = [
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Shared line\n')),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q1 Next?\n')),
                ]
            elif loads == 2:
                replay = list(saved) + [
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Shared line\n')),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q1 Next?\n')),
                ]
            elif loads == 3:
                emit(dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file', kind='read', status='in_progress'))
                emit(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Step Pre1\n')))
                replay = list(saved)
                post = [
                    dict(sessionUpdate='tool_call_update', toolCallId='t1', name='read_file', kind='read', status='completed'),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Shared line\n')),
                ]
            else:
                for e in saved:
                    if e.get('toolCallId') == 't1':
                        e['status'] = 'completed'
                replay = list(saved) + [
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Step Pre1\n')),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Shared line\n')),
                ]
        for e in replay:
            emit(e)
        send(dict(id=msg['id'], result=meta()))
        for e in post:
            emit(e)
    elif m == 'session/close':
        closed = True
        send(dict(id=msg['id'], result={}))
"""
        for mode in ('b3_dropped_expanded_draft', 'b3_recovered_after_fail', 'b5_multi_interaction_dedupe'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shim = root / 'agy'
                shim.write_text(b3_shim)
                shim.chmod(0o700)
                with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ.get('PATH', ''),
                                B3_B5_MODE=mode), \
                        patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                        patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                    client = c.connect('hermes')['client_id']
                    task = c.start(client, agent='agy', model='ocs', cwd=directory, prompt='b3 b5 check', write=False)
                    if mode != 'b5_multi_interaction_dedupe':
                        code = run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)
                        st = c.status(client, task['id'])
                        if mode == 'b3_dropped_expanded_draft':
                            self.assertEqual((code, st['state']), (1, 'unknown'))
                            self.assertNotIn('native_result', st)
                        else:
                            self.assertEqual((code, st['state']), (0, 'completed'), st.get('settlement_note'))
                            self.assertEqual(st['native_result']['result'], 'Recovered verified result.')
                    else:
                        out_rc = []
                        th = threading.Thread(target=lambda: out_rc.append(
                            run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)))
                        th.start()
                        deadline = time.monotonic() + 5
                        while th.is_alive() and not c.status(client, task['id']).get('questions') and time.monotonic() < deadline:
                            time.sleep(0.01)
                        q_id = c.status(client, task['id'])['questions'][0]
                        c.dialogue(client, task['id'], 'proceed', expected_turn=1, command_id='dlg-b5', question_id=q_id)
                        th.join(6)
                        self.assertFalse(th.is_alive())
                        self.assertEqual(out_rc, [0])
                        st = c.status(client, task['id'])
                        self.assertEqual(st['state'], 'completed', st.get('settlement_note'))
                        self.assertEqual(st['native_result']['interaction_seq'], 1)
                        self.assertIn('Shared line', st['native_result']['result'])
                        evs = c.events(client, task['id'])
                        shared_by_seq = [e.get('interaction_seq') for e in evs if e['kind'] == 'answer' and e['text'] == 'Shared line']
                        self.assertEqual(shared_by_seq, [0, 1])

    def test_m2_dead_attributes_removed_and_m3_load_cancel_race_closed(self):
        import inspect
        import selectors
        from types import SimpleNamespace
        from agy_native import NativeOwner

        init_src = inspect.getsource(NativeOwner.__init__)
        self.assertNotIn('last_load_orphan_updates', init_src)
        self.assertNotIn('last_load_duplicate_creates', init_src)
        self.assertNotIn('unreconciled_load_fatal', init_src)
        self.assertIn('pending_retry_state', init_src)

        with tempfile.TemporaryDirectory() as directory:
            with patch('subprocess.Popen') as popen:
                popen.return_value = SimpleNamespace(stdout=SimpleNamespace(fileno=lambda: 0))
                with patch('selectors.DefaultSelector') as sel_cls:
                    sel_cls.return_value = SimpleNamespace(register=lambda *a, **k: None)
                    real_owner = NativeOwner(dict(device='local', cwd=directory))
            self.assertFalse(hasattr(real_owner, 'last_load_orphan_updates'))
            self.assertFalse(hasattr(real_owner, 'last_load_duplicate_creates'))
            self.assertFalse(hasattr(real_owner, 'unreconciled_load_fatal'))
            self.assertIsNone(real_owner.pending_retry_state)

        def make_pipe_owner():
            r_out, w_out = os.pipe()
            r_in, w_in = os.pipe()
            owner = object.__new__(NativeOwner)
            owner.task = dict(cwd='/tmp', agy_approval='approve-all')
            owner.session_id = 'owned'
            owner.serial = 0
            owner.buffer = b''
            owner.permission_decisions = []
            owner.review = None
            owner.review_blocked = False
            owner.available_commands = set()
            owner.permission_handler = None
            owner.workflow_identity = {}
            owner.approval_wait_seconds = 0.0
            owner.pending_drained = []
            owner.drain_workflow_calls = set()
            owner.drained_public_text = ''
            owner.pending_retry_state = None
            owner.proc = SimpleNamespace(
                stdout=os.fdopen(r_out, 'rb', buffering=0),
                stdin=os.fdopen(w_in, 'wb', buffering=0),
            )
            owner.poller = selectors.DefaultSelector()
            owner.poller.register(owner.proc.stdout, selectors.EVENT_READ)
            reader = os.fdopen(r_in, 'rb', buffering=0)
            writer = os.fdopen(w_out, 'wb', buffering=0)
            def cleanup():
                owner.poller.close()
                for h in (owner.proc.stdout, owner.proc.stdin, reader, writer):
                    try:
                        h.close()
                    except OSError:
                        pass
            return owner, reader, writer, cleanup

        # Pre-load cancel inside drain_pending: 1 session/cancel, 0 session/load
        owner, reader, writer, cleanup = make_pipe_owner()
        try:
            writer.write((json.dumps(dict(
                jsonrpc='2.0', method='session/update',
                params=dict(sessionId='owned', update=dict(
                    sessionUpdate='agent_message_chunk', content=dict(type='text', text='x'),
                )),
            )) + '\n').encode())
            writer.flush()
            cancel_state = {'n': 0}
            def check_preload():
                cancel_state['n'] += 1
                return cancel_state['n'] >= 2
            res, hist = owner.load(reconcile_stream=True, check_cancel=check_preload)
            self.assertEqual((res, hist), ({}, []))
            self.assertIs(owner.request_cancel_sent, True)
            writer.close()
            owner.proc.stdin.close()
            sent = [json.loads(line) for line in reader.read().decode().splitlines() if line.strip()]
            self.assertEqual([m.get('method') for m in sent], ['session/cancel'])
        finally:
            cleanup()

        # Pre-retry cancel inside d2 drain_pending: 1 session/load, 1 session/cancel, 0 duplicate cancel or load #2
        owner, reader, writer, cleanup = make_pipe_owner()
        try:
            def serve_attempt1():
                first = json.loads(reader.readline().decode())
                self.assertEqual(first.get('method'), 'session/load')
                payload = ''.join(json.dumps(dict(jsonrpc='2.0', **frame)) + '\n' for frame in (
                    dict(method='session/update', params=dict(sessionId='owned', update=dict(
                        sessionUpdate='tool_call_update', toolCallId='t1', kind='read', status='in_progress',
                    ))),
                    dict(id=first['id'], result=dict(sessionId='owned')),
                    dict(method='session/update', params=dict(sessionId='owned', update=dict(
                        sessionUpdate='agent_message_chunk', content=dict(type='text', text='d2'),
                    ))),
                ))
                writer.write(payload.encode())
                writer.flush()
            import threading
            srv = threading.Thread(target=serve_attempt1)
            srv.start()
            retry_checks = {'n': 0}
            def check_preretry():
                if not owner.last_load_retried:
                    return False
                retry_checks['n'] += 1
                return retry_checks['n'] >= 2
            res, hist = owner.load(reconcile_stream=True, check_cancel=check_preretry)
            srv.join(5)
            self.assertFalse(srv.is_alive())
            self.assertEqual((res, hist), ({}, []))
            self.assertIs(owner.request_cancel_sent, True)
            self.assertEqual(len(owner.pending_drained), 1)
            self.assertEqual(owner.pending_drained[0].get('content', {}).get('text'), 'd2')
            self.assertIsInstance(owner.pending_retry_state, dict)
            self.assertEqual([e.get('toolCallId') for e in owner.pending_retry_state['attempt_updates']], ['t1'])
            self.assertEqual([e.get('content', {}).get('text') for e in owner.pending_retry_state['d2_updates']], ['d2'])
            writer.close()
            owner.proc.stdin.close()
            rest = [json.loads(line) for line in reader.read().decode().splitlines() if line.strip()]
            self.assertEqual([m.get('method') for m in rest], ['session/cancel'])
        finally:
            cleanup()

    def test_dialogue_missing_compacted_incidents_degrade_and_complete_without_prompt_replay(self):
        import threading
        shim_code = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
cfg = json.loads(Path(os.environ['AGY_INCIDENT_CFG']).read_text())
dlgs = cfg['dialogues']
SID = '77777777-7777-4777-8777-777777777777'
prompt_count = 0
post_loads = 0
closed = False
saved = []
prompts = []
def send(v):
    sys.stdout.write(json.dumps(dict(jsonrpc='2.0', **v)) + '\n')
    sys.stdout.flush()
def emit(u):
    send(dict(method='session/update', params=dict(sessionId=SID, update=u)))
def meta():
    s = ('gemini-pro-orchestrator',)
    return dict(sessionId=SID, models=dict(currentModelId=s[0], availableModels=[dict(modelId=s[0])]),
                configOptions=[dict(id='model', currentValue=s[0], options=[dict(value=s[0], name=s[0])])])
for line in sys.stdin:
    msg = json.loads(line)
    m = msg.get('method')
    if m == 'initialize':
        send(dict(id=msg['id'], result=dict(protocolVersion=1, agentCapabilities=dict(loadSession=True, sessionCapabilities=dict(close={}, resume={})))))
    elif m == 'session/new':
        send(dict(id=msg['id'], result=meta()))
        emit(dict(sessionUpdate='available_commands_update', availableCommands=[dict(name='swarm')]))
    elif m == 'session/set_config_option':
        if closed:
            send(dict(id=msg['id'], error=dict(code=-32602, message='Session not found')))
        else:
            send(dict(id=msg['id'], result=meta()))
    elif m == 'session/prompt':
        body = msg['params']['prompt'][0]['text']
        prompt_count += 1
        prompts.append(body)
        Path(cfg['prompt_count_file']).write_text(str(prompt_count))
        if prompt_count <= dlgs:
            q = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=f'CALLCLI_QUESTION Q{prompt_count} Synthetic ask {prompt_count}?\n'))
            saved.extend([dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)), q])
            emit(q)
            send(dict(id=msg['id'], result=dict(stopReason='end_turn')))
        else:
            pairs = 48 if dlgs == 3 else 32
            saved.append(dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)))
            for i in range(1, pairs + 1):
                saved.append(dict(sessionUpdate='tool_call', toolCallId=f't{i}', name='read_file', kind='read', status='in_progress' if i == pairs else 'completed'))
                saved.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=f'CALLCLI_STAGE synthetic {i}\n')))
            emit(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_STAGE synthetic 1\n')))
            send(dict(id=msg['id'], result=dict(stopReason='end_turn')))
    elif m == 'session/load':
        if closed:
            send(dict(id=msg['id'], error=dict(code=-32602, message='Session not found')))
            continue
        if prompt_count == dlgs + 1:
            post_loads += 1
            if post_loads == 2:
                compacted = [dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=prompts[0]))]
                for i in range(1, 24):
                    compacted.append(dict(sessionUpdate='tool_call', toolCallId=f'c{i}', name='read_file', kind='read', status='completed'))
                    compacted.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=f'CALLCLI_STAGE compacted {i}\n')))
                if dlgs == 1:
                    compacted.append(dict(sessionUpdate='tool_call', toolCallId='c24', name='read_file', kind='read', status='completed'))
                compacted.append(dict(sessionUpdate='tool_call', toolCallId=f't{48 if dlgs == 3 else 32}', name='read_file', kind='read', status='completed'))
                compacted.append(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=f'Compacted final deliverable seq {dlgs}\n')))
                saved = compacted
        for e in saved:
            emit(e)
        send(dict(id=msg['id'], result=meta()))
    elif m == 'session/close':
        closed = True
        send(dict(id=msg['id'], result={}))
"""
        for dlgs, prev_len, curr_len in ((3, 103, 49), (1, 67, 50)):
            with self.subTest(dialogues=dlgs), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shim = root / 'agy'
                shim.write_text(shim_code)
                shim.chmod(0o700)
                pc_file = root / 'pcount.txt'
                cfg = root / 'cfg.json'
                cfg.write_text(json.dumps(dict(dialogues=dlgs, prompt_count_file=str(pc_file))))
                with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'], AGY_INCIDENT_CFG=str(cfg)), \
                        patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                        patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                    client = c.connect('hermes')['client_id']
                    task = c.start(client, agent='agy', model='ocs', cwd=directory, prompt='synthetic incident structure', write=False)
                    rc_box = []
                    th = threading.Thread(target=lambda: rc_box.append(run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)))
                    th.start()
                    for idx in range(1, dlgs + 1):
                        deadline = time.monotonic() + 5
                        while th.is_alive() and not c.status(client, task['id']).get('questions') and time.monotonic() < deadline:
                            time.sleep(0.01)
                        q = c.status(client, task['id'])['questions'][0]
                        c.dialogue(client, task['id'], f'ans {idx}', expected_turn=1, command_id=f'd{idx}', question_id=q)
                        deadline = time.monotonic() + 5
                        while th.is_alive() and c.status(client, task['id'])['commands'][f'd{idx}']['status'] != 'returned' and time.monotonic() < deadline:
                            time.sleep(0.01)
                    th.join(8)
                    self.assertFalse(th.is_alive())
                    self.assertEqual(rc_box, [0])
                    st = c.status(client, task['id'])
                    self.assertEqual(st['state'], 'completed')
                    self.assertEqual(st['native_result']['result'], f'Compacted final deliverable seq {dlgs}')
                    self.assertEqual(st['native_result']['interaction_seq'], dlgs)
                    self.assertTrue(st['native_observation']['history_degraded'])
                    diag = st['native_observation']['history_diagnostic']
                    self.assertEqual((diag['reason_code'], diag['interaction_seq'], diag['previous_len'], diag['current_len']),
                                     ('dialogue_missing', dlgs, prev_len, curr_len))
                    self.assertEqual(int(pc_file.read_text()), dlgs + 1)
                    self.assertEqual(c.close(client, task['id'])['state'], 'closed')
