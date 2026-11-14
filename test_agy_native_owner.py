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

    def test_first_replay_publishes_same_id_terminal_upgrade_once(self):
        for status in ('completed', 'failed'):
            with self.subTest(status=status), patch.dict(os.environ, AGY_NATIVE_TOOL_UPGRADE=status):
                self.exercise_native_owner()

    def test_advertised_families_resolve_with_independent_worker_effort(self):
        with patch.dict(os.environ, AGY_NATIVE_FAMILY_SELECTION='1'):
            self.exercise_native_owner()

    def test_permission_callback_reports_safe_scope_missing_option_and_send_outcome(self):
        from agy_native import NativeOwner, ProtocolError
        for scope, options, reason in [('owned',['allow_once','reject_once'],'selected'),
                ('owned',['allow_always','reject_always'],'allow_once_unavailable'),
                ('child',['allow_once'],'foreign_session')]:
            with self.subTest(scope=scope):
                owner = object.__new__(NativeOwner)
                owner.session_id='owned'; owner.task=dict(agy_approval='approve-all'); owner.permission_decisions=[]
                frame=dict(id=7,method='session/request_permission', params=dict(sessionId=scope,
                    toolCall=dict(toolCallId='tool',kind='execute',title='PRIVATE /secret',rawInput='PRIVATE'),
                    options=[dict(kind=k,optionId=k) for k in options]))
                with patch.object(owner,'send'):
                    if scope=='child':
                        with self.assertRaises(ProtocolError): owner._callback(frame)
                    else: owner._callback(frame)
                diagnostic=owner.permission_decisions[-1]
                self.assertEqual(diagnostic['reason'],reason)
                self.assertEqual(diagnostic['scope'],'root' if scope=='owned' else 'foreign')
                self.assertEqual(diagnostic['rpc_id'],7)
                self.assertNotIn('PRIVATE',json.dumps(diagnostic))
                self.assertIn('response_sent_at',diagnostic)
        owner=object.__new__(NativeOwner)
        owner.session_id='owned'; owner.task=dict(agy_approval='approve-reads'); owner.permission_decisions=[]
        with patch.object(owner,'send',side_effect=OSError('PRIVATE')):
            with self.assertRaisesRegex(ProtocolError,'send failed'):
                owner._callback(dict(id=8,method='session/request_permission',params=dict(sessionId='owned',
                    toolCall=dict(kind='execute'),options=[dict(kind='reject_once',optionId='deny')])))
        self.assertEqual(owner.permission_decisions[-1]['choice'],'reject_once')
        self.assertEqual(owner.permission_decisions[-1]['send_error'],'permission_response_failed')
        self.assertNotIn('PRIVATE',json.dumps(owner.permission_decisions))

    def test_native_close_failure_is_not_replayed_by_cleanup(self):
        from agy_native import NativeOwner, ProtocolError
        owner=object.__new__(NativeOwner)
        owner.session_id='owned'; owner.model_config_value='model'
        with patch.object(owner,'request',side_effect=ProtocolError('uncertain close')) as request:
            with self.assertRaises(ProtocolError): owner.close_session()
            with self.assertRaises(ProtocolError): owner.close_session()
            self.assertEqual(request.call_count,1)

    def test_fresh_setup_cancellation_seals_without_prompt_and_releases(self):
        from agy_native import stage as native_stage
        from bridge import Origin
        def cancel_at_stage(store, task, emit):
            staged=native_stage(store,task,emit)
            store.request_cancel(task['id'],Origin(**task['origin']))
            return staged
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); executable=root/'agy'
            executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            with patch.dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH'],AGY_NATIVE_FIXTURE=str(root)), \
                    patch('updates.DEFAULT_LOCK_ROOT',root/'locks'),patch('updates.ensure_current',return_value={'decision':'fixture'}), \
                    patch('agy_native.stage',side_effect=cancel_at_stage),contextlib.redirect_stdout(io.StringIO()):
                c=Collaboration(root/'tasks.db',launch=lambda task:None); client=c.connect('omp')['client_id']
                task=c.start(client,agent='agy',cwd=directory,prompt='owned',write=True)
                self.assertEqual(run_task(c.store.path,task['id'],turn=1,launch_id=task['launch_id'],epoch=1),0)
                self.assertEqual(c.status(client,task['id'])['state'],'cancelled')
                self.assertEqual(c.close(client,task['id'])['state'],'closed')
                self.assertFalse(any(json.loads(line).get('method')=='session/prompt' for line in (root/'requests.jsonl').read_text().splitlines()))


    def test_native_model_selection_rejects_unadvertised_ambiguous_and_wrong_readback(self):
        from agy_native import NativeOwner, ProtocolError
        metadata = dict(models=dict(availableModels=[dict(modelId='family-medium'), dict(modelId='gemini-4-hydrogen-medium')]),
            configOptions=[dict(id='model', options=[dict(value='family'), dict(value='gemini-4-hydrogen')]),
                           dict(id='thought_level', currentValue='medium', options=[dict(value='medium'), dict(value='low')])])
        cases = [(dict(model='unknown'), metadata), (dict(model='family', effort='low'), metadata),
                 (dict(model='family-medium', effort='low'), metadata),
                 (dict(model='family-extra-medium'), dict(models=dict(availableModels=[dict(modelId='family-extra-medium')]), configOptions=[
                     dict(id='model', options=[dict(value='family'), dict(value='family-extra')]),
                     dict(id='thought_level', currentValue='medium', options=[dict(value='medium'), dict(value='extra-medium')])]))]
        for fields, catalog in cases:
            with self.subTest(fields=fields):
                owner = object.__new__(NativeOwner); owner.task = fields; owner.session_id = 'owned'
                with patch.object(owner, 'request') as request, self.assertRaises(ProtocolError):
                    owner.select(catalog)
                request.assert_not_called()
        owner = object.__new__(NativeOwner); owner.task = dict(model='family'); owner.session_id = 'owned'
        with patch.object(owner, 'request') as request, patch.object(owner, 'load', return_value=({}, [])):
            with self.assertRaisesRegex(ProtocolError, 'readback'):
                owner.select(metadata)
            self.assertEqual([call.args[0] for call in request.call_args_list], ['session/set_config_option']*2)

    def test_later_replay_rechunking_retains_reconciliation_boundary(self):
        from agy_native import additions, snapshot, ProtocolError
        def text(value):
            return dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=value))
        tool = dict(sessionUpdate='tool_call', toolCallId='k', status='pending')
        with self.assertRaises(ProtocolError):
            additions(snapshot([text('Hello '), tool, text('world\n')]), snapshot([text('Hello world\n'), tool]))

    def test_family_resolution_rejects_ambiguous_exact_config_mapping(self):
        from agy_native import NativeOwner, ProtocolError
        owner = object.__new__(NativeOwner)
        owner.task = dict(model='family', effort='extra-medium'); owner.session_id = 'owned'
        metadata = dict(models=dict(availableModels=[dict(modelId='family-extra-medium')]), configOptions=[
            dict(id='model', options=[dict(value='family'), dict(value='family-extra')]),
            dict(id='thought_level', currentValue='medium', options=[dict(value='medium'), dict(value='extra-medium')])])
        with patch.object(owner, 'request') as request, patch.object(owner, 'load', return_value=({}, [])):
            with self.assertRaisesRegex(ProtocolError, 'unambiguous'):
                owner.select(metadata)
            request.assert_not_called()

    def test_hermes_dialogue_answers_questions_and_completes_on_same_owner(self):
        self.exercise_host_dialogue()

    def test_reverse_same_answers_bind_original_question_labels(self):
        self.exercise_host_dialogue(reverse=True)

    def test_corrupted_dialogue_header_is_rejected_without_artifact_or_unload(self):
        from agy_native import NativeOwner
        send = NativeOwner.send
        def corrupt_header(owner, value):
            if value.get('method') == 'session/prompt':
                value = json.loads(json.dumps(value))
                value['params']['prompt'][0]['text'] = value['params']['prompt'][0]['text'].replace('CALLCLI_DIALOGUE ', 'INVALID ', 1)
            return send(owner, value)
        with patch.object(NativeOwner, 'send', corrupt_header):
            self.exercise_host_dialogue(corrupt=True)

    def exercise_host_dialogue(self, reverse=False, corrupt=False):
        from test_plugin import load_plugin, Ctx
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'agy'
            executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            mod = load_plugin(); ctx = Ctx()
            env = {'HERMES_SESSION_ID':'owned', 'HERMES_SESSION_KEY':'owned-key'}
            host = mod.Coordinator(ctx, root, lambda k,d='':env.get(k,d))
            with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'],
                            AGY_NATIVE_FIXTURE=str(root), AGY_NATIVE_DIALOGUE='1', AGY_NATIVE_DIALOGUE_GATE='1'), \
                    patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), \
                    patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                task = host.handle(dict(action='start', agent='agy', cwd=directory, prompt='owned', write=True))['task']
                (root/'task-id').write_text(task['id'])
                store = mod.Store(root/'call-cli/tasks.sqlite3')
                thread = threading.Thread(target=lambda: run_task(store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1))
                thread.start()
                try:
                    deadline = time.monotonic()+6
                    while time.monotonic()<deadline:
                        state = store.get(task['id'])
                        if len(state.get('questions', [])) == 2 or not thread.is_alive():
                            break
                        time.sleep(.02)
                    self.assertTrue(state.get('native_input', {}).get('accepting'), state.get('settlement_note'))
                    self.assertEqual((state['state'], state['turn']), ('running', 1))
                    questions = list(state['questions'])
                    self.assertEqual(len(questions), 2)
                    if reverse:
                        questions.reverse()
                    deadline = time.monotonic()+5
                    while not (root/'dialogue-load-outstanding').exists() and time.monotonic()<deadline:
                        time.sleep(.01)
                    self.assertTrue((root/'dialogue-load-outstanding').exists())
                    self.assertFalse((root/'dialogue-load-returned').exists())
                    for index, question in enumerate(questions, 1):
                        args = dict(action='dialogue', task_id=task['id'], expected_turn=1,
                            command_id='answer'+str(index), question_id=question, prompt='yes' if reverse else 'answer '+str(index))
                        host.handle(args)
                        self.assertEqual(host.handle(args)['task']['commands']['answer'+str(index)]['interaction_seq'], index)
                        if corrupt:
                            (root/'dialogue-load-continue').touch()
                            break
                        if index == 1:
                            self.assertEqual(store.get(task['id'])['commands']['answer1']['status'], 'accepted')
                            self.assertTrue((root/'dialogue-load-outstanding').exists())
                            (root/'dialogue-load-continue').touch()
                            deadline = time.monotonic()+5
                            while not (root/'dialogue-prompt-outstanding').exists() and time.monotonic()<deadline:
                                time.sleep(.01)
                            self.assertTrue((root/'dialogue-prompt-outstanding').exists())
                            self.assertEqual(store.get(task['id'])['commands']['answer1']['status'], 'send_started')
                            self.assertIn(questions[0], store.get(task['id'])['questions'])
                    (root/'dialogue-continue').touch()
                    if corrupt:
                        thread.join(10)
                        self.assertFalse(thread.is_alive())
                        result = store.get(task['id'])
                        self.assertEqual(result['state'], 'unknown')
                        self.assertFalse((root/'revision.txt').exists())
                        self.assertNotIn('native_cleanup', result)
                        self.assertTrue((root/'invalid-dialogue').exists())
                        return
                    thread.join(10)
                    self.assertFalse(thread.is_alive())
                    result = host.handle(dict(action='status', task_id=task['id']))['task']
                    self.assertEqual(result['state'], 'completed', result.get('settlement_note'))
                    self.assertEqual((result['turn'], result['interaction_seq'], result['questions']), (1, 2, []))
                    self.assertEqual(result['native_result']['interaction_seq'], 2)
                    self.assertEqual((root/'revision.txt').read_text(), 'yes;yes' if reverse else 'answer 1;answer 2')
                    mapped = json.loads((root/'answer-map.json').read_text())
                    self.assertEqual([item['label'] for item in mapped], ['Q2','Q1'] if reverse else ['Q1','Q2'])
                    self.assertEqual([item['interaction_seq'] for item in mapped], [0,0])
                    self.assertEqual(len(ctx.calls), 1)
                    requests = [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
                    prompts = [r for r in requests if r.get('method')=='session/prompt']
                    self.assertEqual(len(prompts), 3)
                    self.assertTrue(prompts[0]['params']['prompt'][0]['text'].startswith('/swarm\n'))
                    self.assertTrue(all(not r['params']['prompt'][0]['text'].startswith('/swarm') for r in prompts[1:]))
                    self.assertEqual(len({r['params']['sessionId'] for r in prompts}), 1)
                    self.assertFalse(any(r.get('method')=='session/cancel' for r in requests))
                    self.assertEqual(result['native_cleanup']['workers'], ['worker-a'])
                    self.assertFalse((root/'concurrent-request').exists())
                finally:
                    (root/'dialogue-continue').touch()
                    (root/'dialogue-load-continue').touch()
                    if thread.is_alive():
                        store.request_cancel(task['id'], mod.capture_origin(lambda k,d='':env.get(k,d), 'default'))
                        thread.join(8)

    def test_dialogue_end_turn_publishes_complete_live_question_without_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'agy'
            executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], AGY_NATIVE_FIXTURE=str(root),
                            AGY_NATIVE_DIALOGUE='1', AGY_NATIVE_DIALOGUE_QUESTION='1'), \
                    patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                c = Collaboration(root/'tasks.db', launch=lambda task: None)
                client = c.connect('omp')['client_id']
                task = c.start(client, agent='agy', cwd=directory, prompt='owned')
                (root/'task-id').write_text(task['id'])
                thread = threading.Thread(target=lambda: run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1))
                thread.start()
                try:
                    deadline = time.monotonic()+5
                    while not c.status(client, task['id']).get('questions') and time.monotonic()<deadline:
                        time.sleep(.01)
                    c.dialogue(client, task['id'], 'revise', expected_turn=1, command_id='revise')
                    deadline = time.monotonic()+4
                    while time.monotonic()<deadline:
                        questions = [e for e in c.events(client, task['id']) if e['kind']=='question' and e.get('interaction_seq')==1]
                        if questions:
                            break
                        time.sleep(.01)
                    self.assertEqual([e['text'] for e in questions], ['Q3 Confirm revision?'])
                    self.assertEqual(c.status(client, task['id'])['state'], 'running')
                    all_questions = [e for e in c.events(client, task['id']) if e['kind']=='question']
                    self.assertEqual([e['text'] for e in all_questions if e.get('interaction_seq')==0],
                                     ['Q1 Format?', 'Q2 Destination?'])
                    self.assertEqual(c.status(client, task['id'])['commands']['revise']['status'], 'returned')
                finally:
                    c.cancel(client, task['id']); thread.join(8)

    def test_dialogue_does_not_attribute_old_pending_question_to_new_input(self):
        with patch.dict(os.environ, AGY_NATIVE_OLD_PENDING='1'):
            self.test_dialogue_end_turn_publishes_complete_live_question_without_newline()

    def test_deferred_history_question_recovers_original_interaction_and_settles(self):
        self.exercise_deferred_history()

    def test_deferred_history_blockers_are_safe_on_both_hosts(self):
        for surface in ('hermes', 'omp'):
            with self.subTest(surface=surface):
                self.exercise_deferred_history(surface=surface)

    def test_ambiguous_deferred_history_reports_unknown_with_actionable_blocker(self):
        self.exercise_deferred_history(ambiguous=True)

    def test_invalid_replay_does_not_publish_deferred_question_before_validation(self):
        self.exercise_deferred_history(invalid_replay=True)

    def test_multiple_deferred_interactions_recover_without_replay_duplicates(self):
        self.exercise_deferred_history(multiple=True)

    def test_deferred_disconnect_preserves_actual_cause_not_history_ambiguity(self):
        self.exercise_deferred_history(disconnect=True)

    def test_deferred_growth_without_newline_uses_existing_user_boundary(self):
        self.exercise_deferred_history(boundary='user')

    def test_deferred_growth_without_newline_uses_existing_tool_boundary(self):
        self.exercise_deferred_history(boundary='tool')

    def test_multiple_no_newline_deferred_questions_keep_interaction_and_dedupe(self):
        self.exercise_deferred_history(multiple=True, boundary='user')

    def exercise_deferred_history(self, surface=None, ambiguous=False, invalid_replay=False, multiple=False, disconnect=False, boundary=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'agy'
            executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], AGY_NATIVE_FIXTURE=str(root), AGY_NATIVE_DEFERRED='1'), \
                    patch.dict(os.environ, AGY_NATIVE_DEFERRED_MULTIPLE='1' if multiple else ''), \
                    patch.dict(os.environ, AGY_NATIVE_DEFERRED_BOUNDARY=boundary or ''), \
                    patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                c = Collaboration(root/'tasks.db', launch=lambda task: None)
                client = c.connect('omp')['client_id']
                task = c.start(client, agent='agy', cwd=directory, prompt='owned', write=True)
                (root/'task-id').write_text(task['id'])
                thread = threading.Thread(target=lambda: run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1))
                thread.start()
                def until(predicate):
                    deadline = time.monotonic()+6
                    while time.monotonic()<deadline and thread.is_alive():
                        state = c.status(client, task['id'])
                        if predicate(state):
                            return state
                        time.sleep(.01)
                    self.fail('Expected deferred lifecycle state; got '+c.status(client, task['id'])['state'])
                try:
                    state = until(lambda s: bool(s.get('native_observation', {}).get('question_blockers')))
                    self.assertEqual(state['questions'], [])
                    blocker = state['native_observation']['question_blockers'][0]
                    until(lambda s: any(e.get('notice_kind')=='incomplete_question' for e in c.events(client, task['id'])))
                    notices = [e for e in c.events(client, task['id']) if e.get('notice_kind')=='incomplete_question']
                    self.assertEqual(len(notices), 1)
                    self.assertEqual(notices[0]['interaction_seq'], 0)
                    self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(notices))
                    self.assertEqual(blocker['interaction_seq'], 0)
                    self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(blocker))
                    self.assertEqual(blocker['reason'], 'incomplete_question')
                    self.assertIn('observe', blocker['available_actions'])
                    if surface:
                        if surface == 'hermes':
                            from test_plugin import load_plugin
                            public = load_plugin().Coordinator.public(state)
                        else:
                            from omp_bridge import HostBridge
                            public = HostBridge.public(state)
                        self.assertEqual(public['native_observation']['question_blockers'], [blocker])
                        self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(public))
                    c.dialogue(client, task['id'], 'revise', expected_turn=1, command_id='revise')
                    until(lambda s: len(s['questions'])==1)
                    if boundary:
                        first = until(lambda s: s['commands']['revise']['status']=='returned')
                        observed = first['native_observation']['last_success_at']
                        unchanged = until(lambda s: s['native_observation']['last_success_at']>observed+2)
                        self.assertEqual(len(unchanged['questions']), 1, 'No growth must not publish the old fragment')
                        self.assertTrue(any(b['interaction_seq']==0 for b in unchanged['native_observation']['question_blockers']))
                    if disconnect:
                        (root/'disconnect-deferred').touch()
                        thread.join(8)
                        result = c.status(client, task['id'])
                        self.assertFalse(thread.is_alive())
                        self.assertEqual(result['state'], 'unknown')
                        self.assertIn('disconnected', result['settlement_note'])
                        self.assertEqual(result['native_observation']['question_blockers'][0]['reason'], 'unresolved_question')
                        self.assertNotIn('native_cleanup', result)
                        return
                    if multiple:
                        until(lambda s: any(b['interaction_seq']==1 for b in s['native_observation']['question_blockers']))
                        c.dialogue(client, task['id'], 'revise again', expected_turn=1, command_id='revise2')
                        until(lambda s: len(s['questions'])==2)
                        (root/'recover-1').touch()
                    if ambiguous:
                        (root/'ambiguous-0').touch()
                        thread.join(8)
                        result = c.status(client, task['id'])
                        self.assertFalse(thread.is_alive())
                        self.assertEqual(result['state'], 'unknown')
                        self.assertIn('Deferred question history ambiguous', result['settlement_note'])
                        self.assertEqual(result['native_observation']['question_blockers'][0]['interaction_seq'], 0)
                        self.assertEqual(result['native_observation']['question_blockers'][0]['reason'], 'history_ambiguous')
                        self.assertNotIn('native_cleanup', result)
                        return
                    if invalid_replay:
                        (root/'invalid-replay').touch()
                        thread.join(8)
                        result = c.status(client, task['id'])
                        self.assertEqual(result['state'], 'unknown')
                        self.assertEqual(len([e for e in c.events(client, task['id']) if e['kind']=='question']), 1)
                        return
                    (root/'recover-0').touch()
                    state = until(lambda s: len(s['questions'])==(4 if multiple else 2))
                    observed_at = state['native_observation']['last_success_at']
                    until(lambda s: s['native_observation']['last_success_at']>observed_at+1)
                    events = [e for e in c.events(client, task['id']) if e['kind']=='question']
                    self.assertEqual([e['interaction_seq'] for e in events], [1,2,0,1] if multiple else [1,0])
                    self.assertEqual([e['text'] for e in events],
                        ['Q1 SYNTHETIC_PRIVATE question?']*3+['Q2 SYNTHETIC_PRIVATE question?']
                        if multiple else ['Q1 SYNTHETIC_PRIVATE question?']*2)
                    for index, event in enumerate(reversed(events)):
                        c.dialogue(client, task['id'], 'yes', question_id=event['token'], expected_turn=1, command_id='answer'+str(index))
                    thread.join(8)
                    self.assertFalse(thread.is_alive())
                    result = c.status(client, task['id'])
                    self.assertEqual(result['state'], 'completed', result.get('settlement_note'))
                    self.assertEqual(result['native_observation']['question_blockers'], [])
                    self.assertEqual(result['native_cleanup']['workers'], ['worker-a'])
                    self.assertEqual(len([e for e in c.events(client, task['id']) if e['kind']=='question']), 4 if multiple else 2)
                    self.assertEqual([a[0] for a in json.loads((root/'deferred-answers.json').read_text())], [1,0,2,1] if multiple else [0,1])
                finally:
                    if thread.is_alive():
                        c.cancel(client, task['id']); thread.join(8)

    def test_dialogue_response_loss_marks_unknown_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'agy'
            executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], AGY_NATIVE_FIXTURE=str(root),
                            AGY_NATIVE_DIALOGUE='1', AGY_NATIVE_DIALOGUE_EOF='1'), \
                    patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                c = Collaboration(root/'tasks.db', launch=lambda task: None)
                client = c.connect('omp')['client_id']
                task = c.start(client, agent='agy', cwd=directory, prompt='owned', write=True)
                (root/'task-id').write_text(task['id'])
                thread = threading.Thread(target=lambda: run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1))
                thread.start()
                try:
                    deadline = time.monotonic()+5
                    while not c.status(client, task['id']).get('questions') and thread.is_alive() and time.monotonic()<deadline:
                        time.sleep(.01)
                    c.dialogue(client, task['id'], 'once', expected_turn=1, command_id='once')
                    thread.join(8)
                    self.assertFalse(thread.is_alive())
                    result = c.status(client, task['id'])
                    self.assertEqual((result['state'], result['commands']['once']['status']), ('unknown', 'unknown'))
                    self.assertFalse(result['native_input']['accepting'])
                    self.assertEqual(c.dialogue(client, task['id'], 'once', expected_turn=1, command_id='once')['commands']['once']['status'], 'unknown')
                    requests = [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
                    self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests), 2)
                    self.assertFalse(any(r.get('method')=='session/close' for r in requests))
                finally:
                    if thread.is_alive():
                        c.cancel(client, task['id']); thread.join(8)

    def test_old_interaction_result_cannot_settle_new_dialogue(self):
        from agy_native import completion
        task = dict(id='owned', turn=1, interaction_seq=2, agy_worker_model='gemini-4-hydrogen-medium')
        history = [dict(sessionUpdate='tool_call', toolCallId='add', status='completed',
                        swarm=dict(action='add', name='worker', model=task['agy_worker_model'])),
                   dict(sessionUpdate='tool_call', toolCallId='kill', status='completed', swarm=dict(action='kill', name='worker'))]
        for seq in (None, 0, 1):
            envelope = dict(task='owned', turn=1, status='completed', result='old', workers=['worker'])
            if seq is not None:
                envelope['interaction_seq'] = seq
            message = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_RESULT '+json.dumps(envelope)))
            self.assertIsNone(completion(history+[message], task))


    def test_reused_baseline_tail_growth_reaches_events_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
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
                            AGY_NATIVE_FIXTURE=str(root), AGY_NATIVE_TAIL_GROWTH='1'), \
                    patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), \
                    patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)
            result = c.status(client, task['id'])
            self.assertEqual(result['state'], 'completed', result.get('settlement_note'))
            self.assertEqual(code, 0)
            self.assertEqual(result['native_result']['result'], 'tail result')
            events = c.events(client, task['id'])
            self.assertEqual([e['text'] for e in events if e['kind']=='stage'].count('tail stage'), 1)
            self.assertNotIn('old result', '\n'.join(e['text'] for e in events))

    def run_question_replay(self, foreground, replays):
        # Finite replays end in fixture disconnect: observation evidence, not a reply/continuation loop.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'agy'
            executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            (root/'scenario.json').write_text(json.dumps(dict(foreground=foreground, replays=replays)))
            with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], AGY_NATIVE_FIXTURE=str(root)), \
                    patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), \
                    patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                c = Collaboration(root/'tasks.db', launch=lambda task: None)
                client = c.connect('omp')['client_id']
                task = c.start(client, agent='agy', cwd=directory, prompt='owned', write=True)
                run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)
            requests = [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
            return c.status(client, task['id']), c.events(client, task['id']), requests

    def test_incomplete_replay_question_is_not_flushed_on_disconnect(self):
        text = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q1 Half'))
        result, events, _ = self.run_question_replay([], [[text], [text]])
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual([e for e in events if e['kind']=='question'], [])

    def test_unterminated_background_question_blocks_result_unload(self):
        from agy_native import NativeOwner
        original = NativeOwner.load
        def with_result(owner):
            metadata, history = original(owner)
            if history:
                result = dict(task=owner.task['id'], turn=owner.task['turn'], status='completed', result='candidate', workers=['worker'])
                history = [dict(sessionUpdate='tool_call', toolCallId='add', status='completed',
                    swarm=dict(action='add', name='worker', model='gemini-4-hydrogen-medium')),
                    dict(sessionUpdate='tool_call', toolCallId='kill', status='completed', swarm=dict(action='kill', name='worker')),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_RESULT '+json.dumps(result)+'\n'+history[0]['content']['text']))]
            return metadata, history
        for tail in ('CALLCLI_QUESTION Q1 Half', 'CALLCLI_QUEST'):
            with self.subTest(tail=tail), patch.object(NativeOwner, 'load', with_result):
                text = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=tail))
                result, events, requests = self.run_question_replay([], [[text]])
                self.assertFalse(any(r.get('method')=='session/close' for r in requests))
                self.assertEqual(result['state'], 'unknown')
                self.assertNotIn('native_cleanup', result)
                self.assertEqual([e for e in events if e['kind']=='question'], [])

    def test_foreground_question_retains_owner_for_observation(self):
        question = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q1 Format?'))
        result, events, requests = self.run_question_replay([question], [[question]])
        self.assertEqual([e['text'] for e in events if e['kind']=='question'], ['Q1 Format?'])
        prompt_index = next(i for i, r in enumerate(requests) if r.get('method')=='session/prompt')
        self.assertTrue(any(r.get('method')=='session/load' for r in requests[prompt_index+1:]))
        self.assertEqual(result['state'], 'unknown')

    def test_native_question_boundary_and_identity_across_snapshots(self):
        def text(value, role='agent'):
            return dict(sessionUpdate=role+'_message_chunk', content=dict(type='text', text=value))
        tool = dict(sessionUpdate='tool_call', toolCallId='boundary', title='read_board', status='pending')
        partial = text('CALLCLI_QUESTION Q1 Which')
        complete = text('CALLCLI_QUESTION Q1 Which format?')
        repeated = text('CALLCLI_QUESTION Q1 Which format?\nCALLCLI_QUESTION Q2 Which format?\n')
        result, events, _ = self.run_question_replay([], [[partial], [partial], [complete, tool],
            [complete, tool], [complete, tool, repeated]])
        self.assertEqual([e['text'] for e in events if e['kind']=='question'],
                         ['Q1 Which format?', 'Q2 Which format?'])
        self.assertEqual(len(result['questions']), 2)

    def test_native_user_boundary_completes_unterminated_question(self):
        question = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q1 Format?'))
        user = dict(sessionUpdate='user_message_chunk', content=dict(type='text', text='private user input'))
        _, events, _ = self.run_question_replay([], [[question, user]])
        self.assertEqual([e['text'] for e in events if e['kind']=='question'], ['Q1 Format?'])
        self.assertNotIn('private user input', json.dumps(events))

    def test_historical_tool_upgrade_does_not_end_streaming_question(self):
        tool = dict(sessionUpdate='tool_call', toolCallId='earlier', title='read_board', status='pending')
        partial = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q1 Half'))
        _, events, _ = self.run_question_replay([], [[tool, partial], [dict(tool, status='completed'), partial]])
        self.assertEqual([e for e in events if e['kind']=='question'], [])

    def test_failed_load_preserves_last_success_without_publishing_partial_question(self):
        tool = dict(sessionUpdate='tool_call', toolCallId='owned-tool', title='read_board', kind='read', status='pending')
        partial = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q1 Half\n'))
        result, events, _ = self.run_question_replay([], [[tool], dict(updates=[partial], fail=True)])
        self.assertEqual([e for e in events if e['kind']=='question'], [])
        self.assertEqual(result['native_snapshot'], [tool])
        observation = result['native_observation']
        self.assertEqual(observation['turn'], 1)
        self.assertEqual(observation['pending_tools'], [dict(id='owned-tool', name='read_board', kind='read', status='pending')])
        self.assertEqual(observation['last_success_at'], observation['last_semantic_change_at'])
        self.assertFalse(observation['input_available'])

    def test_pending_question_prevents_native_result_cleanup(self):
        from agy_native import completion
        task = dict(id='owned', turn=1, agy_worker_model='gemini-4-hydrogen-medium', questions=['1:q'])
        history = [dict(sessionUpdate='tool_call', toolCallId='add', status='completed',
                        swarm=dict(action='add', name='worker', model=task['agy_worker_model'])),
                   dict(sessionUpdate='tool_call', toolCallId='kill', status='completed', swarm=dict(action='kill', name='worker')),
                   dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_RESULT '+json.dumps(
                       dict(task='owned', turn=1, status='completed', result='result', workers=['worker']))))]
        self.assertIsNone(completion(history, task))

    def test_first_replay_rejects_rewritten_live_message(self):
        from agy_native import first_replay, ProtocolError
        def text(value):
            return dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=value))
        with self.assertRaises(ProtocolError):
            first_replay([text('original public message')], [text('rewritten public message')])

    def test_first_replay_consumes_published_text_across_tool_blocks_in_order(self):
        from agy_native import first_replay, snapshot, ProtocolError
        def text(value):
            return dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=value))
        tool = dict(sessionUpdate='tool_call', toolCallId='k', title='read_board', status='pending')
        cases = [([text('Hello world')], [text('Hello '), tool, text('world')], [tool]),
                 ([text('Hello '), tool, text('world')], [text('Hello world'), tool], []),
                 ([text('same'), tool, text('same')], [text('samesamesame'), tool], [text('same')])]
        for published, replay, expected in cases:
            with self.subTest(published=published):
                self.assertEqual(first_replay(snapshot(published), snapshot(replay)), expected)
        for published, replay in [([text('first'), tool, text('second')], [text('second'), tool, text('first')]),
                                  ([text('Hello world')], [text('Hello '), tool, text('changed')])]:
            with self.subTest(replay=replay), self.assertRaises(ProtocolError):
                first_replay(snapshot(published), snapshot(replay))

    def test_rechunked_first_replay_settles_and_unloads_native_session(self):
        for mode in ('split', 'merge'):
            with self.subTest(mode=mode), patch.dict(os.environ, AGY_NATIVE_RECHUNK=mode):
                self.exercise_native_owner()

    def test_pending_tool_kind_is_safe_on_both_host_surfaces(self):
        from omp_bridge import HostBridge
        from test_plugin import load_plugin, Ctx
        secret = 'SYNTHETIC_SECRET /private/path'
        tool = dict(sessionUpdate='tool_call', toolCallId='owned-tool', title='read_board', kind='read', status='pending')
        for surface in ('omp', 'hermes'):
            with self.subTest(surface=surface), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                executable = root/'agy'
                executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
                executable.chmod(0o700)
                (root/'scenario.json').write_text(json.dumps(dict(foreground=[tool], replays=[[dict(tool, kind=secret)]])))
                with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], AGY_NATIVE_FIXTURE=str(root)), \
                        patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), \
                        patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    if surface == 'omp':
                        host = dict(session_id='owned-host', agent_id='main')
                        bridge = HostBridge(root/'tasks.db', launch=lambda task: None)
                        key = bridge.bind(host)
                        handle = lambda args: bridge.handle(host, key, args)
                        store = bridge.store
                    else:
                        mod = load_plugin()
                        env = {'HERMES_SESSION_ID':'owned', 'HERMES_SESSION_KEY':'owned-key'}
                        coordinator = mod.Coordinator(Ctx(), root, lambda k,d='':env.get(k,d))
                        handle = coordinator.handle
                        store = mod.Store(root/'call-cli/tasks.sqlite3')
                    task = handle(dict(action='start', agent='agy', cwd=directory, prompt='owned'))['task']
                    run_task(store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)
                    packet = handle(dict(action='status', task_id=task['id']))
                    self.assertNotIn(secret, json.dumps(packet))
                    self.assertEqual(packet['task']['native_observation']['pending_tools'][0]['kind'], '')
                    from bridge import Origin
                    events = store.events(task['id'], Origin(**store.get(task['id'])['origin']))
                    self.assertIn('owned-tool read_board read pending', [e['text'] for e in events if e['kind']=='tool'])

    def test_repeated_history_review_uses_cooldown_without_semantic_progress(self):
        from types import SimpleNamespace
        clock = [0.0]
        def sleep(seconds):
            clock[0] += 31
        tool = dict(sessionUpdate='tool_call', toolCallId='pending', title='read_board', status='pending')
        timer = SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000+clock[0], sleep=sleep)
        with patch('agy_native.time', timer):
            result, events, requests = self.run_question_replay([], [[tool]]*4)
        observation = result['native_observation']
        self.assertGreater(observation['last_success_at'], observation['last_semantic_change_at'])
        self.assertEqual(len([e for e in events if e.get('quiet_notice')]), 3)
        self.assertEqual(len([e for e in events if e['kind']=='tool']), 1)
        self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests), 1)

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
                task = c.start(client, agent='agy', cwd=directory, prompt='owned task', write=True,
                    **(dict(model='gemini-4-carbon', effort='low', agy_worker_model='gemini-4-hydrogen')
                       if os.environ.get('AGY_NATIVE_FAMILY_SELECTION') else {}))
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
                self.assertEqual(result['native_owner_exit']['kind'],'natural')
                self.assertEqual(result['native_owner_exit']['returncode'],0)
                if os.environ.get('AGY_NATIVE_FAMILY_SELECTION'):
                    self.assertEqual(result['model'], 'gemini-4-carbon-low')
                    self.assertEqual(result['agy_worker_model'], 'gemini-4-hydrogen-medium')
                    self.assertEqual(result['native_selection']['requested']['model'], 'gemini-4-carbon')
                    self.assertEqual(result['native_selection']['resolved']['model'], 'gemini-4-carbon-low')
                events = c.events(client, task['id'])
                self.assertEqual([e['text'] for e in events if e['kind']=='answer'].count('same legitimate answer'), 2)
                self.assertEqual(len([e for e in events if e['kind']=='stage' and e['text']=='workers launched']), 1)
                if os.environ.get('AGY_NATIVE_TOOL_UPGRADE'):
                    tools = [e['text'] for e in events if e['kind'] == 'tool' and e['text'].startswith('observed ')]
                    self.assertEqual(tools, ['observed read_board read pending',
                        'observed read_board read '+os.environ['AGY_NATIVE_TOOL_UPGRADE']])
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

    def test_cancel_cleanup_uses_serial_stdio_prompt_and_existing_receipt(self):
        self.exercise_cancel_receipt(cleanup=True)

    def test_background_cancel_requests_exact_roster_cleanup_before_unload(self):
        result, task, calls = self.exercise_cleanup_prompt()
        self.assertEqual(result.state,'cancelled',result.note)
        self.assertEqual(task['native_cleanup']['workers'],['a'])
        self.assertNotIn('native_result',task)
        cleanup=[c for c in calls if c[0]=='cleanup']
        self.assertEqual(len(cleanup),1)
        header=cleanup[0][1].splitlines()[0]
        binding=json.loads(header.removeprefix('CALLCLI_CANCEL_CLEANUP '))
        self.assertEqual(binding,dict(task=task['id'],session_id='session',turn=1,
            launch_id='launch',epoch=1,cancel_id=task['native_cancel_id'],workers=['a']))
        self.assertIsNone(cleanup[0][2])
        self.assertEqual(calls[-2:],[('unload',),('stop',)])

    def test_cleanup_allows_existing_add_update_and_read_title_refresh(self):
        for fault in ('legal-add-update','legal-add-update-after-kill','legal-read-title',
                      'legal-sparse-update','legal-partial-update','legal-no-title-update','legal-refreshed-update'):
            with self.subTest(fault=fault):
                result,task,calls=self.exercise_cleanup_prompt(fault)
                self.assertEqual(result.state,'cancelled',result.note)
                self.assertEqual(task['native_cleanup']['workers'],['a'])

    def test_cleanup_rejects_update_action_worker_model_and_kind_mutations(self):
        for fault in ('update-action','update-worker','update-model','update-kind'):
            with self.subTest(fault=fault):
                result,task,calls=self.exercise_cleanup_prompt(fault)
                self.assertEqual(result.state,'unknown')
                self.assertNotIn('native_cleanup',task)
                self.assertNotIn(('unload',),calls)

    def test_cleanup_rejects_known_swarm_conflicts_without_identity_title(self):
        for field in ('action','worker','model'):
            for display in ('omitted','refreshed'):
                with self.subTest(field=field,display=display):
                    result,task,calls=self.exercise_cleanup_prompt('hidden-'+field+'-'+display)
                    self.assertEqual(result.state,'unknown')
                    self.assertNotIn('native_cleanup',task)
                    self.assertNotIn(('unload',),calls)

    def test_cleanup_rejects_identity_update_before_its_tool_call(self):
        for display in ('omitted','refreshed','swarm'):
            with self.subTest(display=display):
                result,task,calls=self.exercise_cleanup_prompt('orphan-'+display)
                self.assertEqual(result.state,'unknown')
                self.assertNotIn('native_cleanup',task)
                self.assertNotIn(('unload',),calls)

    def test_cancel_evidence_keeps_known_non_swarm_parameters_unclassified(self):
        from agy_native import normalize, snapshot, cancellation_workers
        def event(ident,name,args,kind='tool_call'):
            return normalize(dict(sessionUpdate=kind,toolCallId=ident,name=name,status='completed',rawInput=args))
        add=event('add','swarm',dict(action='add',name='a',model='model'))
        read=event('read','read_file',{})
        update=event('read','Read complete',dict(name='ordinary-parameter'),'tool_call_update')
        kill=event('kill','swarm',dict(action='kill',name='a'))
        history=[add,read,update,kill]
        self.assertEqual(cancellation_workers([add],history,'model'),['a'])
        projected=snapshot(history)
        self.assertNotIn('swarm',projected[1])
        self.assertFalse(any('swarm_update' in entry for entry in projected))

    def test_no_force_exit_observes_natural_eof_and_default_stop_keeps_fallback(self):
        import subprocess
        from unittest.mock import Mock
        from agy_native import NativeOwner
        owner=object.__new__(NativeOwner); owner.poller=Mock()
        owner.proc=subprocess.Popen([sys.executable,'-c','import sys; sys.stdin.read()'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        receipt=owner.stop(force=False)
        self.assertEqual((receipt['kind'],receipt['returncode'],receipt['forced']),('natural',0,False))
        owner.poller=Mock(); owner.proc=Mock(); owner.proc.returncode=-15
        owner.proc.wait.side_effect=[subprocess.TimeoutExpired('fixture',2),None]
        self.assertEqual(owner.stop(),dict(kind='terminate-fallback',returncode=-15))
        owner.proc.terminate.assert_called_once(); owner.proc.kill.assert_not_called()

    def test_cancel_unknown_closes_stdio_without_process_signals(self):
        import subprocess
        from unittest.mock import Mock
        from agy_native import NativeOwner
        owner=object.__new__(NativeOwner)
        owner.poller=Mock(); owner.proc=Mock(); owner.proc.pid=123; owner.proc.returncode=None
        owner.proc.wait.side_effect=subprocess.TimeoutExpired('fixture',2)
        receipt=owner.stop(force=False)
        owner.proc.stdin.close.assert_called_once()
        owner.proc.stdout.close.assert_called_once()
        owner.proc.terminate.assert_not_called(); owner.proc.kill.assert_not_called()
        self.assertEqual(receipt,dict(kind='stdio-closed-exit-unverified',returncode=None,pid=123,
            forced=False,recovery='unsupported-exit-unproven-no-owner',worker_fate='unknown'))

    def test_missing_cancel_kill_selects_no_force_exit_without_claiming_live_owner(self):
        result,task,calls=self.exercise_cleanup_prompt('missing')
        self.assertEqual(result.state,'unknown')
        self.assertIn(('stop-no-force',),calls)
        self.assertNotIn(('stop',),calls)

    def test_cleanup_timeout_disconnect_and_send_failure_do_not_retry_or_unload(self):
        for fault in ('timeout','disconnect','send-error','post-load-error'):
            with self.subTest(fault=fault):
                result,task,calls=self.exercise_cleanup_prompt(fault)
                self.assertEqual(result.state,'unknown')
                self.assertEqual(task['native_cancel_cleanup']['status'],'returned' if fault=='post-load-error' else 'unknown')
                self.assertEqual(len([c for c in calls if c[0]=='cleanup']),1)
                self.assertNotIn(('unload',),calls)
                self.assertNotIn('native_cleanup',task)

    def test_duplicate_cancel_during_cleanup_seals_dialogue_without_cancelling_cleanup(self):
        result,task,calls=self.exercise_cleanup_prompt('duplicate')
        self.assertEqual(result.state,'cancelled',result.note)
        self.assertEqual(len([c for c in calls if c[0]=='cleanup']),1)
        self.assertEqual(task['commands']['queued']['status'],'not_sent')
        self.assertIn(('dialogue-rejected',),calls)

    def test_cleanup_permission_refusal_cannot_release_even_with_kill_record(self):
        result,task,calls=self.exercise_cleanup_prompt('denied')
        self.assertEqual(result.state,'unknown')
        self.assertNotIn(('unload',),calls)
        self.assertNotIn('native_cleanup',task)

    def test_cleanup_new_business_tool_cannot_release_with_matching_kills(self):
        result,task,calls=self.exercise_cleanup_prompt('new-business')
        self.assertEqual(result.state,'unknown')
        self.assertNotIn(('unload',),calls)
        self.assertNotIn('native_cleanup',task)

    def test_cleanup_late_worker_readd_and_missing_kill_retain_unloaded_scene(self):
        for fault in ('late-worker','new-worker','readd','same-id-readd','missing','active-tool','foreground'):
            with self.subTest(fault=fault):
                result,task,calls=self.exercise_cleanup_prompt(fault)
                self.assertEqual(result.state,'unknown')
                self.assertNotIn(('unload',),calls)
                self.assertNotIn('native_cleanup',task)
                if fault in {'late-worker','active-tool','foreground'}:
                    self.assertFalse(any(c[0]=='cleanup' for c in calls))

    def test_live_tool_replay_status_and_kill_reaches_trusted_cancellation(self):
        replay=json.loads((Path(__file__).parent/'fixtures/agy_cancel_status_replay.json').read_text())
        result,task,calls=self.exercise_cleanup_prompt(replay=replay)
        self.assertEqual(result.state,'cancelled',result.note)
        self.assertEqual(task['native_cleanup']['workers'],['cancel_echo'])
        self.assertEqual(task['native_cleanup']['kind'],'verified-cancellation')
        self.assertEqual(calls[-2:],[('unload',),('stop',)])
        self.assertNotIn('native_result',task)

    def test_cleanup_status_requires_native_identity_and_successful_kill(self):
        original=json.loads((Path(__file__).parent/'fixtures/agy_cancel_status_replay.json').read_text())
        for fault in ('wrong-title','missing-action','wrong-action','execute-kind','failed-status',
                      'foreign-worker','model-argument','bad-id','failed-kill','new-worker','new-business',
                      'status-update-foreign-worker'):
            with self.subTest(fault=fault):
                replay=json.loads(json.dumps(original))
                kill,status=replay['cleanup']
                if fault=='wrong-title':status['title']='read_board'
                elif fault=='missing-action':status.pop('swarm')
                elif fault=='wrong-action':status['swarm']['action']='status '
                elif fault=='execute-kind':status['kind']='execute'
                elif fault=='failed-status':status['status']='failed'
                elif fault=='foreign-worker':status['swarm']['name']='other-worker'
                elif fault=='model-argument':status['swarm']['model']='other-model'
                elif fault=='bad-id':status['toolCallId']='invalid id'
                elif fault=='failed-kill':kill['status']='failed'
                elif fault=='new-worker':replay['cleanup'].append(dict(kill,toolCallId='new-add',swarm=dict(action='add',name='other',model='gemini-4-hydrogen-medium')))
                elif fault=='new-business':replay['cleanup'].append(dict(status,toolCallId='execute',title='execute',kind='execute',swarm={}))
                elif fault=='status-update-foreign-worker':
                    replay['cleanup'].append(dict(status,tool_event='tool_call_update',swarm=dict(action='status',name='other-worker')))
                result,task,calls=self.exercise_cleanup_prompt(replay=replay)
                self.assertEqual(result.state,'unknown')
                self.assertNotIn('native_cleanup',task)
                self.assertNotIn(('unload',),calls)

    def test_cleanup_native_status_for_known_worker_is_read_only(self):
        replay=json.loads((Path(__file__).parent/'fixtures/agy_cancel_status_replay.json').read_text())
        replay['cleanup'][1]['swarm']['name']='cancel_echo'
        result,task,calls=self.exercise_cleanup_prompt(replay=replay)
        self.assertEqual(result.state,'cancelled',result.note)
        self.assertEqual(task['native_cleanup']['workers'],['cancel_echo'])

    def test_cleanup_rejects_malformed_status_args_before_projection(self):
        from agy_native import normalize
        original=json.loads((Path(__file__).parent/'fixtures/agy_cancel_status_replay.json').read_text())
        for phase in ('call','update','untitled-update','sparse-update'):
            for field,value in (('name',['SYNTHETIC_STATUS_SECRET']),('model',123),
                                ('command','SYNTHETIC_STATUS_SECRET')):
                with self.subTest(phase=phase,field=field):
                    replay=json.loads(json.dumps(original))
                    status=replay['cleanup'][1]
                    payload=dict(sessionUpdate='tool_call' if phase=='call' else 'tool_call_update',
                        toolCallId=status['toolCallId'],kind='other',status='completed',
                        rawInput=dict(action='status',**{field:value}))
                    if phase=='sparse-update':payload['rawInput'].pop('action')
                    if phase in {'call','update'}:payload['name']='swarm'
                    entry=normalize(payload)
                    if phase=='call':replay['cleanup'][1]=entry
                    else:replay['cleanup'].append(entry)
                    result,task,calls=self.exercise_cleanup_prompt(replay=replay)
                    self.assertEqual(result.state,'unknown')
                    self.assertNotIn('native_cleanup',task)
                    self.assertNotIn(('unload',),calls)
                    self.assertNotIn('SYNTHETIC_STATUS_SECRET',json.dumps(dict(entry=entry,task=task)))

    def test_normal_snapshot_does_not_enforce_cancel_status_argument_contract(self):
        from agy_native import normalize, snapshot
        status=normalize(dict(sessionUpdate='tool_call',toolCallId='status',name='swarm',
            kind='other',status='completed',rawInput=dict(action='status',command='SYNTHETIC_STATUS_SECRET')))
        history=snapshot([status])
        self.assertEqual(history,[dict(sessionUpdate='tool_call',toolCallId='status',title='swarm',
            kind='other',status='completed',swarm=dict(action='status'))])
        self.assertNotIn('SYNTHETIC_STATUS_SECRET',json.dumps(history))

    def test_cleanup_status_sparse_updates_and_ordinary_parameters_remain_valid(self):
        from agy_native import normalize, snapshot
        replay=json.loads((Path(__file__).parent/'fixtures/agy_cancel_status_replay.json').read_text())
        ident=replay['cleanup'][1]['toolCallId']
        for args in (None,{},dict(name='cancel_echo')):
            payload=dict(sessionUpdate='tool_call_update',toolCallId=ident,status='completed')
            if args is not None:payload['rawInput']=args
            replay['cleanup'].append(normalize(payload))
        result,task,calls=self.exercise_cleanup_prompt(replay=replay)
        self.assertEqual(result.state,'cancelled',result.note)
        ordinary=[normalize(dict(sessionUpdate='tool_call',toolCallId='read',name='read_file',status='completed')),
            normalize(dict(sessionUpdate='tool_call_update',toolCallId='read',rawInput=dict(command='SYNTHETIC_STATUS_SECRET')))]
        self.assertNotIn('SYNTHETIC_STATUS_SECRET',json.dumps(snapshot(ordinary)))
        self.assertNotIn('swarm',snapshot(ordinary)[0])
        self.assertFalse(any('status_args_invalid' in entry for entry in snapshot(ordinary)))

    def exercise_cleanup_prompt(self, fault=None, *, replay=None):
        import agy_native as native
        from bridge import Store, Origin
        model='gemini-4-hydrogen-medium'
        def tool(ident,action,name='a',status='completed'):
            return dict(sessionUpdate='tool_call',toolCallId=ident,title='swarm',status=status,
                swarm=dict(action=action,name=name,**(dict(model=model) if action=='add' else {})))
        history=[tool('add-a','add')]; calls=[]
        if replay is not None:history=json.loads(json.dumps(replay['before']))
        if fault=='legal-read-title':history.append(dict(sessionUpdate='tool_call',toolCallId='read-a',title='Read',kind='read',status='completed'))
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'tasks.db'); origin=Origin('fixture','fixture','fixture')
            task=store.create(origin,'local','agy',directory,'Do not remove the worker until operator cancels',True)
            task=store.update(task['id'],state='running',launch_id='launch',runner_pid=1,
                agy_swarm='native',version_check={},agy_worker_model=model)
            class Endpoint:
                def __init__(self,task):
                    self.task=task; self.session_id=None; self.created_session=False
                    self.permission_decisions=[]; self.prompt_returned=False; self.request_cancel_sent=False; self.loads=0
                def initialize(self):return {}
                def new(self):self.session_id='session'; self.created_session=True; return {}
                def select(self,metadata):return dict(agy_worker_model=model)
                def load(self):
                    self.loads+=1
                    if fault=='post-load-error' and any(c[0]=='cleanup' for c in calls):
                        raise native.ProtocolError('Cleanup replay disconnected; effects unknown')
                    self.last_load_updates=[] if self.loads<=2 else list(history)
                    if fault=='late-worker' and self.loads>=3:
                        self.last_load_updates.append(tool('add-b','add','b'))
                    if self.loads==3 and fault!='foreground':
                        store.dialogue(task['id'],origin,'ordinary revision',expected_turn=1,command_id='queued')
                        store.request_cancel(task['id'],origin)
                    return {},native.snapshot(self.last_load_updates)
                def request(self,method,params,**kw):
                    body=params['prompt'][0]['text']
                    if not self.prompt_returned:
                        for entry in history:kw['on_update'](entry)
                        self.prompt_returned=True
                        if fault=='active-tool':history.append(dict(sessionUpdate='tool_call',toolCallId='busy',status='in_progress'))
                        if fault=='foreground':
                            self.request_cancel_sent=True
                            store.request_cancel(task['id'],origin)
                    else:
                        calls.append(('cleanup',body,kw.get('check_cancel')))
                        if fault in {'timeout','disconnect'}:raise native.ProtocolError('cleanup '+fault+'; effects unknown')
                        if fault=='send-error':raise BrokenPipeError('synthetic send failure')
                        if fault=='duplicate':
                            old=store.get(task['id'])['native_cancel_id']
                            store.request_cancel(task['id'],origin)
                            assert store.native_cancel_seal(task['id'],1,'launch',1)['native_cancel_id']==old
                            try:store.dialogue(task['id'],origin,'late ordinary input',expected_turn=1,command_id='late')
                            except ValueError:calls.append(('dialogue-rejected',))
                        if fault=='denied':self.permission_decisions.append(dict(allowed=False,reason='selected',choice='reject_once'))
                        if fault=='same-id-readd':history.append(tool('add-a','add'))
                        if fault=='legal-add-update':history.append(native.normalize(dict(sessionUpdate='tool_call_update',toolCallId='add-a',name='swarm',status='completed',rawInput=dict(action='add',name='a',model=model))))
                        if fault=='legal-sparse-update':history.append(native.normalize(dict(sessionUpdate='tool_call_update',toolCallId='add-a',name='swarm',status='completed')))
                        if fault=='legal-partial-update':history.append(native.normalize(dict(sessionUpdate='tool_call_update',toolCallId='add-a',name='swarm',status='completed',rawInput=dict(action='add'))))
                        if fault=='legal-no-title-update':history.append(native.normalize(dict(sessionUpdate='tool_call_update',toolCallId='add-a',status='completed')))
                        if fault=='legal-refreshed-update':history.append(native.normalize(dict(sessionUpdate='tool_call_update',toolCallId='add-a',title='Swarm complete',status='completed',rawInput=dict(name='a'))))
                        if fault=='legal-read-title':history[1]=dict(history[1],title='Read complete')
                        if fault and fault.startswith('orphan-'):
                            update=dict(sessionUpdate='tool_call_update',toolCallId='kill-a',status='completed',
                                rawInput=dict(action='kill',name='other-worker'))
                            if fault!='orphan-omitted':update['title']='swarm' if fault=='orphan-swarm' else 'Swarm complete'
                            history.append(native.normalize(update))
                        if replay is not None:history.extend(json.loads(json.dumps(replay['cleanup'])))
                        elif fault!='missing':history.append(tool('kill-a','kill'))
                        if fault=='legal-add-update-after-kill':history.append(native.normalize(dict(sessionUpdate='tool_call_update',toolCallId='add-a',name='swarm',status='completed',rawInput=dict(action='add',name='a',model=model))))
                        if fault in {'update-action','update-worker','update-model','update-kind'}:
                            args=dict(action='add',name='a',model=model)
                            if fault=='update-action':args['action']='kill'
                            if fault=='update-worker':args['name']='other'
                            if fault=='update-model':args['model']='other-model'
                            history.append(native.normalize(dict(sessionUpdate='tool_call_update',toolCallId='add-a',name='swarm',
                                kind='execute' if fault=='update-kind' else 'other',status='completed',rawInput=args)))
                        if fault and fault.startswith('hidden-'):
                            _,field,display=fault.split('-')
                            key={'action':'action','worker':'name','model':'model'}[field]
                            value={'action':'kill','worker':'other-worker','model':'other-model'}[field]
                            update=dict(sessionUpdate='tool_call_update',toolCallId='add-a',status='completed',rawInput={key:value})
                            if display=='refreshed':update['title']='Swarm complete'
                            history.append(native.normalize(update))
                        if fault=='new-business':history.append(dict(sessionUpdate='tool_call',toolCallId='business',title='execute',kind='execute',status='completed'))
                        if fault=='new-worker':history.extend([tool('add-b','add','b'),tool('kill-b','kill','b')])
                        if fault=='readd':history.append(tool('add-a','add'))
                    return dict(stopReason='end_turn'),[]
                def notify_cancel(self):calls.append(('cancel',))
                def close_session(self):calls.append(('unload',)); return True
                def stop(self, *, force=True):
                    calls.append(('stop',) if force else ('stop-no-force',))
                    return dict(kind='natural',returncode=0)
            with patch.object(native,'NativeOwner',Endpoint),patch.object(native,'device_lock',lambda t:contextlib.nullcontext()), \
                    patch.object(native,'stage',lambda s,t,e:t):
                result=native.run_native(store,task,lambda *a,**kw:None,'')
            return result,store.get(task['id']),calls

    def test_verified_foreground_and_background_cancel_release_without_business_result(self):
        for foreground in (True, False):
            with self.subTest(foreground=foreground):
                self.exercise_cancel_receipt(foreground)

    def test_dialogue_cancel_verified_history_releases_without_answering_question(self):
        self.exercise_cancel_receipt(dialogue=True)

    def test_unverified_cancellation_keeps_hold_for_failed_missing_or_new_workers(self):
        for fault in ('failed','missing','new_worker','dropped_worker','readd','load_error'):
            with self.subTest(fault=fault):
                self.exercise_cancel_receipt(fault=fault)

    def test_trusted_native_cancel_receipt_reconciles_unknown_without_new_owner(self):
        self.exercise_cancel_receipt(reconcile=True)

    def test_cancel_during_outstanding_load_cannot_admit_new_workers(self):
        result, task = self.exercise_cancel_observation('new-worker')
        self.assertEqual(result.state, 'unknown')
        self.assertNotIn('native_cleanup', task)

    def test_cancel_during_load_allows_known_worker_status_progress(self):
        result, task = self.exercise_cancel_observation('status-only')
        self.assertEqual(result.state, 'cancelled', result.note)
        self.assertEqual(task['native_cleanup']['workers'], ['a'])

    def test_cancel_during_later_load_cannot_expand_last_verified_roster(self):
        result, task = self.exercise_cancel_observation('later-new-worker')
        self.assertEqual(result.state,'unknown')
        self.assertNotIn('native_cleanup',task)

    def test_cancel_during_later_load_keeps_status_updates_for_known_workers(self):
        result, task = self.exercise_cancel_observation('later-status-only')
        self.assertEqual(result.state,'cancelled',result.note)
        self.assertEqual(task['native_cleanup']['workers'],['a'])

    def test_first_cancel_replay_accepts_user_echo_and_strict_live_alignment(self):
        result, task = self.exercise_cancel_observation('foreground-echo')
        self.assertEqual(result.state,'cancelled',result.note)
        self.assertEqual(task['native_cleanup']['workers'],['a'])
        self.assertEqual((task['state'],task['runner_pid']),('cancelled',0))

    def test_first_cancel_replay_rejects_rewritten_live_message(self):
        result, task = self.exercise_cancel_observation('foreground-rewrite')
        self.assertEqual(result.state,'unknown')
        self.assertNotIn('native_cleanup',task)

    def test_cancel_rejects_same_tool_id_readd_after_completed_kill(self):
        from agy_native import cancellation_workers, ProtocolError
        def tool(ident,action,status='completed'):
            return dict(sessionUpdate='tool_call',toolCallId=ident,title='swarm',status=status,
                swarm=dict(action=action,name='worker',**(dict(model='model') if action=='add' else {})))
        add=tool('add','add'); kill=tool('kill','kill')
        with self.assertRaisesRegex(ProtocolError,'generation|re-add'):
            cancellation_workers([add],[add,kill,add],'model')
        self.assertEqual(cancellation_workers([tool('add','add','pending')],
            [tool('add','add','pending'),add,kill],'model'),['worker'])

    def exercise_cancel_observation(self, mode):
        import agy_native as native
        from bridge import Store, Origin
        model='gemini-4-hydrogen-medium'
        def tool(ident, action, name, status='completed'):
            return dict(sessionUpdate='tool_call',toolCallId=ident,title='swarm',status=status,
                swarm=dict(action=action,name=name,**(dict(model=model) if action=='add' else {})))
        live=[tool('add-a','add','a','pending' if mode.endswith('status-only') else 'completed')]
        final=[tool('add-a','add','a'),tool('kill-a','kill','a')]
        if mode.endswith('new-worker'): final += [tool('add-b','add','b'),tool('kill-b','kill','b')]
        if mode.startswith('foreground-'):
            live.append(dict(sessionUpdate='agent_message_chunk',content=dict(type='text',text='original public message\n')))
            final = [dict(sessionUpdate='user_message_chunk',content=dict(type='text',text='prompt echo')),
                final[0],dict(sessionUpdate='agent_message_chunk',content=dict(type='text',text=
                    'rewritten public message\n' if mode=='foreground-rewrite' else 'original public message\n')),final[1]]
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'tasks.db'); origin=Origin('fixture','fixture','fixture')
            task=store.create(origin,'local','agy',directory,'synthetic',True)
            task=store.update(task['id'],state='running',launch_id='launch',runner_pid=1,
                agy_swarm='native',version_check={},agy_worker_model=model)
            class Endpoint:
                def __init__(self,task):
                    self.task=task; self.session_id=None; self.created_session=False
                    self.permission_decisions=[]; self.prompt_returned=False; self.request_cancel_sent=False; self.loads=0
                def initialize(self): return {}
                def new(self): self.session_id='session'; self.created_session=True; return {}
                def select(self,metadata): return dict(agy_worker_model=model)
                def load(self):
                    self.loads+=1
                    history=[] if self.loads<=2 else live if mode.startswith('later-') and self.loads==3 else final
                    if self.loads==(4 if mode.startswith('later-') else 3) and not mode.startswith('foreground-'):
                        store.request_cancel(task['id'],origin)
                    self.last_load_updates=history
                    return {},native.snapshot(history)
                def request(self,*args,**kwargs):
                    for entry in live: kwargs['on_update'](entry)
                    self.prompt_returned=True
                    if mode.startswith('foreground-'): store.request_cancel(task['id'],origin)
                    return dict(stopReason='end_turn'),live
                def notify_cancel(self): pass
                def close_session(self): return True
                def stop(self, *, force=True): return dict(kind='natural',returncode=0)
            with patch.object(native,'NativeOwner',Endpoint), patch.object(native,'device_lock',lambda t:contextlib.nullcontext()), \
                    patch.object(native,'stage',lambda s,t,e:t):
                result=native.run_native(store,task,lambda *a,**kw:None,'')
            return result,store.get(task['id'])

    def test_fresh_preprompt_model_failure_releases_but_reused_failure_holds(self):
        for reused in (False, True):
            with self.subTest(reused=reused), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                executable = root/'agy'
                executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
                executable.chmod(0o700)
                with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], AGY_NATIVE_FIXTURE=str(root)), \
                        patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    c = Collaboration(root/'tasks.db', launch=lambda task: None)
                    client = c.connect('omp')['client_id']
                    task = c.start(client, agent='agy', cwd=directory, prompt='owned', model='unadvertised', write=True)
                    if reused: c.store.update(task['id'], native_session_id='11111111-1111-4111-8111-111111111111')
                    self.assertEqual(run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1), 1)
                    before = c.status(client, task['id'])
                    self.assertEqual(before['state'], 'unknown')
                    self.assertIn('not advertised', before['settlement_note'])
                    observation=before['native_unload_observation']
                    self.assertEqual((observation['task'],observation['session_id'],observation['turn'],observation['launch_id'],observation['epoch']),
                        (task['id'],before['native_session_id'],1,task['launch_id'],1))
                    self.assertEqual((observation['attempted'],observation['verified'],observation['reason_code']),
                        (True,True,'unload_verified'))
                    if reused:self.assertNotIn('native_cleanup',before)
                    self.assertEqual(c.close(client, task['id'])['state'], 'unknown' if reused else 'closed')
                    self.assertFalse(any(json.loads(r).get('method')=='session/prompt' for r in (root/'requests.jsonl').read_text().splitlines()))

    def test_forged_native_cleanup_task_fields_cannot_release(self):
        from bridge import Origin
        from release import recover_native
        with tempfile.TemporaryDirectory() as directory:
            c = Collaboration(Path(directory)/'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']
            task = c.start(client, agent='agy', cwd=directory, prompt='owned', write=True)
            for kind in ('verified-cancellation','fresh-no-prompt'):
                c.store.update(task['id'], state='unknown', native_session_id='forged-session', runner_pid=0,
                    native_cancel_id='fake', native_cleanup=dict(kind=kind, task=task['id'], turn=1,
                        launch_id=task['launch_id'], epoch=1, session_id='forged-session', cancel_id='fake',
                        workers=[], no_prompt=True, session_unloaded=True))
                self.assertEqual(recover_native(c.store, Origin(**task['origin']),task['id'],1,'reconcile')['state'], 'unknown')
            c.store.update(task['id'], state='cancelled', native_cleanup=dict(turn=1,workers=[],no_prompt=True,session_unloaded=True))
            self.assertEqual(c.close(client,task['id'])['state'],'unknown')
            c.store.update(task['id'],state='cancelled',session_creation_attempted=True,native_result={'turn':1},
                native_cleanup=dict(turn=1,workers=['worker'],session_unloaded=True,
                    evidence='native-add-kill-completed-and-session-unload'))
            self.assertEqual(c.close(client,task['id'])['state'],'unknown')


    def exercise_cancel_receipt(self, foreground=False, fault=None, reconcile=False, dialogue=False, cleanup=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'agy'
            executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            history = [dict(sessionUpdate='tool_call', toolCallId=action, name='swarm', status='completed',
                args=dict(action=action, name='worker', **(dict(model='gemini-4-hydrogen-medium') if action=='add' else {})))
                for action in ('add','kill')]
            if fault == 'failed': history[1]['status'] = 'failed'
            if fault == 'missing': history = history[:1]
            final = list(history)
            if fault == 'new_worker':
                final += [dict(sessionUpdate='tool_call', toolCallId='new', name='swarm', status='completed',
                    args=dict(action='add', name='new-worker', model='gemini-4-hydrogen-medium'))]
            if fault == 'dropped_worker': final = []
            if fault == 'readd':
                final += [dict(sessionUpdate='tool_call', toolCallId='readd', name='swarm', status='completed',
                    args=dict(action='add', name='worker', model='gemini-4-hydrogen-medium'))]
            scenario=dict(foreground=history,replays=[history]*20,cancel_replay=final,cancel_error=fault=='load_error')
            if cleanup:
                scenario.update(foreground=history[:1],replays=[history[:1]]*20,cancel_replay=history[:1],cleanup_replay=history)
            (root/'scenario.json').write_text(json.dumps(scenario))
            if dialogue:
                (root/'dialogue-block').touch()
            with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], AGY_NATIVE_FIXTURE=str(root),
                            AGY_NATIVE_BLOCK='1' if foreground else ''), patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), \
                    patch('updates.ensure_current', return_value={'decision':'fixture'}), contextlib.redirect_stdout(io.StringIO()) as output:
                c = Collaboration(root/'tasks.db', launch=lambda task: None)
                client = c.connect('omp')['client_id']
                task = c.start(client, agent='agy', cwd=directory, prompt='cancel fixture', write=True)
                thread = threading.Thread(target=lambda: run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1))
                thread.start()
                deadline = time.monotonic()+5
                while thread.is_alive() and time.monotonic()<deadline:
                    current = c.status(client, task['id'])
                    if (foreground and (root/'prompt').exists()) or (not foreground and current.get('native_snapshot')): break
                    time.sleep(.01)
                if dialogue:
                    c.dialogue(client,task['id'],'inflight',expected_turn=1,command_id='inflight')
                    deadline=time.monotonic()+5
                    while not (root/'dialogue-blocked').exists() and thread.is_alive() and time.monotonic()<deadline:
                        time.sleep(.01)
                    self.assertTrue((root/'dialogue-blocked').exists())
                c.cancel(client, task['id']); thread.join(8)
                self.assertFalse(thread.is_alive())
                result = c.status(client, task['id'])
                self.assertNotIn('native_result', result)
                if fault:
                    self.assertEqual(result['state'], 'unknown')
                    self.assertNotIn('native_unload_observation',result)
                    self.assertEqual(c.close(client, task['id'])['state'], 'unknown')
                    with self.assertRaises(ValueError): c.start(client, agent='omp', cwd=directory, prompt='writer', write=True)
                    return
                self.assertEqual(result['state'], 'cancelled', result.get('settlement_note'))
                if cleanup:
                    requests=[json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
                    prompts=[r for r in requests if r.get('method')=='session/prompt']
                    self.assertEqual(len(prompts),2)
                    self.assertTrue(prompts[1]['params']['prompt'][0]['text'].startswith('CALLCLI_CANCEL_CLEANUP '))
                    self.assertEqual(sum(r.get('method')=='session/cancel' for r in requests),1)
                self.assertTrue(result['native_unload_observation']['verified'])
                self.assertEqual(result['native_unload_observation']['context'],'cancellation')
                self.assertEqual(len([e for e in c.events(client,task['id']) if e['kind']=='cancelled']),1)
                self.assertEqual(len([json.loads(line) for line in output.getvalue().splitlines()
                    if json.loads(line).get('event')=='cancelled']),1)
                receipt = result['native_cleanup']
                self.assertEqual((receipt['task'], receipt['session_id'], receipt['turn'], receipt['launch_id'], receipt['epoch']),
                    (task['id'], result['native_session_id'], 1, task['launch_id'], 1))
                self.assertEqual(receipt['kind'], 'verified-cancellation')
                self.assertTrue(receipt['session_unloaded'])
                self.assertFalse(receipt['all_descendants_proven_stopped'])
                if reconcile:
                    from bridge import Origin
                    from release import recover_native
                    c.store.update(task['id'], state='unknown')
                    requests_before = (root/'requests.jsonl').read_text()
                    recovered = recover_native(c.store, Origin(**task['origin']), task['id'], 1, 'reconcile')
                    self.assertEqual(recovered['state'], 'released')
                    self.assertEqual((root/'requests.jsonl').read_text(), requests_before)
                    self.assertEqual(recover_native(c.store, Origin(**task['origin']), task['id'], 1, 'reconcile')['state'], 'released')
                    return
                self.assertEqual(c.close(client, task['id'])['state'], 'closed')
                self.assertEqual(c.close(client, task['id'])['state'], 'closed')
                c.start(client, agent='omp', cwd=directory, prompt='writer', write=True)


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
                    self.assertNotIn('native_unload_observation',result)
                    self.assertNotIn('native_cleanup',result)
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
                           native_roster=['old'], native_snapshot=[{'old':'history'}],
                           native_observation=dict(turn=1, question_ids=['1:q'], input_available=False),
                           native_input=dict(turn=1, launch_id='old', epoch=1, accepting=False),
                           interaction_seq=2, native_interaction_next=2)
            successor = c.intervene(client, task['id'], 'next', command_id='next')
            for key in ('native_result','native_cleanup','native_roster','native_snapshot','native_observation',
                        'native_input','interaction_seq','native_interaction_next'):
                self.assertNotIn(key, successor)
            successor['agy_worker_model'] = 'gemini-4-hydrogen-medium'
            message = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_RESULT '+json.dumps(
                dict(task=task['id'], turn=1, status='completed', result='old', workers=['old']))))
            self.assertIsNone(completion([message], successor))
            message['content']['text'] = message['content']['text'].replace('"turn": 1','"turn": 2')
            with self.assertRaises(ProtocolError):
                completion([message], successor)

    def test_completed_native_successor_creates_fresh_session_after_unload(self):
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
                self.assertNotEqual(result['native_session_id'], first_session)
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
