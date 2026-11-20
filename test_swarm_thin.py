"""Public offline Swarm bridge with native ACP fixture, no provider."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch
from collab import Collaboration
from runner import run_task

class SwarmThinTests(unittest.TestCase):
    @contextlib.contextmanager
    def workspace(self, fault='normal'):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            executable=root/'agy'
            executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            with patch.dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH'],AGY_NATIVE_FIXTURE=directory,
                    AGY_SWARM_THIN=fault), patch('updates.DEFAULT_LOCK_ROOT',root/'locks'), \
                    patch('updates.ensure_current',return_value={'decision':'fixture'}),contextlib.redirect_stdout(io.StringIO()):
                c=Collaboration(root/'tasks.db',launch=lambda task:None)
                client=c.connect('hermes')['client_id']
                task=c.start(client,agent='agy',model='ocs',cwd=directory,prompt='Write the requested short result',write=True)
                thread=threading.Thread(target=run_task,args=(c.store.path,task['id']),kwargs=dict(turn=1,launch_id=task['launch_id'],epoch=1))
                thread.start()
                try:yield root,c,client,task,thread
                finally:
                    if thread.is_alive():c.cancel(client,task['id']);thread.join(5)
                    self.assertFalse(thread.is_alive())

    def test_native_public_answer_without_member_receipts_completes_and_closes(self):
        with self.workspace() as (root,c,client,task,thread):
            thread.join(5)
            self.assertFalse(thread.is_alive())
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'completed',status.get('settlement_note'))
            self.assertEqual((root/'result.txt').read_text(),'initial')
            self.assertEqual([e['text'] for e in c.events(client,task['id']) if e['kind']=='answer'],['Native final initial'])
            prompt=(root/'prompt').read_text()
            self.assertTrue(prompt.startswith('/swarm\n'))
            self.assertNotIn('CALLCLI_RESULT',prompt)
            self.assertNotIn('report to @coordinator',prompt)
            self.assertNotIn('agy_worker_model',status)
            self.assertFalse(status['native_cleanup']['all_descendants_proven_stopped'])
            self.assertEqual(c.close(client,task['id'])['state'],'closed')

    def test_question_answer_and_revision_stay_on_original_owner(self):
        with self.workspace('question') as (root,c,client,task,thread):
            deadline=time.monotonic()+4
            while not c.status(client,task['id']).get('questions') and time.monotonic()<deadline: time.sleep(.01)
            question=c.status(client,task['id'])['questions'][0]
            c.dialogue(client,task['id'],'use revised label',expected_turn=1,command_id='revision',question_id=question)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'completed',status.get('settlement_note'))
            self.assertEqual(status['questions'],[])
            self.assertEqual(status['commands']['revision']['status'],'returned')
            self.assertEqual((root/'result.txt').read_text(),'revised')
            self.assertEqual(status['native_result']['interaction_seq'],1)
            self.assertEqual(c.close(client,task['id'])['state'],'closed')
            requests=[json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
            self.assertEqual(sum(r.get('method')=='session/new' for r in requests),1)
            self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests),2)
            self.assertFalse(any(r.get('method')=='session/cancel' for r in requests))

    def test_cancel_is_forwarded_once_without_business_or_member_cleanup_replay(self):
        with self.workspace('block') as (root,c,client,task,thread):
            deadline=time.monotonic()+4
            while not (root/'active').exists() and time.monotonic()<deadline: time.sleep(.01)
            self.assertTrue((root/'active').exists())
            c.cancel(client,task['id'])
            thread.join(5)
            self.assertFalse(thread.is_alive())
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'cancelled')
            self.assertTrue(status['native_cancel']['session_unloaded'])
            self.assertFalse(status['native_cleanup']['all_descendants_proven_stopped'])
            requests=[json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
            self.assertEqual(sum(r.get('method')=='session/cancel' for r in requests),1)
            self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests),1)
            self.assertEqual(sum(r.get('method')=='session/close' for r in requests),1)
            self.assertEqual(c.close(client,task['id'])['state'],'closed')
            second=c.start(client,prompt='non-conflicting after verified cancel',cwd=str(root),write=True)
            c.cancel(client,second['id'])

    def test_fatal_and_empty_response_never_become_success(self):
        for fault in ('fatal','empty'):
            with self.subTest(fault=fault),self.workspace(fault) as (root,c,client,task,thread):
                thread.join(5)
                self.assertFalse(thread.is_alive())
                status=c.status(client,task['id'])
                self.assertEqual(status['state'],'unknown')
                self.assertNotIn('native_cleanup',status)
                self.assertEqual(c.close(client,task['id'])['state'],'unknown')

    def test_missing_swarm_command_never_sends_plain_prompt(self):
        with self.workspace('no-command') as (root,c,client,task,thread):
            thread.join(4)
            self.assertFalse(thread.is_alive())
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'unknown')
            self.assertIn('not advertised',status.get('settlement_note',''))
            requests=[json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
            self.assertFalse(any(r.get('method')=='session/prompt' for r in requests))

    def test_active_public_tool_retains_owner_and_writer_until_result(self):
        with self.workspace('async') as (root,c,client,task,thread):
            deadline=time.monotonic()+4
            while not any(e['kind']=='stage' for e in c.events(client,task['id'])) and time.monotonic()<deadline: time.sleep(.01)
            self.assertEqual(c.status(client,task['id'])['state'],'running')
            other=c.connect('hermes')['client_id']
            with self.assertRaises(PermissionError): c.events(other,task['id'])
            with self.assertRaises(ValueError): c.start(client,prompt='writer conflict',cwd=str(root),write=True)
            (root/'finish').touch()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(c.status(client,task['id'])['state'],'completed')
            self.assertEqual((root/'result.txt').read_text(),'initial')

    def test_enriched_old_tool_does_not_erase_complete_public_answer(self):
        with self.workspace('enrich') as (root,c,client,task,thread):
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(c.status(client,task['id'])['state'],'completed')

    def test_late_old_answer_cannot_complete_unanswered_revision(self):
        with self.workspace('late-old') as (root,c,client,task,thread):
            deadline=time.monotonic()+4
            while not c.status(client,task['id']).get('questions') and time.monotonic()<deadline: time.sleep(.01)
            q=c.status(client,task['id'])['questions'][0]
            c.dialogue(client,task['id'],'new requirement',expected_turn=1,command_id='new',question_id=q)
            deadline=time.monotonic()+4
            while c.status(client,task['id'])['commands']['new']['status']!='returned' and time.monotonic()<deadline: time.sleep(.01)
            time.sleep(.3)
            self.assertEqual(c.status(client,task['id'])['state'],'running')
            self.assertNotIn('native_cleanup',c.status(client,task['id']))
            c.cancel(client,task['id']);thread.join(4)

    def test_unterminated_background_final_answer_is_published_before_completion(self):
        with self.workspace('replay-no-newline') as (root,c,client,task,thread):
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(c.status(client,task['id'])['state'],'completed')
            self.assertEqual([e['text'] for e in c.events(client,task['id']) if e['kind']=='answer'],['Native final replay'])

    def test_board_working_progress_keeps_owner_and_accepts_revision_until_final_result(self):
        with self.workspace('board-working') as (root,c,client,task,thread):
            deadline=time.monotonic()+4
            while not any('Run /swarm board' in e['text'] for e in c.events(client,task['id'])) and time.monotonic()<deadline: time.sleep(.01)
            time.sleep(.2)
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'running')
            self.assertNotIn('native_result',status)
            self.assertNotIn('native_cleanup',status)
            self.assertTrue(status['native_input']['accepting'])
            requests=[json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
            self.assertFalse(any(r.get('method')=='session/close' for r in requests))
            c.dialogue(client,task['id'],'return actual revised result',expected_turn=1,command_id='actual')
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(c.status(client,task['id'])['state'],'completed')
            self.assertEqual((root/'result.txt').read_text(),'revised')
            self.assertEqual(c.close(client,task['id'])['state'],'closed')

    def test_stage_only_and_board_only_each_keep_dialogue_open(self):
        for fault in ('status-working','board-only'):
            with self.subTest(fault=fault),self.workspace(fault) as (root,c,client,task,thread):
                deadline=time.monotonic()+4
                while not any(e['kind']=='stage' if fault=='status-working' else e['kind']=='answer' for e in c.events(client,task['id'])):
                    if time.monotonic()>deadline: self.fail('Native progress missing')
                    time.sleep(.01)
                time.sleep(.15)
                status=c.status(client,task['id'])
                self.assertEqual(status['state'],'running')
                self.assertTrue(status['native_input']['accepting'])
                self.assertNotIn('native_cleanup',status)
                c.dialogue(client,task['id'],'return final revised result',expected_turn=1,command_id='finish')
                thread.join(5)
                self.assertEqual(c.status(client,task['id'])['state'],'completed')
                self.assertEqual((root/'result.txt').read_text(),'revised')

    def test_normal_final_answer_may_mention_board_instruction(self):
        with self.workspace('final-board-mention') as (root,c,client,task,thread):
            thread.join(4)
            self.assertFalse(thread.is_alive())
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'completed')
            self.assertIn('Run /swarm board',status['native_result']['result'])
            self.assertEqual(c.close(client,task['id'])['state'],'closed')

    def test_new_status_observation_supersedes_historical_working(self):
        with self.workspace('status-new-id') as (root,c,client,task,thread):
            deadline=time.monotonic()+4
            while not any(e['kind']=='answer' for e in c.events(client,task['id'])) and time.monotonic()<deadline: time.sleep(.01)
            time.sleep(.15)
            self.assertEqual(c.status(client,task['id'])['state'],'running')
            c.dialogue(client,task['id'],'return final revised result',expected_turn=1,command_id='finish')
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(c.status(client,task['id'])['state'],'completed')
            self.assertEqual(c.close(client,task['id'])['state'],'closed')


_CONTINUITY_FAKE_AGY = textwrap.dedent("""\
    #!__PYTHON__
    import json
    import os
    from pathlib import Path
    import sys
    import time

    root = Path(os.environ['SWARM_CONTINUITY_DIR'])
    mode = os.environ.get('SWARM_CONTINUITY_MODE', 'multiline_wait')
    state_file = root / 'acp_state.json'

    def load_state():
        if state_file.exists():
            return json.loads(state_file.read_text())
        return {'next_seq': 1, 'sessions': {}, 'swarm_sessions': []}

    def save_state(st):
        state_file.write_text(json.dumps(st))

    def send(payload):
        print(json.dumps({'jsonrpc': '2.0', **payload}), flush=True)

    def metadata(sid, active_model='gemini-pro-orchestrator'):
        return {
            'sessionId': sid,
            'models': {
                'currentModelId': active_model,
                'availableModels': [
                    {'modelId': 'gemini-pro-orchestrator'},
                    {'modelId': 'gemini-pro-orchestrator-argon'},
                ],
            },
            'configOptions': [
                {
                    'id': 'model',
                    'currentValue': active_model,
                    'options': [
                        {'value': 'gemini-pro-orchestrator', 'name': 'Orchestrator'},
                        {'value': 'gemini-pro-orchestrator-argon', 'name': 'Orchestrator Argon'},
                    ],
                },
            ],
        }

    def emit_update(sid, update):
        send({'method': 'session/update', 'params': {'sessionId': sid, 'update': update}})

    for raw in sys.stdin:
        msg = json.loads(raw)
        method = msg.get('method')
        with (root / 'requests.jsonl').open('a') as f:
            f.write(json.dumps(msg) + '\\n')

        if method == 'initialize':
            send({
                'id': msg['id'],
                'result': {
                    'protocolVersion': 1,
                    'agentCapabilities': {'loadSession': True, 'sessionCapabilities': {'close': {}, 'resume': {}}},
                },
            })
        elif method == 'session/new':
            st = load_state()
            idx = st['next_seq']
            st['next_seq'] += 1
            sid = f'00000000-0000-4000-8000-{idx:012d}'
            st['sessions'][sid] = {
                'closed': False,
                'history': [],
                'post_prompt_loads': 0,
                'prompted_at': None,
                'model': 'gemini-pro-orchestrator',
                'phase': 0,
            }
            save_state(st)
            send({'id': msg['id'], 'result': metadata(sid)})
            emit_update(sid, {
                'sessionUpdate': 'available_commands_update',
                'availableCommands': [{'name': 'swarm'}, {'name': 'teamwork'}],
            })
        elif method == 'session/set_config_option':
            sid = msg['params']['sessionId']
            st = load_state()
            sess = st['sessions'].get(sid)
            if not sess or sess.get('closed'):
                send({'id': msg['id'], 'error': {'code': -32602, 'message': 'Session not found'}})
            else:
                if msg['params'].get('configId') == 'model':
                    sess['model'] = msg['params']['value']
                    save_state(st)
                send({'id': msg['id'], 'result': metadata(sid, sess.get('model', 'gemini-pro-orchestrator'))})
        elif method == 'session/load':
            sid = msg['params']['sessionId']
            st = load_state()
            sess = st['sessions'].get(sid)
            if not sess or sess.get('closed'):
                send({'id': msg['id'], 'error': {'code': -32602, 'message': 'Session not found'}})
                continue
            if sess.get('prompted_at') is not None:
                sess['post_prompt_loads'] += 1
            phase = sess.get('phase', 0)
            if sess.get('prompted_at') is not None and sid in st['swarm_sessions']:
                if mode in {'multiline_wait', 'terse_wait_no_board_hint', 'read_board_still_pending', 'partial_kill_still_pending', 'status_unknown_rawoutput'} and phase == 0 and (root / 'release_wakeup').exists():
                    left = (root / 'left.txt').read_text() if (root / 'left.txt').exists() else ''
                    right = (root / 'right.txt').read_text() if (root / 'right.txt').exists() else ''
                    (root / 'result.txt').write_text(f'{left}|{right}')
                    sess['history'].extend([
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'wakeup-read-board',
                            'name': 'read_board',
                            'kind': 'other',
                            'status': 'completed',
                        },
                        {
                            'sessionUpdate': 'agent_message_chunk',
                            'content': {'type': 'text', 'text': f'Swarm complete: {left}|{right}\\n'},
                        },
                    ])
                    sess['phase'] = 1
                elif mode == 'wakeup_working_status_with_board_hint':
                    if phase == 0 and (root / 'release_wakeup_1').exists():
                        sess['history'].extend([
                            {
                                'sessionUpdate': 'tool_call',
                                'toolCallId': 'hop1-read-board',
                                'name': 'read_board',
                                'kind': 'other',
                                'status': 'completed',
                            },
                            {
                                'sessionUpdate': 'tool_call',
                                'toolCallId': 'hop1-status-working',
                                'name': 'swarm',
                                'kind': 'other',
                                'status': 'completed',
                                'rawInput': {'action': 'status'},
                                'rawOutput': {'workers': [
                                    {'name': 'left', 'status': 'completed'},
                                    {'name': 'right', 'status': 'working'},
                                ]},
                            },
                            {
                                'sessionUpdate': 'agent_message_chunk',
                                'content': {
                                    'type': 'text',
                                    'text': (
                                        'Left finished; right is still working.\\n'
                                        'Run /swarm board (default shortcut ctrl+b) to open the live board.\\n'
                                    ),
                                },
                            },
                        ])
                        sess['phase'] = 1
                    elif phase == 1 and (root / 'release_wakeup_2').exists():
                        left = (root / 'left.txt').read_text() if (root / 'left.txt').exists() else ''
                        right = (root / 'right.txt').read_text() if (root / 'right.txt').exists() else ''
                        (root / 'result.txt').write_text(f'{left}|{right}')
                        sess['history'].extend([
                            {
                                'sessionUpdate': 'tool_call',
                                'toolCallId': 'hop2-read-board',
                                'name': 'read_board',
                                'kind': 'other',
                                'status': 'completed',
                            },
                            {
                                'sessionUpdate': 'agent_message_chunk',
                                'content': {'type': 'text', 'text': f'Swarm complete: {left}|{right}\\n'},
                            },
                        ])
                        sess['phase'] = 2
                elif mode == 'dialogue_redispatch' and phase == 1 and (root / 'release_wakeup').exists():
                    left = (root / 'left.txt').read_text() if (root / 'left.txt').exists() else ''
                    right = (root / 'right.txt').read_text() if (root / 'right.txt').exists() else ''
                    (root / 'result.txt').write_text(f'{left}|{right}')
                    sess['history'].extend([
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'wakeup-read-board-2',
                            'name': 'read_board',
                            'kind': 'other',
                            'status': 'completed',
                        },
                        {
                            'sessionUpdate': 'agent_message_chunk',
                            'content': {'type': 'text', 'text': f'Swarm complete: {left}|{right}\\n'},
                        },
                    ])
                    sess['phase'] = 2
                elif mode == 'multihop_done':
                    if phase == 0 and (root / 'release_wakeup_1').exists():
                        sess['history'].extend([
                            {
                                'sessionUpdate': 'tool_call',
                                'toolCallId': 'hop1-read',
                                'name': 'read_board',
                                'kind': 'other',
                                'status': 'completed',
                            },
                            {
                                'sessionUpdate': 'tool_call',
                                'toolCallId': 'hop1-add-verifier',
                                'name': 'swarm',
                                'kind': 'other',
                                'status': 'completed',
                                'rawInput': {'action': 'add', 'name': 'verifier'},
                            },
                            {
                                'sessionUpdate': 'agent_message_chunk',
                                'content': {
                                    'type': 'text',
                                    'text': (
                                        'Left finished; spawned verifier and waiting for board confirmation.\\n'
                                        'Run /swarm board (default shortcut ctrl+b) to open the live board.\\n'
                                    ),
                                },
                            },
                        ])
                        sess['phase'] = 1
                    elif phase == 1 and (root / 'release_wakeup_2').exists():
                        left = (root / 'left.txt').read_text() if (root / 'left.txt').exists() else ''
                        right = (root / 'right.txt').read_text() if (root / 'right.txt').exists() else ''
                        (root / 'result.txt').write_text(f'{left}|{right}')
                        sess['history'].extend([
                            {
                                'sessionUpdate': 'tool_call',
                                'toolCallId': 'hop2-read',
                                'name': 'read_board',
                                'kind': 'other',
                                'status': 'completed',
                            },
                            {
                                'sessionUpdate': 'tool_call',
                                'toolCallId': 'hop2-kill-verifier',
                                'name': 'swarm',
                                'kind': 'other',
                                'status': 'completed',
                                'rawInput': {'action': 'kill', 'name': 'verifier'},
                            },
                            {
                                'sessionUpdate': 'tool_call',
                                'toolCallId': 'hop2-done-post',
                                'name': 'post_message',
                                'kind': 'other',
                                'status': 'completed',
                                'rawInput': {'text': f'[done] verified {left}|{right}'},
                            },
                            {
                                'sessionUpdate': 'tool_call',
                                'toolCallId': 'hop2-coord-post',
                                'name': 'post_message',
                                'kind': 'other',
                                'status': 'completed',
                                'rawInput': {'text': f'@coordinator [done] verified {left}|{right}'},
                            },
                            {
                                'sessionUpdate': 'agent_message_chunk',
                                'content': {
                                    'type': 'text',
                                    'text': (
                                        f'Final verified: {left}|{right}.\\n'
                                        'Run /swarm board (default shortcut ctrl+b) to open the live board.\\n'
                                    ),
                                },
                            },
                        ])
                        sess['phase'] = 2
                elif mode == 'stale_status_pending':
                    if phase == 0 and (root / 'release_wakeup_1').exists():
                        sess['history'].extend([
                            {
                                'sessionUpdate': 'tool_call',
                                'toolCallId': 'hop1-status',
                                'name': 'swarm',
                                'kind': 'other',
                                'status': 'completed',
                                'rawInput': {'action': 'status'},
                                'rawOutput': {'workers': [{'name': 'left', 'status': 'working'}]},
                            },
                            {
                                'sessionUpdate': 'agent_message_chunk',
                                'content': {'type': 'text', 'text': 'CALLCLI_STAGE still working\\n'},
                            },
                        ])
                        sess['phase'] = 1
                    elif phase == 1 and (root / 'release_wakeup_2').exists():
                        sess['history'].extend([
                            {
                                'sessionUpdate': 'tool_call',
                                'toolCallId': 'hop2-read',
                                'name': 'read_board',
                                'kind': 'other',
                                'status': 'completed',
                            },
                            {
                                'sessionUpdate': 'agent_message_chunk',
                                'content': {'type': 'text', 'text': 'Final result without extra status poll\\n'},
                            },
                        ])
                        sess['phase'] = 2
                elif mode == 'bg_fatal' and phase == 0 and (root / 'release_wakeup').exists():
                    sess['history'].append({
                        'sessionUpdate': 'agent_message_chunk',
                        'content': {'type': 'text', 'text': '\\u26a0 Agent execution terminated due to error.\\n'},
                    })
                    sess['phase'] = 1
            save_state(st)
            for item in sess['history']:
                emit_update(sid, item)
            send({'id': msg['id'], 'result': metadata(sid, sess.get('model', 'gemini-pro-orchestrator'))})
        elif method == 'session/prompt':
            sid = msg['params']['sessionId']
            body = msg['params']['prompt'][0]['text']
            st = load_state()
            sess = st['sessions'][sid]
            sess['prompted_at'] = time.time()
            if not body.startswith('CALLCLI_DIALOGUE '):
                (root / 'prompt').write_text(body)
                st['swarm_sessions'].append(sid)
                (root / 'left.txt').write_text('ALPHA')
                (root / 'right.txt').write_text('BETA')
                if mode == 'failed_add':
                    events = [
                        {
                            'sessionUpdate': 'user_message_chunk',
                            'content': {'type': 'text', 'text': body},
                        },
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'call_add_failed',
                            'name': 'swarm',
                            'kind': 'other',
                            'status': 'failed',
                            'rawInput': {'action': 'add', 'name': 'left'},
                        },
                        {
                            'sessionUpdate': 'agent_message_chunk',
                            'content': {'type': 'text', 'text': 'Unable to spawn worker left: quota exceeded.\\n'},
                        },
                    ]
                elif mode == 'fg_invalid_output':
                    events = [
                        {
                            'sessionUpdate': 'user_message_chunk',
                            'content': {'type': 'text', 'text': body},
                        },
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'call_add_left',
                            'name': 'swarm',
                            'kind': 'other',
                            'status': 'completed',
                            'rawInput': {'action': 'add', 'name': 'left'},
                        },
                        {
                            'sessionUpdate': 'agent_message_chunk',
                            'content': {'type': 'text', 'text': 'CALLCLI_STAGE launched\\n\\u26a0 Model produced invalid output.\\n'},
                        },
                    ]
                elif mode == 'read_board_still_pending':
                    events = [
                        {
                            'sessionUpdate': 'user_message_chunk',
                            'content': {'type': 'text', 'text': body},
                        },
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'call_add_left',
                            'name': 'swarm',
                            'kind': 'other',
                            'status': 'completed',
                            'rawInput': {'action': 'add', 'name': 'left'},
                        },
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'call_add_right',
                            'name': 'swarm',
                            'kind': 'other',
                            'status': 'completed',
                            'rawInput': {'action': 'add', 'name': 'right'},
                        },
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'call_read_board_pending',
                            'name': 'read_board',
                            'kind': 'other',
                            'status': 'completed',
                        },
                        {
                            'sessionUpdate': 'agent_message_chunk',
                            'content': {
                                'type': 'text',
                                'text': (
                                    'Checked board; workers left and right still running.\\n'
                                    'Run /swarm board (default shortcut ctrl+b) to open the live board.\\n'
                                ),
                            },
                        },
                    ]
                elif mode == 'partial_kill_still_pending':
                    events = [
                        {
                            'sessionUpdate': 'user_message_chunk',
                            'content': {'type': 'text', 'text': body},
                        },
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'call_add_left',
                            'name': 'swarm',
                            'kind': 'other',
                            'status': 'completed',
                            'rawInput': {'action': 'add', 'name': 'left'},
                        },
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'call_add_right',
                            'name': 'swarm',
                            'kind': 'other',
                            'status': 'completed',
                            'rawInput': {'action': 'add', 'name': 'right'},
                        },
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'call_kill_old',
                            'name': 'swarm',
                            'kind': 'other',
                            'status': 'completed',
                            'rawInput': {'action': 'kill', 'name': 'old_worker'},
                        },
                        {
                            'sessionUpdate': 'agent_message_chunk',
                            'content': {
                                'type': 'text',
                                'text': (
                                    'Stopped old_worker; left and right still running.\\n'
                                    'Run /swarm board (default shortcut ctrl+b) to open the live board.\\n'
                                ),
                            },
                        },
                    ]
                elif mode == 'status_unknown_rawoutput':
                    status_ev = {
                        'sessionUpdate': 'tool_call',
                        'toolCallId': 'call_status_unknown',
                        'name': 'swarm',
                        'kind': 'other',
                        'status': 'completed',
                        'rawInput': {'action': 'status'},
                    }
                    raw_spec = os.environ.get('SWARM_TEST_STATUS_OUTPUT', '__OMIT__')
                    if raw_spec != '__OMIT__':
                        status_ev['rawOutput'] = json.loads(raw_spec)
                    events = [
                        {
                            'sessionUpdate': 'user_message_chunk',
                            'content': {'type': 'text', 'text': body},
                        },
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'call_add_left',
                            'name': 'swarm',
                            'kind': 'other',
                            'status': 'completed',
                            'rawInput': {'action': 'add', 'name': 'left'},
                        },
                        status_ev,
                        {
                            'sessionUpdate': 'agent_message_chunk',
                            'content': {
                                'type': 'text',
                                'text': (
                                    'Checked status; waiting on worker.\\n'
                                    'Run /swarm board (default shortcut ctrl+b) to open the live board.\\n'
                                ),
                            },
                        },
                    ]
                else:
                    fg_text = (
                        '已启动 left 处理任务。\\n'
                        if mode == 'terse_wait_no_board_hint'
                        else (
                            'Launched native swarm workers left and right; waiting on board completion.\\n'
                            'Run /swarm board (default shortcut ctrl+b) to open the live board.\\n'
                        )
                    )
                    events = [
                        {
                            'sessionUpdate': 'user_message_chunk',
                            'content': {'type': 'text', 'text': body},
                        },
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'call_add_left',
                            'name': 'swarm',
                            'kind': 'other',
                            'status': 'completed',
                            'rawInput': {'action': 'add', 'name': 'left'},
                        },
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'call_add_right',
                            'name': 'swarm',
                            'kind': 'other',
                            'status': 'completed',
                            'rawInput': {'action': 'add', 'name': 'right'},
                        },
                        {
                            'sessionUpdate': 'agent_message_chunk',
                            'content': {'type': 'text', 'text': fg_text},
                        },
                    ]
            else:
                events = [
                    {
                        'sessionUpdate': 'user_message_chunk',
                        'content': {'type': 'text', 'text': body},
                    },
                ]
                if mode == 'dialogue_redispatch':
                    (root / 'left.txt').write_text('GAMMA')
                    sess['phase'] = 1
                    events.extend([
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'dlg-post-left',
                            'name': 'post_message',
                            'kind': 'other',
                            'status': 'completed',
                            'rawInput': {'text': '@left update output to GAMMA'},
                        },
                        {
                            'sessionUpdate': 'agent_message_chunk',
                            'content': {
                                'type': 'text',
                                'text': (
                                    'Updated left via board mention; waiting for updated deliverable.\\n'
                                    'Run /swarm board (default shortcut ctrl+b) to open the live board.\\n'
                                ),
                            },
                        },
                    ])
                elif mode == 'dialogue_direct_resolve':
                    (root / 'result.txt').write_text('ALPHA|BETA')
                    events.extend([
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'dlg-read-board',
                            'name': 'read_board',
                            'kind': 'other',
                            'status': 'completed',
                        },
                        {
                            'sessionUpdate': 'agent_message_chunk',
                            'content': {'type': 'text', 'text': 'Dialogue resolved: ALPHA|BETA\\n'},
                        },
                    ])
                elif mode == 'dialogue_positive_terminal_status':
                    (root / 'result.txt').write_text('ALPHA|BETA')
                    events.extend([
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'dlg-add-helper',
                            'name': 'swarm',
                            'kind': 'other',
                            'status': 'completed',
                            'rawInput': {'action': 'add', 'name': 'helper'},
                        },
                        {
                            'sessionUpdate': 'tool_call',
                            'toolCallId': 'dlg-status-terminal',
                            'name': 'swarm',
                            'kind': 'other',
                            'status': 'completed',
                            'rawInput': {'action': 'status'},
                            'rawOutput': {'workers': [{'name': 'helper', 'status': 'completed'}]},
                        },
                        {
                            'sessionUpdate': 'agent_message_chunk',
                            'content': {'type': 'text', 'text': 'Dialogue terminal resolved: ALPHA|BETA\\n'},
                        },
                    ])
            for ev in events:
                sess['history'].append(ev)
                if ev.get('sessionUpdate') != 'user_message_chunk':
                    emit_update(sid, ev)
            save_state(st)
            send({'id': msg['id'], 'result': {'stopReason': 'end_turn'}})
        elif method == 'session/cancel':
            (root / 'cancelled').touch()
        elif method == 'session/close':
            sid = msg['params']['sessionId']
            st = load_state()
            if sid in st['sessions']:
                st['sessions'][sid]['closed'] = True
                save_state(st)
            send({'id': msg['id'], 'result': {}})
""")


class SwarmContinuityTests(unittest.TestCase):
    @contextlib.contextmanager
    def continuity_workspace(self, mode='multiline_wait'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agy_bin = root / 'agy'
            agy_bin.write_text(_CONTINUITY_FAKE_AGY.replace('__PYTHON__', sys.executable))
            agy_bin.chmod(0o700)
            with patch.dict(
                os.environ,
                PATH=str(root) + os.pathsep + os.environ['PATH'],
                SWARM_CONTINUITY_DIR=directory,
                SWARM_CONTINUITY_MODE=mode,
            ), patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
               patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
               contextlib.redirect_stdout(io.StringIO()):
                c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                client = c.connect('hermes')['client_id']
                task = c.start(
                    client,
                    agent='agy',
                    model='oas',
                    cwd=directory,
                    prompt='Coordinate workers to create left.txt=ALPHA, right.txt=BETA, then combine into result.txt',
                    write=True,
                )
                thread = threading.Thread(
                    target=run_task,
                    args=(c.store.path, task['id']),
                    kwargs=dict(turn=1, launch_id=task['launch_id'], epoch=1),
                )
                thread.start()
                try:
                    yield root, c, client, task, thread
                finally:
                    if thread.is_alive():
                        c.cancel(client, task['id'])
                        thread.join(5)
                    self.assertFalse(thread.is_alive())

    def _until(self, predicate, msg='Expected state did not arrive'):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(msg)

    def _requests(self, root):
        req_path = root / 'requests.jsonl'
        if not req_path.exists():
            return []
        return [json.loads(line) for line in req_path.read_text().splitlines() if line.strip()]

    def test_multiline_waiting_with_board_hint_keeps_owner_until_background_result(self):
        with self.continuity_workspace('multiline_wait') as (root, c, client, task, thread):
            self._until(
                lambda: c.status(client, task['id']).get('native_observation', {}).get('response_pending_reason') == 'native_handoff_only',
                'Turn 1 did not stay running in native_handoff_only',
            )
            time.sleep(0.15)
            waiting = c.status(client, task['id'])
            self.assertEqual(waiting['state'], 'running')
            self.assertTrue(thread.is_alive())
            self.assertTrue(waiting['native_input']['accepting'])
            self.assertFalse((root / 'result.txt').exists(), 'Turn 1 settled before background swarm wrote result.txt')
            self.assertFalse(any(r.get('method') == 'session/close' for r in self._requests(root)))
            prompt = (root / 'prompt').read_text()
            self.assertIn('Native swarm launch or board status notices', prompt)
            self.assertNotIn('CALLCLI_RESULT', prompt)
            self.assertNotIn('report to @coordinator', prompt)

            (root / 'release_wakeup').touch()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            status = c.status(client, task['id'])
            self.assertEqual(status['state'], 'completed', status.get('settlement_note'))
            self.assertTrue(status['native_cleanup']['session_unloaded'])
            self.assertEqual(status['native_session_id'], '00000000-0000-4000-8000-000000000001')
            self.assertEqual((root / 'result.txt').read_text(), 'ALPHA|BETA')
            self.assertIn('Swarm complete: ALPHA|BETA', status['native_result']['result'])
            requests = self._requests(root)
            self.assertEqual(sum(r.get('method') == 'session/new' for r in requests), 1)
            self.assertEqual(sum(r.get('method') == 'session/close' for r in requests), 1)
            self.assertEqual(c.close(client, task['id'])['state'], 'closed')

    def test_terse_non_english_launch_without_board_hint_keeps_owner(self):
        with self.continuity_workspace('terse_wait_no_board_hint') as (root, c, client, task, thread):
            self._until(
                lambda: c.status(client, task['id']).get('native_observation', {}).get('response_pending_reason') == 'native_handoff_only',
            )
            time.sleep(0.15)
            self.assertEqual(c.status(client, task['id'])['state'], 'running')
            self.assertFalse((root / 'result.txt').exists())
            (root / 'release_wakeup').touch()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            status = c.status(client, task['id'])
            self.assertEqual(status['state'], 'completed', status.get('settlement_note'))
            self.assertEqual(status['native_session_id'], '00000000-0000-4000-8000-000000000001')
            self.assertEqual((root / 'result.txt').read_text(), 'ALPHA|BETA')
            self.assertIn('Swarm complete: ALPHA|BETA', status['native_result']['result'])
            requests = self._requests(root)
            self.assertEqual(sum(r.get('method') == 'session/new' for r in requests), 1)
            self.assertEqual(sum(r.get('method') == 'session/close' for r in requests), 1)
            self.assertEqual(c.close(client, task['id'])['state'], 'closed')

    def test_same_owner_dialogue_redispatch_post_message_keeps_owner_until_wakeup(self):
        with self.continuity_workspace('dialogue_redispatch') as (root, c, client, task, thread):
            self._until(
                lambda: c.status(client, task['id']).get('native_observation', {}).get('response_pending_reason') == 'native_handoff_only',
            )
            c.dialogue(client, task['id'], 'change left to GAMMA', expected_turn=1, command_id='rev-gamma')
            self._until(lambda: c.status(client, task['id'])['commands']['rev-gamma']['status'] == 'returned')
            time.sleep(0.2)
            mid = c.status(client, task['id'])
            self.assertEqual(mid['state'], 'running', mid.get('settlement_note'))
            self.assertEqual(mid['native_session_id'], '00000000-0000-4000-8000-000000000001')
            self.assertEqual(mid['native_observation']['response_pending_reason'], 'native_handoff_only')
            self.assertFalse(any(r.get('method') == 'session/close' for r in self._requests(root)))

            (root / 'release_wakeup').touch()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            final = c.status(client, task['id'])
            self.assertEqual(final['state'], 'completed', final.get('settlement_note'))
            self.assertEqual(final['native_session_id'], '00000000-0000-4000-8000-000000000001')
            self.assertEqual(final['native_result']['interaction_seq'], 1)
            self.assertEqual((root / 'result.txt').read_text(), 'GAMMA|BETA')
            self.assertIn('Swarm complete: GAMMA|BETA', final['native_result']['result'])
            requests = self._requests(root)
            self.assertEqual(sum(r.get('method') == 'session/new' for r in requests), 1)
            self.assertEqual(sum(r.get('method') == 'session/prompt' for r in requests), 2)
            self.assertEqual(sum(r.get('method') == 'session/close' for r in requests), 1)
            self.assertEqual(c.close(client, task['id'])['state'], 'closed')

    def test_same_owner_dialogue_direct_resolve_settles_on_same_session(self):
        for mode, expected_text in (
            ('dialogue_direct_resolve', 'Dialogue resolved: ALPHA|BETA'),
            ('dialogue_positive_terminal_status', 'Dialogue terminal resolved: ALPHA|BETA'),
        ):
            with self.subTest(mode=mode), self.continuity_workspace(mode) as (root, c, client, task, thread):
                self._until(
                    lambda: c.status(client, task['id']).get('native_observation', {}).get('response_pending_reason') == 'native_handoff_only',
                )
                c.dialogue(client, task['id'], 'collect now', expected_turn=1, command_id='resolve-now')
                thread.join(5)
                self.assertFalse(thread.is_alive())
                final = c.status(client, task['id'])
                self.assertEqual(final['state'], 'completed', final.get('settlement_note'))
                self.assertEqual(final['native_session_id'], '00000000-0000-4000-8000-000000000001')
                self.assertEqual(final['native_result']['interaction_seq'], 1)
                self.assertIn(expected_text, final['native_result']['result'])
                self.assertEqual((root / 'result.txt').read_text(), 'ALPHA|BETA')
                requests = self._requests(root)
                self.assertEqual(sum(r.get('method') == 'session/new' for r in requests), 1)
                self.assertEqual(sum(r.get('method') == 'session/prompt' for r in requests), 2)
                self.assertEqual(sum(r.get('method') == 'session/close' for r in requests), 1)
                self.assertEqual(c.close(client, task['id'])['state'], 'closed')

    def test_read_board_partial_kill_and_unknown_status_do_not_resolve_pending_handoff(self):
        cases = [
            ('read_board_still_pending', None),
            ('partial_kill_still_pending', None),
            ('status_unknown_rawoutput', '__OMIT__'),
            ('status_unknown_rawoutput', json.dumps('left: working')),
            ('status_unknown_rawoutput', json.dumps({})),
            ('status_unknown_rawoutput', json.dumps({'workers': []})),
            ('status_unknown_rawoutput', json.dumps({'workers': [{'name': 'left'}]})),
            ('status_unknown_rawoutput', json.dumps({'workers': [{'name': 'left', 'status': 'unknown'}]})),
            ('status_unknown_rawoutput', json.dumps({'workers': [{'name': 'left', 'status': 'waiting'}]})),
        ]
        for mode, raw_spec in cases:
            env_ctx = patch.dict(os.environ, SWARM_TEST_STATUS_OUTPUT=raw_spec) if raw_spec is not None else contextlib.nullcontext()
            with self.subTest(mode=mode, raw_spec=raw_spec), env_ctx, self.continuity_workspace(mode) as (root, c, client, task, thread):
                self._until(
                    lambda: c.status(client, task['id']).get('native_observation', {}).get('response_pending_reason') == 'native_handoff_only',
                    f'{mode} ({raw_spec}) settled prematurely instead of staying running',
                )
                time.sleep(0.15)
                waiting = c.status(client, task['id'])
                self.assertEqual(waiting['state'], 'running')
                self.assertTrue(thread.is_alive())
                self.assertFalse((root / 'result.txt').exists(), f'{mode} ({raw_spec}) wrote result prematurely')
                self.assertFalse(any(r.get('method') == 'session/close' for r in self._requests(root)))

                (root / 'release_wakeup').touch()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                final = c.status(client, task['id'])
                self.assertEqual(final['state'], 'completed', final.get('settlement_note'))
                self.assertEqual(final['native_session_id'], '00000000-0000-4000-8000-000000000001')
                self.assertEqual((root / 'result.txt').read_text(), 'ALPHA|BETA')
                self.assertIn('Swarm complete: ALPHA|BETA', final['native_result']['result'])
                requests = self._requests(root)
                self.assertEqual(sum(r.get('method') == 'session/new' for r in requests), 1)
                self.assertEqual(sum(r.get('method') == 'session/close' for r in requests), 1)
                self.assertEqual(c.close(client, task['id'])['state'], 'closed')

    def test_wakeup_working_status_with_board_hint_waits_then_stale_working_completes(self):
        with self.continuity_workspace('wakeup_working_status_with_board_hint') as (root, c, client, task, thread):
            self._until(
                lambda: c.status(client, task['id']).get('native_observation', {}).get('response_pending_reason') == 'native_handoff_only',
            )
            (root / 'release_wakeup_1').touch()
            self._until(
                lambda: any('right is still working' in e['text'] for e in c.events(client, task['id']) if e['kind'] == 'answer'),
            )
            time.sleep(0.2)
            mid = c.status(client, task['id'])
            self.assertEqual(mid['state'], 'running', mid.get('settlement_note'))
            self.assertTrue(thread.is_alive())
            self.assertFalse((root / 'result.txt').exists())
            self.assertFalse(any(r.get('method') == 'session/close' for r in self._requests(root)))

            (root / 'release_wakeup_2').touch()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            final = c.status(client, task['id'])
            self.assertEqual(final['state'], 'completed', final.get('settlement_note'))
            self.assertEqual(final['native_session_id'], '00000000-0000-4000-8000-000000000001')
            self.assertEqual((root / 'result.txt').read_text(), 'ALPHA|BETA')
            self.assertIn('Swarm complete: ALPHA|BETA', final['native_result']['result'])
            requests = self._requests(root)
            self.assertEqual(sum(r.get('method') == 'session/new' for r in requests), 1)
            self.assertEqual(sum(r.get('method') == 'session/close' for r in requests), 1)
            self.assertEqual(c.close(client, task['id'])['state'], 'closed')

    def test_multihop_wakeup_done_post_and_final_board_mention_settle_cleanly(self):
        with self.continuity_workspace('multihop_done') as (root, c, client, task, thread):
            self._until(
                lambda: c.status(client, task['id']).get('native_observation', {}).get('response_pending_reason') == 'native_handoff_only',
            )
            (root / 'release_wakeup_1').touch()
            self._until(
                lambda: any('Left finished; spawned verifier' in e['text'] for e in c.events(client, task['id']) if e['kind'] == 'answer'),
            )
            time.sleep(0.2)
            mid = c.status(client, task['id'])
            self.assertEqual(mid['state'], 'running', mid.get('settlement_note'))
            self.assertTrue(thread.is_alive())
            self.assertFalse(any(r.get('method') == 'session/close' for r in self._requests(root)))

            (root / 'release_wakeup_2').touch()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            final = c.status(client, task['id'])
            self.assertEqual(final['state'], 'completed', final.get('settlement_note'))
            self.assertTrue(final['native_cleanup']['session_unloaded'])
            self.assertIn('Final verified: ALPHA|BETA.', final['native_result']['result'])
            self.assertIn('Run /swarm board', final['native_result']['result'])
            requests = self._requests(root)
            self.assertEqual(sum(r.get('method') == 'session/new' for r in requests), 1)
            self.assertEqual(sum(r.get('method') == 'session/close' for r in requests), 1)
            self.assertEqual(c.close(client, task['id'])['state'], 'closed')

    def test_stale_native_work_pending_does_not_hang_completion(self):
        with self.continuity_workspace('stale_status_pending') as (root, c, client, task, thread):
            self._until(
                lambda: c.status(client, task['id']).get('native_observation', {}).get('response_pending_reason') == 'native_handoff_only',
            )
            (root / 'release_wakeup_1').touch()
            self._until(
                lambda: any(e['kind'] == 'stage' and 'still working' in e['text'] for e in c.events(client, task['id'])),
            )
            time.sleep(0.15)
            self.assertEqual(c.status(client, task['id'])['state'], 'running')
            (root / 'release_wakeup_2').touch()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            final = c.status(client, task['id'])
            self.assertEqual(final['state'], 'completed', final.get('settlement_note'))
            self.assertIn('Final result without extra status poll', final['native_result']['result'])
            self.assertEqual(c.close(client, task['id'])['state'], 'closed')

    def test_cancel_and_error_after_swarm_dispatch_never_hang_or_replay(self):
        with self.continuity_workspace('cancel_while_waiting') as (root, c, client, task, thread):
            self._until(
                lambda: c.status(client, task['id']).get('native_observation', {}).get('response_pending_reason') == 'native_handoff_only',
            )
            c.cancel(client, task['id'])
            thread.join(5)
            self.assertFalse(thread.is_alive())
            status = c.status(client, task['id'])
            self.assertEqual(status['state'], 'cancelled')
            self.assertTrue(status['native_cancel']['session_unloaded'])
            requests = self._requests(root)
            self.assertEqual(sum(r.get('method') == 'session/cancel' for r in requests), 1)
            self.assertEqual(sum(r.get('method') == 'session/prompt' for r in requests), 1)
            self.assertEqual(sum(r.get('method') == 'session/close' for r in requests), 1)
            self.assertEqual(c.close(client, task['id'])['state'], 'closed')

        with self.continuity_workspace('failed_add') as (root, c, client, task, thread):
            thread.join(5)
            self.assertFalse(thread.is_alive())
            status = c.status(client, task['id'])
            self.assertEqual(status['state'], 'completed', status.get('settlement_note'))
            self.assertIn('Unable to spawn worker left: quota exceeded.', status['native_result']['result'])
            requests = self._requests(root)
            self.assertEqual(sum(r.get('method') == 'session/prompt' for r in requests), 1)
            self.assertEqual(sum(r.get('method') == 'session/close' for r in requests), 1)
            self.assertEqual(c.close(client, task['id'])['state'], 'closed')

        for mode in ('fg_invalid_output', 'bg_fatal'):
            with self.subTest(mode=mode), self.continuity_workspace(mode) as (root, c, client, task, thread):
                if mode == 'bg_fatal':
                    self._until(
                        lambda: c.status(client, task['id']).get('native_observation', {}).get('response_pending_reason') == 'native_handoff_only',
                    )
                    (root / 'release_wakeup').touch()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                status = c.status(client, task['id'])
                self.assertEqual(status['state'], 'unknown', status.get('settlement_note'))
                self.assertNotIn('native_result', status)
                requests = self._requests(root)
                self.assertEqual(sum(r.get('method') == 'session/prompt' for r in requests), 1)
                if mode == 'fg_invalid_output':
                    self.assertEqual(sum(r.get('method') == 'session/close' for r in requests), 1)
                    self.assertEqual(c.close(client, task['id'])['state'], 'closed')
                else:
                    self.assertNotIn('native_cleanup', status)
                    self.assertEqual(c.close(client, task['id'])['state'], 'unknown')
