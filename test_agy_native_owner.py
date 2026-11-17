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

    def test_orchestrator_exact_config_catalog_selects_without_tier_or_prompt(self):
        from agy_native import NativeOwner
        catalog=json.loads((Path(__file__).parent/'fixtures/agy_orchestrator_catalog.json').read_text())
        for model in catalog['selected']:
            for metadata in (catalog['initial'],catalog['selected'][model]):
                with self.subTest(model=model,has_effort=len(metadata['configOptions'])>1):
                    owner=object.__new__(NativeOwner); owner.task=dict(model=model); owner.session_id='owned'
                    selected_model=[metadata['configOptions'][0]['currentValue']]; calls=[]
                    def request(method,params,**kw):
                        calls.append((method,params)); self.assertEqual(method,'session/set_config_option')
                        self.assertEqual(params['configId'],'model')
                        selected_model[0]=params['value']; return {},[]
                    def load():
                        current=selected_model[0]
                        if current in catalog['selected']:return catalog['selected'][current],[]
                        worker=json.loads(json.dumps(catalog['initial']))
                        worker['configOptions'][0]['currentValue']=current
                        worker['models']['currentModelId']=current+'-medium'
                        return worker,[]
                    with patch.object(owner,'request',side_effect=request),patch.object(owner,'load',side_effect=load):
                        result=owner.select(metadata)
                    self.assertEqual(result['model'],model)
                    self.assertIsNone(result['effort'])
                    self.assertNotIn('agy_worker_model',result)
                    self.assertEqual(selected_model[0],model)
                    self.assertTrue(result['native_selection']['readback_verified'])

    def test_explicit_coordinator_selects_from_oc_catalog_on_same_connection(self):
        from agy_native import NativeOwner, ProtocolError
        cases = [('gemini-4-carbon-medium', None, 'gemini-4-carbon-medium'),
                 ('gemini-4-argon', None, 'gemini-4-argon-medium'),
                 ('gemini-4-carbon', 'low', 'gemini-4-carbon-low'),
                 ('gemini-4-carbon', 'not-advertised', None),
                 ('gemini-4-carbon-medium', 'low', None)]
        for model, effort, expected in cases:
            with self.subTest(model=model), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/'catalog.json').write_bytes((Path(__file__).parent/'fixtures/agy_orchestrator_catalog.json').read_bytes())
                executable = root/'agy'
                executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
                executable.chmod(0o700)
                task = dict(device='local', cwd=directory, model=model, effort=effort)
                with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'],
                        AGY_NATIVE_FIXTURE=directory, AGY_NATIVE_INITIAL_MODEL='gemini-pro-orchestrator'):
                    owner = NativeOwner(task)
                    try:
                        owner.initialize()
                        owner.new()
                        metadata, _ = owner.load()
                        if expected is None:
                            with self.assertRaises(ProtocolError):
                                owner.select(metadata)
                        else:
                            result = owner.select(metadata)
                            self.assertEqual(result['model'], expected)
                            self.assertTrue(result['native_selection']['readback_verified'])
                            self.assertNotIn('agy_worker_model',result)
                        self.assertTrue(owner.close_session())
                    finally:
                        owner.stop()
                requests = [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
                self.assertFalse(any(r.get('method')=='session/prompt' for r in requests))

    def test_orchestrator_missing_catalog_or_explicit_effort_fails_before_requests(self):
        from agy_native import NativeOwner,ProtocolError
        initial=json.loads((Path(__file__).parent/'fixtures/agy_orchestrator_catalog.json').read_text())['initial']
        missing=json.loads(json.dumps(initial))
        missing['models']['availableModels']=[o for o in missing['models']['availableModels'] if o['modelId']!='gemini-pro-orchestrator']
        for task,metadata in ((dict(model='gemini-pro-orchestrator'),missing),
                              (dict(model='gemini-pro-orchestrator',effort='low'),initial)):
            owner=object.__new__(NativeOwner);owner.task=task;owner.session_id='owned'
            with patch.object(owner,'request') as request,self.assertRaises(ProtocolError):owner.select(metadata)
            request.assert_not_called()

    def test_orchestrator_restored_catalog_preserves_explicit_worker_model(self):
        from agy_native import NativeOwner
        catalog=json.loads((Path(__file__).parent/'fixtures/agy_orchestrator_catalog.json').read_text())
        owner=object.__new__(NativeOwner);owner.session_id='owned'
        owner.task=dict(model='gemini-pro-orchestrator',agy_worker_model='gemini-4-hydrogen-low')
        current=['gemini-pro-orchestrator']
        def request(method,params,**kw):current[0]=params['value'];return {},[]
        def load():
            if current[0] in catalog['selected']:return catalog['selected'][current[0]],[]
            metadata=json.loads(json.dumps(catalog['initial']))
            metadata['configOptions'][0]['currentValue']=current[0]
            metadata['models']['currentModelId']=current[0]+'-medium'
            return metadata,[]
        with patch.object(owner,'request',side_effect=request),patch.object(owner,'load',side_effect=load):
            result=owner.select(catalog['selected']['gemini-pro-orchestrator'])
        self.assertEqual(owner.task['agy_worker_model'],'gemini-4-hydrogen-low')

    def test_orchestrator_wrong_readback_and_legacy_missing_model_are_not_fallbacks(self):
        from agy_native import NativeOwner,ProtocolError
        catalog=json.loads((Path(__file__).parent/'fixtures/agy_orchestrator_catalog.json').read_text())
        owner=object.__new__(NativeOwner);owner.session_id='owned';owner.task=dict(model='gemini-pro-orchestrator')
        with patch.object(owner,'request') as request,patch.object(owner,'load',return_value=(catalog['initial'],[])):
            with self.assertRaisesRegex(ProtocolError,'readback'):owner.select(catalog['initial'])
            self.assertEqual([c.args[0] for c in request.call_args_list],['session/set_config_option'])
        owner.task={}
        with patch.object(owner,'request'),patch.object(owner,'load',return_value=(catalog['initial'],[])):
            result=owner.select(catalog['initial'])
        self.assertEqual(result['model'],'gemini-4-carbon-medium')

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
                    self.assertEqual(result['native_cleanup']['evidence'],'public-response-and-session-unload')
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
                    self.assertEqual(result['native_cleanup']['evidence'],'public-response-and-session-unload')
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
            self.assertIn('tail result',result['native_result']['result'])
            events = c.events(client, task['id'])
            self.assertEqual([e['text'] for e in events if e['kind']=='stage'].count('tail stage'), 1)
            self.assertNotIn('old result', '\n'.join(e['text'] for e in events))

    def run_question_replay(self, foreground, replays, *, include_stored=False):
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
            if include_stored:
                return c.status(client, task['id']), c.events(client, task['id']), requests, c.store.get(task['id'])
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
        result, events, _, stored = self.run_question_replay([], [[tool], dict(updates=[partial], fail=True)], include_stored=True)
        self.assertEqual([e for e in events if e['kind']=='question'], [])
        self.assertNotIn('native_snapshot', result)
        self.assertEqual(stored['native_snapshot'], [tool])
        observation = result['native_observation']
        self.assertEqual(observation['turn'], 1)
        self.assertEqual(observation['pending_tools'], [dict(id='owned-tool', name='read_board', kind='read', status='pending')])
        self.assertEqual(observation['last_success_at'], observation['last_semantic_change_at'])
        self.assertFalse(observation['input_available'])

    

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

    def test_complete_fatal_suppresses_review_in_foreground_dialogue_and_load(self):
        from agy import AGY_FATAL_LINE
        from types import SimpleNamespace
        original_read = os.read
        fatal = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=AGY_FATAL_LINE+'\n'))
        for phase in ('foreground', 'dialogue', 'load'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                executable = root/'agy'
                executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
                executable.chmod(0o700)
                gate = 'before-prompt0' if phase=='foreground' else 'before-load0'
                scenario = dict(foreground=[], replays=[[fatal]] if phase=='load' else [[]], review_gates=[gate])
                if phase=='dialogue':
                    scenario['review_gates'].append('before-prompt1')
                scenario['foregrounds'] = [[fatal]] if phase=='foreground' else [[], [fatal]] if phase=='dialogue' else [[]]
                (root/'scenario.json').write_text(json.dumps(scenario))
                clock = [0.0]
                def read(fd, size):
                    data = original_read(fd, size)
                    if b'Agent execution terminated due to error.' in data:
                        clock[0] = 600.0
                    return data
                timer = SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000+clock[0], sleep=time.sleep)
                with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], AGY_NATIVE_FIXTURE=directory), \
                        patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), \
                        patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                        patch('agy_native.time', timer), patch('agy_native.os.read', side_effect=read), \
                        contextlib.redirect_stdout(io.StringIO()):
                    c = Collaboration(root/'tasks.db', launch=lambda task: None)
                    client = c.connect('omp')['client_id']
                    task = c.start(client, agent='agy', cwd=directory, prompt='owned')
                    thread = threading.Thread(target=lambda: run_task(c.store.path, task['id'],
                        turn=1, launch_id=task['launch_id'], epoch=1))
                    thread.start()
                    try:
                        deadline = time.monotonic()+4
                        while not (root/('waiting-'+gate)).exists() and time.monotonic()<deadline:
                            time.sleep(.01)
                        self.assertTrue((root/('waiting-'+gate)).exists())
                        if phase=='dialogue':
                            c.dialogue(client, task['id'], 'explicit revision', expected_turn=1, command_id='revision')
                        if phase=='dialogue':
                            (root/('release-'+gate)).touch()
                            gate = 'before-prompt1'
                            deadline = time.monotonic()+4
                            while not (root/('waiting-'+gate)).exists() and time.monotonic()<deadline:
                                time.sleep(.01)
                            self.assertTrue((root/('waiting-'+gate)).exists())
                        clock[0] = 599.0
                        (root/('release-'+gate)).touch()
                        thread.join(6)
                        self.assertFalse(thread.is_alive())
                        self.assertEqual(c.status(client, task['id'])['state'], 'unknown')
                        events = c.events(client, task['id'])
                        self.assertFalse(any(e.get('quiet_notice') for e in events), events)
                        requests = [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
                        self.assertFalse(any(r.get('method') in {'session/cancel','session/close'} for r in requests))
                        self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests), 2 if phase=='dialogue' else 1)
                    finally:
                        (root/('release-'+gate)).touch()
                        if thread.is_alive():
                            c.cancel(client, task['id'])
                            thread.join(5)

    def test_buffered_terminal_or_foreign_frames_precede_review_but_active_frames_tick(self):
        from agy_native import NativeOwner, ProtocolError
        for frame in (dict(id=1, error=dict(code=-1, message='synthetic')),
                dict(id=1, result=dict(stopReason='cancelled')),
                dict(method='session/update', params=dict(sessionId='foreign', update={}))):
            with self.subTest(frame=frame):
                owner = object.__new__(NativeOwner)
                owner.serial=0; owner.session_id='owned'
                owner.task={}
                owner.buffer=json.dumps(frame).encode()+b'\n'
                owner.send=lambda value: None
                ticks=[]; owner.review=lambda: ticks.append(True)
                try:
                    owner.request('session/prompt', {}, timeout=None)
                except ProtocolError:
                    pass
                self.assertEqual(ticks, [])
        owner = object.__new__(NativeOwner)
        owner.serial=0; owner.session_id='owned'; owner.send=lambda value: None
        owner.task={}
        frame=dict(method='session/update', params=dict(sessionId='owned', update=dict(
            sessionUpdate='tool_call_update', toolCallId='busy', status='in_progress')))
        owner.buffer=(json.dumps(frame)+'\n').encode()*2000+ b'{"id":1,"result":{}}\n'
        ticks=[]; owner.review=lambda: ticks.append(True)
        owner.request('session/prompt', {}, timeout=None)
        self.assertEqual(len(ticks), 2000)

    def test_cancel_attempt_is_single_across_drains_loads_and_new_owners(self):
        from agy_native import NativeOwner
        task = dict(device='local', cwd='/', agy_approval='approve-all')
        with patch('agy_native.subprocess.Popen'), patch('agy_native.selectors.DefaultSelector'):
            owners = [NativeOwner(task), NativeOwner(task)]
        for owner in owners:
            owner.session_id = 'owned'
            sent = []
            owner.send = sent.append
            owner.drain_pending(check_cancel=lambda: True)
            owner.drain_pending(check_cancel=lambda: True)
            owner.load(reconcile_stream=True, check_cancel=lambda: True)
            owner.buffer = b'{"id":1,"result":{}}\n'
            owner.request('session/load', {}, check_cancel=lambda: True)
            owner.notify_cancel()
            self.assertEqual([frame for frame in sent if frame.get('method') == 'session/cancel'],
                             [dict(method='session/cancel', params=dict(sessionId='owned'))])

    def test_failed_cancel_send_is_not_retried_on_same_owner(self):
        from agy_native import NativeOwner
        with patch('agy_native.subprocess.Popen'), patch('agy_native.selectors.DefaultSelector'):
            owner = NativeOwner(dict(device='local', cwd='/'))
        owner.session_id = 'owned'
        with patch.object(owner, 'send', side_effect=OSError('uncertain synthetic send')) as send:
            with self.assertRaises(OSError):
                owner.notify_cancel()
            owner.drain_pending(check_cancel=lambda: True)
            owner.load(reconcile_stream=True, check_cancel=lambda: True)
            owner.notify_cancel()
            self.assertEqual(send.call_count, 1)

    def test_review_cooldown_spans_prompt_dialogue_and_background_until_cancelled(self):
        from types import SimpleNamespace
        from agy_native import NativeOwner
        original_request = NativeOwner.request
        for active in (False, True):
            with self.subTest(active=active), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                executable = root/'agy'
                executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
                executable.chmod(0o700)
                add = dict(sessionUpdate='tool_call', toolCallId='add', name='swarm', status='completed',
                    args=dict(action='add', name='worker-a', model='gemini-4-hydrogen-medium'))
                read = dict(sessionUpdate='tool_call', toolCallId='read', name='read_board', status='completed')
                kill = dict(sessionUpdate='tool_call', toolCallId='kill', name='swarm', status='completed',
                    args=dict(action='kill', name='worker-a'))
                gates = ['prompt0', 'load0', 'prompt1', 'load1']
                (root/'scenario.json').write_text(json.dumps(dict(foreground=[add, read],
                    replays=[[add, read]]*2, cancel_replay=[add, read, kill], review_gates=gates)))
                if active:
                    for gate in gates:
                        (root/('active-'+gate)).touch()
                clock = [0.0]
                timer = SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000+clock[0],
                    sleep=lambda seconds: time.sleep(.001))
                release_consumption = threading.Event()
                consumed = [threading.Event(), threading.Event()]
                quiesced = [threading.Event(), threading.Event()]
                prompt_index = [0]
                def observed_request(owner, method, params, **kwargs):
                    callback = kwargs.get('on_update')
                    if method == 'session/prompt' and callback is not None:
                        index = prompt_index[0]
                        prompt_index[0] += 1
                        seen = set()
                        def on_update(update):
                            if index == 0:
                                if not release_consumption.wait(4):
                                    raise AssertionError('Synthetic consumption release missing')
                            callback(update)
                            if update.get('sessionUpdate') == 'tool_call':
                                seen.add(update.get('toolCallId'))
                            if {'add', 'read'} <= seen:
                                consumed[index].set()
                            if (update.get('sessionUpdate') == 'user_message_chunk'
                                    and update.get('content', {}).get('text') == f'quiesced-prompt{index}'):
                                quiesced[index].set()
                        kwargs['on_update'] = on_update
                    return original_request(owner, method, params, **kwargs)
                with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], AGY_NATIVE_FIXTURE=directory), \
                        patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), \
                        patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                        patch('agy_native.time', timer), patch.object(NativeOwner, 'request', observed_request), \
                        contextlib.redirect_stdout(io.StringIO()):
                    c = Collaboration(root/'tasks.db', launch=lambda task: None)
                    client = c.connect('omp')['client_id']
                    other = c.connect('omp')['client_id']
                    task = c.start(client, agent='agy', cwd=directory, prompt='owned', write=True)
                    errors = []
                    def run():
                        try:
                            run_task(c.store.path, task['id'], turn=task['turn'], launch_id=task['launch_id'], epoch=1)
                        except BaseException as exc:
                            errors.append(exc)
                    thread = threading.Thread(target=run)
                    def notices():
                        return [e for e in c.events(client, task['id']) if e.get('quiet_notice')]
                    def await_condition(predicate):
                        deadline = time.monotonic()+4
                        while time.monotonic() < deadline:
                            if predicate():
                                return
                            time.sleep(.01)
                        self.fail('Native coordination condition not observed: '+repr((errors, c.status(client, task['id'])['state'], len(notices()))))
                    thread.start()
                    try:
                        for index, gate in enumerate(gates):
                            await_condition(lambda: (root/('waiting-'+gate)).exists())
                            if gate.startswith('prompt'):
                                release_consumption.set()
                                await_condition(lambda: consumed[index // 2].is_set())
                            clock[0] = (index+1)*600-1
                            self.assertEqual(len(notices()), index, gate+' bypassed shared cooldown')
                            clock[0] += 1
                            await_condition(lambda: len(notices()) == index+1)
                            self.assertEqual(c.status(client, task['id'])['state'], 'running')
                            self.assertEqual(notices()[-1]['interaction_seq'], int(index >= 2))
                            expected_kind = 'checkpoint' if active and gate.startswith('prompt') else 'quiet'
                            self.assertEqual(notices()[-1]['notice_kind'], expected_kind)
                            self.assertEqual(notices()[-1]['elapsed'], (index + 1) * 600)
                            with self.assertRaises(PermissionError):
                                c.events(other, task['id'])
                            requests = [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
                            self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests), 1+int(index >= 2))
                            self.assertFalse(any(r.get('method') in {'session/cancel','session/close'} for r in requests))
                            if gate == 'load0':
                                c.dialogue(client, task['id'], 'explicit revision', expected_turn=1, command_id='revision')
                            if gate == 'load1':
                                c.cancel(client, task['id'])
                            # Quiesce producer and drain queued prompt updates before advancing the clock.
                            if gate.startswith('prompt'):
                                (root/('quiesce-'+gate)).touch()
                                await_condition(lambda: quiesced[index // 2].is_set())
                                clock[0] = (index+2)*600-1
                            (root/('release-'+gate)).touch()
                        thread.join(5)
                        self.assertFalse(thread.is_alive())
                        self.assertEqual(errors, [])
                        self.assertEqual(c.status(client, task['id'])['state'], 'cancelled')
                        clock[0] = 6000
                        time.sleep(.15)
                        self.assertEqual(len(notices()), 4)
                        self.assertEqual(c.close(client, task['id'])['state'], 'closed')
                    finally:
                        if thread.is_alive():
                            c.cancel(client, task['id'])
                            for gate in gates:
                                (root/('release-'+gate)).touch()
                            thread.join(5)
                        self.assertFalse(thread.is_alive(), 'Synthetic owner did not stop')

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
        quiet_notices = [e for e in events if e.get('quiet_notice')]
        self.assertEqual(len(quiet_notices), 3)
        self.assertEqual([e.get('notice_kind') for e in quiet_notices], ['quiet', 'quiet', 'quiet'])
        self.assertTrue(all(isinstance(e.get('elapsed'), int) and e['elapsed'] >= 600 for e in quiet_notices))
        self.assertEqual(len([e for e in events if e['kind']=='tool']), 1)
        self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests), 1)

    def test_load_fresh_update_marks_review_checkpoint_before_quiet(self):
        from types import SimpleNamespace
        clock = [0.0]
        def sleep(seconds):
            clock[0] += 15
        tool1 = dict(sessionUpdate='tool_call', toolCallId='step1', title='read_board', status='in_progress')
        tool2 = dict(sessionUpdate='tool_call', toolCallId='step2', title='swarm', status='in_progress')
        timer = SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000+clock[0], sleep=sleep)
        with patch('agy_native.time', timer):
            _, events, requests = self.run_question_replay([], [
                [tool1],
                [tool1, tool2],
                [tool1, tool2],
                [tool1, tool2],
                [tool1, tool2],
            ])
        notices = [e for e in events if e.get('quiet_notice')]
        self.assertEqual([n.get('notice_kind') for n in notices], ['checkpoint', 'quiet'])
        self.assertEqual([n.get('elapsed') for n in notices], [600, 1200])
        self.assertEqual(sum(r.get('method') == 'session/prompt' for r in requests), 1)

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
                    self.assertEqual(result['agy_worker_model'], 'gemini-4-hydrogen')
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
                self.assertEqual(result['native_cleanup']['evidence'],'public-response-and-session-unload')
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


    def test_normal_snapshot_does_not_enforce_cancel_status_argument_contract(self):
        from agy_native import normalize, snapshot
        status=normalize(dict(sessionUpdate='tool_call',toolCallId='status',name='swarm',
            kind='other',status='completed',rawInput=dict(action='status',command='SYNTHETIC_STATUS_SECRET')))
        history=snapshot([status])
        self.assertEqual(history,[dict(sessionUpdate='tool_call',toolCallId='status',title='swarm',
            kind='other',status='completed',swarm=dict(action='status'))])
        self.assertNotIn('SYNTHETIC_STATUS_SECRET',json.dumps(history))


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
                    self.assertEqual(c.close(client, task['id'])['state'], 'unknown')
                    if not reused:self.assertEqual(c.status(client, task['id'])['resource_state'], 'released')
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


    def test_foreground_and_background_cancel_settle_via_session_unload_without_claiming_descendants_stopped(self):
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
                    self.assertEqual(result['state'], 'cancelled')
                    self.assertTrue(result['native_unload_observation']['verified'])
                    self.assertEqual(result['native_cleanup']['kind'], 'verified-cancellation')
                    self.assertFalse(result['native_cleanup']['all_descendants_proven_stopped'])
                    self.assertEqual(c.close(client, task['id'])['state'], 'closed')
                    second = c.start(client, agent='omp', cwd=directory, prompt='non-conflicting after close', write=True)
                    c.cancel(client, second['id'])

    def test_public_host_reads_hide_replay_snapshot_but_keep_result(self):
        import collab
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
            collab_inst = Collaboration(Path(root)/'collab.db', launch=lambda task: None)
            cli_id = collab_inst.connect('hermes')['client_id']
            ctask = collab_inst.start(cli_id, agent='agy', cwd=root, prompt='owned')
            diag = {'reason_code': 'prefix_changed', 'call_site': 'section', 'interaction_seq': 0, 'turn': 1}
            collab_inst.store.update(
                ctask['id'], state='unknown',
                native_snapshot=[{'private_replay': 'CANARY_PRIVATE_REPLAY'}],
                native_result={'result': 'public result'},
                native_observation={'history_degraded': True, 'history_diagnostic': diag},
            )
            for payload in (
                collab_inst.status(cli_id, ctask['id']),
                collab_inst.wait(cli_id, ctask['id'], timeout=0),
                collab_inst.list(cli_id),
                collab_inst.events(cli_id, ctask['id']),
            ):
                blob = json.dumps(payload)
                self.assertNotIn('native_snapshot', blob)
                self.assertNotIn('CANARY_PRIVATE_REPLAY', blob)
            st_pub = collab_inst.status(cli_id, ctask['id'])
            self.assertEqual(st_pub['native_result'], {'result': 'public result'})
            self.assertTrue(st_pub['native_observation']['history_degraded'])
            self.assertEqual(st_pub['native_observation']['history_diagnostic'], diag)
            cli_out = io.StringIO()
            with contextlib.redirect_stdout(cli_out):
                self.assertEqual(collab.main(['--db', str(Path(root)/'collab.db'), '--client', cli_id, 'status', '--task', ctask['id']]), 0)
            self.assertNotIn('native_snapshot', cli_out.getvalue())
            self.assertNotIn('CANARY_PRIVATE_REPLAY', cli_out.getvalue())
            rel_pub = collab_inst.release_only(cli_id, ctask['id'], expected_turn=1, abandon=True)
            self.assertNotIn('native_snapshot', json.dumps(rel_pub))
            self.assertNotIn('CANARY_PRIVATE_REPLAY', json.dumps(rel_pub))
            self.assertEqual(collab_inst.store.get(ctask['id'])['native_snapshot'], [{'private_replay': 'CANARY_PRIVATE_REPLAY'}])

    def test_native_fatal_exit_and_missing_kill_notify_initiator_and_hold(self):
        cases = [('AGY_NATIVE_FATAL', 'foreground', 'fatal execution error'),
                 ('AGY_NATIVE_FATAL', 'background', 'fatal execution error'),
                 ('AGY_NATIVE_EXIT', 'foreground', 'disconnected'),
                 ('AGY_NATIVE_EXIT', 'background', 'disconnected')]
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

    def test_deferred_question_settles_after_dialogue_supersede_seq_gap(self):
        fake_agy = '''import json, os, sys, time
from pathlib import Path
state = Path(os.environ['FAKE_AGY_STATE'])
prompt_count = 0
last_dialogue_body = None
def send(obj):
    sys.stdout.write(json.dumps(obj) + '\\n')
    sys.stdout.flush()
def notify(update):
    send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 'sess-1', 'update': update}})
for line in sys.stdin:
    if not line.strip():
        continue
    req = json.loads(line)
    rid, method, params = req.get('id'), req.get('method'), req.get('params', {})
    if method == 'initialize':
        send({'jsonrpc': '2.0', 'id': rid, 'result': {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True, 'sessionCapabilities': {'close': {}}}}})
    elif method == 'session/new':
        notify({'sessionUpdate': 'available_commands_update', 'availableCommands': [{'name': 'swarm'}]})
        send({'jsonrpc': '2.0', 'id': rid, 'result': {
            'sessionId': 'sess-1',
            'models': {'currentModelId': 'gemini-4-carbon-low', 'availableModels': [{'modelId': 'gemini-4-carbon-low'}]},
            'configOptions': [
                {'id': 'model', 'currentValue': 'gemini-4-carbon', 'options': [{'value': 'gemini-4-carbon'}]},
                {'id': 'thought_level', 'currentValue': 'low', 'options': [{'value': 'low'}]},
            ],
        }})
    elif method == 'session/prompt':
        prompt_count += 1
        text = ''.join(p.get('text', '') for p in params.get('prompt', []))
        if prompt_count == 1:
            notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Foreground ack.\\n'}})
        else:
            last_dialogue_body = text
            notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Final answer after A.\\n'}})
        send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'end_turn'}})
    elif method == 'session/load':
        if prompt_count == 1:
            notify({'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': '/swarm\\ninitial prompt'}})
            notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Foreground ack.\\nCALLCLI_QUESTION Q1 Pick A or B'}})
            if not (state / 'first_load_done').exists():
                (state / 'first_load_done').touch()
            else:
                deadline = time.monotonic() + 5
                while not (state / 'proceed_load').exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
        elif prompt_count >= 2:
            notify({'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': '/swarm\\ninitial prompt'}})
            notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Foreground ack.\\nCALLCLI_QUESTION Q1 Pick A or B'}})
            if last_dialogue_body is not None:
                notify({'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': last_dialogue_body}})
                notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Final answer after A.\\n'}})
        send({'jsonrpc': '2.0', 'id': rid, 'result': {
            'sessionId': 'sess-1',
            'models': {'currentModelId': 'gemini-4-carbon-low', 'availableModels': [{'modelId': 'gemini-4-carbon-low'}]},
            'configOptions': [
                {'id': 'model', 'currentValue': 'gemini-4-carbon', 'options': [{'value': 'gemini-4-carbon'}]},
                {'id': 'thought_level', 'currentValue': 'low', 'options': [{'value': 'low'}]},
            ],
        }})
    elif method == 'session/close':
        (state / 'closed').touch()
        send({'jsonrpc': '2.0', 'id': rid, 'result': {}})
    elif method == 'session/set_config_option':
        if (state / 'closed').exists():
            send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32602, 'message': 'Session not found'}})
        else:
            send({'jsonrpc': '2.0', 'id': rid, 'result': {'configOptions': [
                {'id': 'model', 'currentValue': 'gemini-4-carbon', 'options': [{'value': 'gemini-4-carbon'}]},
                {'id': 'thought_level', 'currentValue': 'low', 'options': [{'value': 'low'}]},
            ]}})
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / 'state'
            state_dir.mkdir()
            executable = root / 'agy'
            executable.write_text('#!' + sys.executable + '\n' + fake_agy)
            executable.chmod(0o700)
            with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'], FAKE_AGY_STATE=str(state_dir)), \
                    patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                    patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                client = c.connect('hermes')['client_id']
                task = c.start(client, agent='agy', model='4cs', cwd=directory, prompt='initial prompt', write=False)
                thread = threading.Thread(target=lambda: run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1), daemon=True)
                thread.start()
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and thread.is_alive():
                        st = c.status(client, task['id'])
                        self.assertIn(st.get('state'), {'queued', 'running'}, st.get('settlement_note'))
                        if (state_dir / 'first_load_done').exists() and st.get('native_observation', {}).get('question_blockers'):
                            break
                        time.sleep(0.01)
                    st = c.status(client, task['id'])
                    self.assertEqual(st.get('state'), 'running', st.get('settlement_note'))
                    c.dialogue(client, task['id'], 'draft 1', expected_turn=1, command_id='d1')
                    c.dialogue(client, task['id'], 'proceed with A', expected_turn=1, command_id='d2', target_command_id='d1')
                    (state_dir / 'proceed_load').touch()
                    thread.join(5)
                    self.assertFalse(thread.is_alive(), 'Deferred seq-gap task failed to settle')
                    result = c.status(client, task['id'])
                    self.assertEqual(result['state'], 'completed', result.get('settlement_note'))
                    self.assertEqual(result['commands']['d1']['status'], 'superseded')
                    self.assertEqual(result['commands']['d2']['status'], 'returned')
                    self.assertEqual(result['native_observation']['question_blockers'], [])
                    supersede_stages = [e for e in c.events(client, task['id'])
                                        if e.get('kind') == 'stage' and 'Incomplete question superseded by returned dialogue d2' in e.get('text', '')]
                    self.assertEqual(len(supersede_stages), 1)
                    self.assertEqual(supersede_stages[0]['interaction_seq'], 0)
                finally:
                    if thread.is_alive():
                        c.cancel(client, task['id'])
                        thread.join(3)

    def test_write_delegated_intervene_resumes_exact_session_on_turn2_and_holds_when_unsupported_or_unsettled(self):
        fake_agy = '''import json, os, sys
from pathlib import Path
state = Path(os.environ['FAKE_AGY_STATE'])
mode = os.environ.get('FAKE_AGY_MODE', 'normal')
sid = 'sess-exact-resume-1'
closed = False
loads = 0
def send(obj):
    sys.stdout.write(json.dumps(obj) + '\\n')
    sys.stdout.flush()
def notify(u):
    send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': sid, 'update': u}})
def meta():
    return {
        'sessionId': sid,
        'models': {'currentModelId': 'gemini-4-carbon-low', 'availableModels': [{'modelId': 'gemini-4-carbon-low'}]},
        'configOptions': [
            {'id': 'model', 'currentValue': 'gemini-4-carbon', 'options': [{'value': 'gemini-4-carbon'}]},
            {'id': 'thought_level', 'currentValue': 'low', 'options': [{'value': 'low'}]},
        ],
    }
for line in sys.stdin:
    if not line.strip():
        continue
    req = json.loads(line)
    rid, method, params = req.get('id'), req.get('method'), req.get('params', {})
    with (state / 'rpcs.jsonl').open('a') as f:
        f.write(json.dumps({'method': method, 'params': params}) + '\\n')
    if method == 'initialize':
        scaps = {'close': {}} if mode == 'no_resume_cap' else {'close': {}, 'resume': {}}
        send({'jsonrpc': '2.0', 'id': rid, 'result': {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True, 'sessionCapabilities': scaps}}})
    elif method == 'session/new':
        notify({'sessionUpdate': 'available_commands_update', 'availableCommands': [{'name': 'swarm'}]})
        send({'jsonrpc': '2.0', 'id': rid, 'result': meta()})
    elif method == 'session/resume':
        (state / 'resumed_sid').write_text(str(params.get('sessionId')))
        (state / 'loads_at_resume').write_text(str(loads))
        send({'jsonrpc': '2.0', 'id': rid, 'result': meta()})
    elif method == 'session/load':
        loads += 1
        if (state / 'turn2_prompt').exists():
            notify({'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': '/swarm\\nturn1'}})
            notify({'sessionUpdate': 'tool_call', 'toolCallId': 'sw-1', 'title': 'swarm', 'kind': 'other', 'status': 'completed', 'rawInput': {'action': 'add', 'name': 'w1'}})
            notify({'sessionUpdate': 'tool_call', 'toolCallId': 'tc-1', 'title': 'bash', 'kind': 'execute', 'status': 'failed'})
            notify({'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': (state / 'turn2_prompt').read_text()}})
            notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Turn 2 resumed deliverable.\\n'}})
        send({'jsonrpc': '2.0', 'id': rid, 'result': meta()})
    elif method == 'session/prompt':
        ptext = ''.join(p.get('text', '') for p in params.get('prompt', []))
        if 'revised turn2 goal' in ptext:
            (state / 'loads_before_turn2_prompt').write_text(str(loads))
            (state / 'turn2_prompt').write_text(ptext)
            notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Turn 2 resumed deliverable.\\n'}})
            send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'end_turn'}})
        else:
            notify({'sessionUpdate': 'tool_call', 'toolCallId': 'sw-1', 'title': 'swarm', 'kind': 'other', 'status': 'completed', 'rawInput': {'action': 'add', 'name': 'w1'}})
            notify({'sessionUpdate': 'tool_call', 'toolCallId': 'tc-1', 'title': 'bash', 'kind': 'execute', 'status': 'in_progress'})
            (state / 'active').touch()
            rline = sys.stdin.readline()
            if rline and json.loads(rline).get('method') == 'session/cancel':
                if mode == 'disconnect_on_cancel':
                    sys.exit(1)
                send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'cancelled'}})
    elif method == 'session/close':
        closed = True
        send({'jsonrpc': '2.0', 'id': rid, 'result': {}})
    elif method == 'session/set_config_option':
        if closed:
            send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32602, 'message': 'Session not found: ' + sid}})
        else:
            send({'jsonrpc': '2.0', 'id': rid, 'result': meta()})
'''
        # Case 1: write=True + Swarm delegated + intervene -> Turn 1 cancelled (resumable), Turn 2 session/resume completes
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / 'state_ok'
            state_dir.mkdir()
            exe = root / 'agy'
            exe.write_text('#!' + sys.executable + '\n' + fake_agy)
            exe.chmod(0o700)
            with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'], FAKE_AGY_STATE=str(state_dir), FAKE_AGY_MODE='normal'), \
                    patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                    patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                client = c.connect('hermes')['client_id']
                task = c.start(client, agent='agy', model='4cs', cwd=directory, prompt='turn1', write=True)
                rc1 = []
                t1 = threading.Thread(target=lambda: rc1.append(run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)))
                t1.start()
                deadline = time.monotonic() + 5
                while not (state_dir / 'active').exists() and t1.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                c.intervene(client, task['id'], 'revised turn2 goal', command_id='iv-t2', expected_turn=1)
                t1.join(5)
                self.assertEqual(rc1, [0])
                st1 = c.status(client, task['id'])
                self.assertEqual(st1['state'], 'cancelled')
                self.assertTrue(st1['native_cancel']['resumable'])
                self.assertEqual(st1['native_cancel']['workers'], 'delegated_observed')
                self.assertFalse(st1['native_cancel']['all_descendants_proven_stopped'])
                # Writer lock still held while Turn 2 intervention is pending
                with self.assertRaisesRegex(ValueError, 'already owned'):
                    c.start(client, agent='omp', cwd=directory, prompt='other writer', write=True)
                waited = c.wait(client, task['id'], timeout=0.1)
                self.assertEqual(waited['task']['turn'], 2)
                t2_task = c.store.get(task['id'])
                rc2 = run_task(c.store.path, task['id'], turn=2, launch_id=t2_task['launch_id'], epoch=t2_task.get('control_epoch', 1))
                self.assertEqual(rc2, 0)
                st2 = c.status(client, task['id'])
                self.assertEqual(st2['state'], 'completed')
                self.assertEqual(st2['native_result']['result'], 'Turn 2 resumed deliverable.')
                self.assertEqual((state_dir / 'resumed_sid').read_text(), 'sess-exact-resume-1')
                # No session/load called on Turn 2 prior to session/prompt
                self.assertEqual((state_dir / 'loads_at_resume').read_text(), (state_dir / 'loads_before_turn2_prompt').read_text())
                self.assertEqual(c.close(client, task['id'])['state'], 'closed')

        # Case 2: Missing resume capability or disconnect on cancel -> retains HOLD (unknown)
        for fail_mode in ('no_resume_cap', 'disconnect_on_cancel'):
            with self.subTest(fail_mode=fail_mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state_dir = root / ('state_' + fail_mode)
                state_dir.mkdir()
                exe = root / 'agy'
                exe.write_text('#!' + sys.executable + '\n' + fake_agy)
                exe.chmod(0o700)
                with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'], FAKE_AGY_STATE=str(state_dir), FAKE_AGY_MODE=fail_mode), \
                        patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                        patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                    client = c.connect('hermes')['client_id']
                    task = c.start(client, agent='agy', model='4cs', cwd=directory, prompt='turn1', write=True)
                    t1 = threading.Thread(target=lambda: run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1))
                    t1.start()
                    deadline = time.monotonic() + 5
                    while not (state_dir / 'active').exists() and t1.is_alive() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if fail_mode == 'no_resume_cap':
                        c.intervene(client, task['id'], 'revised turn2 goal', command_id='iv-fail', expected_turn=1)
                    else:
                        c.cancel(client, task['id'])
                    t1.join(5)
                    st = c.status(client, task['id'])
                    self.assertEqual(st['state'], 'unknown')
                    self.assertFalse(st['native_cancel']['resumable'])

    def test_resumed_session_restores_coordinator_selection_and_fails_closed_on_bad_readback(self):
        from agy_native import NativeOwner, ProtocolError

        catalog_path = Path(__file__).parent / 'fixtures/agy_orchestrator_catalog.json'
        catalog = json.loads(catalog_path.read_text())
        fake_agy = '''import copy, json, os, sys
from pathlib import Path
state = Path(os.environ['FAKE_AGY_STATE'])
mode = os.environ.get('FAKE_AGY_MODE', 'normal')
catalog = json.loads((state / 'catalog.json').read_text())
sid = 'sess-ocs-resume-1'
standalone = {'gemini-pro-orchestrator', 'gemini-pro-orchestrator-argon'}
active_model = 'gemini-4-carbon'
active_effort = 'medium'
closed = False
loads = 0
is_resume = False

def send(obj):
    sys.stdout.write(json.dumps(obj) + '\\n')
    sys.stdout.flush()

def notify(u):
    send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': sid, 'update': u}})

def full_meta():
    if active_model in catalog['selected']:
        data = copy.deepcopy(catalog['selected'][active_model])
    else:
        data = copy.deepcopy(catalog['initial'])
        data['models']['currentModelId'] = active_model + '-' + (active_effort or 'medium')
        for opt in data['configOptions']:
            if opt['id'] == 'model':
                opt['currentValue'] = active_model
            elif opt['id'] == 'thought_level':
                opt['currentValue'] = active_effort
    data['sessionId'] = sid
    return data

def cfg_only_meta(cid=None):
    base = full_meta()
    opts = base['configOptions']
    if is_resume and mode == 'stale_model_readback':
        for opt in opts:
            if opt['id'] == 'model':
                opt['currentValue'] = 'gemini-4-carbon'
    elif is_resume and mode == 'residual_effort_on_standalone':
        if not any(o['id'] == 'thought_level' for o in opts):
            opts.append(copy.deepcopy(catalog['initial']['configOptions'][1]))
    elif is_resume and mode == 'stale_effort_readback' and cid == 'thought_level':
        for opt in opts:
            if opt['id'] == 'thought_level':
                opt['currentValue'] = 'medium'
    res = {'configOptions': opts}
    if is_resume and mode == 'mismatched_models_block':
        res['models'] = {
            'currentModelId': 'gemini-4-carbon-medium',
            'availableModels': base['models']['availableModels'],
        }
    return res

for line in sys.stdin:
    if not line.strip():
        continue
    req = json.loads(line)
    rid, method, params = req.get('id'), req.get('method'), req.get('params', {})
    with (state / 'rpcs.jsonl').open('a') as f:
        f.write(json.dumps({'pid': os.getpid(), 'is_resume': is_resume, 'method': method, 'params': params}) + '\\n')
    if method == 'initialize':
        send({'jsonrpc': '2.0', 'id': rid, 'result': {
            'protocolVersion': 1,
            'agentCapabilities': {'loadSession': True, 'sessionCapabilities': {'close': {}, 'resume': {}}},
        }})
    elif method == 'session/new':
        notify({'sessionUpdate': 'available_commands_update', 'availableCommands': [{'name': 'swarm'}]})
        send({'jsonrpc': '2.0', 'id': rid, 'result': full_meta()})
    elif method == 'session/resume':
        is_resume = True
        (state / 'resumed_sid').write_text(str(params.get('sessionId')))
        (state / 'loads_at_resume').write_text(str(loads))
        notify({'sessionUpdate': 'available_commands_update', 'availableCommands': [{'name': 'swarm'}]})
        meta = full_meta()
        if mode == 'unadvertised_on_resume':
            meta['models']['availableModels'] = [
                m for m in meta['models']['availableModels'] if m['modelId'] != 'gemini-pro-orchestrator'
            ]
            for opt in meta['configOptions']:
                if opt['id'] == 'model':
                    opt['options'] = [o for o in opt['options'] if o['value'] != 'gemini-pro-orchestrator']
        send({'jsonrpc': '2.0', 'id': rid, 'result': meta})
    elif method == 'session/set_config_option':
        if closed:
            send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32602, 'message': 'Session not found: ' + sid}})
        else:
            cid = params.get('configId')
            val = params.get('value')
            if cid == 'model':
                active_model = val
                active_effort = None if val in standalone else (active_effort or 'medium')
            elif cid == 'thought_level':
                active_effort = val
            res = cfg_only_meta(cid=cid)
            notify({'sessionUpdate': 'config_option_update', 'configOptions': res['configOptions']})
            send({'jsonrpc': '2.0', 'id': rid, 'result': res})
    elif method == 'session/load':
        loads += 1
        if (state / 'turn2_prompt').exists():
            notify({'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': '/swarm\\nturn1'}})
            notify({'sessionUpdate': 'tool_call', 'toolCallId': 'sw-1', 'title': 'swarm', 'kind': 'other', 'status': 'completed'})
            notify({'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': (state / 'turn2_prompt').read_text()}})
            notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Resumed deliverable.\\n'}})
        send({'jsonrpc': '2.0', 'id': rid, 'result': full_meta()})
    elif method == 'session/prompt':
        ptext = ''.join(p.get('text', '') for p in params.get('prompt', []))
        if not is_resume:
            notify({'sessionUpdate': 'tool_call', 'toolCallId': 'sw-1', 'title': 'swarm', 'kind': 'other', 'status': 'completed'})
            (state / 'active').touch()
            rline = sys.stdin.readline()
            if rline and json.loads(rline).get('method') == 'session/cancel':
                send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'cancelled'}})
        else:
            (state / 'loads_before_turn2_prompt').write_text(str(loads))
            (state / 'turn2_prompt').write_text(ptext)
            (state / 'turn2_selection').write_text(json.dumps({'model': active_model, 'effort': active_effort}))
            (state.parent / 'resumed.txt').write_text('RESUMED_OK')
            notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Resumed deliverable.\\n'}})
            send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'end_turn'}})
    elif method == 'session/close':
        closed = True
        send({'jsonrpc': '2.0', 'id': rid, 'result': {}})
'''
        positive_cases = [
            ('ocs', None, 'gemini-pro-orchestrator', None),
            ('4as', 'low', 'gemini-4-argon-low', 'low'),
        ]
        for start_model, start_effort, expected_model, expected_effort in positive_cases:
            with self.subTest(case='positive', model=start_model, effort=start_effort), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state_dir = root / 'state'
                state_dir.mkdir()
                (state_dir / 'catalog.json').write_text(json.dumps(catalog))
                exe = root / 'agy'
                exe.write_text('#!' + sys.executable + '\n' + fake_agy)
                exe.chmod(0o700)
                with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                                FAKE_AGY_STATE=str(state_dir), FAKE_AGY_MODE='normal'), \
                        patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                        patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                    client = c.connect('hermes')['client_id']
                    task = c.start(client, agent='agy', model=start_model, effort=start_effort,
                                   cwd=directory, prompt='turn1', write=True)
                    rc1 = []
                    t1 = threading.Thread(target=lambda: rc1.append(run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)))
                    t1.start()
                    deadline = time.monotonic() + 5
                    while not (state_dir / 'active').exists() and t1.is_alive() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue((state_dir / 'active').exists())
                    c.intervene(client, task['id'], 'turn2 resume', command_id='cancel-resume-v1', expected_turn=1)
                    t1.join(5)
                    self.assertEqual(rc1, [0])
                    st1 = c.status(client, task['id'])
                    self.assertEqual(st1['state'], 'cancelled')
                    self.assertTrue(st1['native_cancel']['resumable'])
                    self.assertTrue(st1['native_cancel']['write_lock_retained'])
                    waited = c.wait(client, task['id'], timeout=0.1)
                    self.assertEqual(waited['task']['turn'], 2)
                    t2_task = c.store.get(task['id'])
                    rc2 = run_task(c.store.path, task['id'], turn=2, launch_id=t2_task['launch_id'],
                                   epoch=t2_task.get('control_epoch', 1))
                    st2 = c.status(client, task['id'])
                    self.assertEqual(rc2, 0, st2.get('settlement_note'))
                    self.assertEqual(st2['state'], 'completed', st2.get('settlement_note'))
                    self.assertEqual(st2['model'], expected_model)
                    self.assertEqual(st2['effort'], expected_effort)
                    self.assertEqual(st2['native_result']['result'], 'Resumed deliverable.')
                    self.assertEqual((root / 'resumed.txt').read_text(), 'RESUMED_OK')
                    self.assertEqual((state_dir / 'resumed_sid').read_text(), 'sess-ocs-resume-1')
                    self.assertEqual((state_dir / 'loads_at_resume').read_text(),
                                     (state_dir / 'loads_before_turn2_prompt').read_text())
                    actual_sel = json.loads((state_dir / 'turn2_selection').read_text())
                    expected_family = expected_model if expected_effort is None else expected_model[:-(len(expected_effort) + 1)]
                    self.assertEqual(actual_sel, {'model': expected_family, 'effort': expected_effort})
                    self.assertEqual(c.close(client, task['id'])['state'], 'closed')

        negative_cases = [
            ('stale_model_readback', 'ocs', None, True),
            ('residual_effort_on_standalone', 'ocs', None, True),
            ('stale_effort_readback', '4as', 'low', True),
            ('mismatched_models_block', 'ocs', None, True),
            ('unadvertised_on_resume', 'ocs', None, False),
        ]
        for fail_mode, start_model, start_effort, expect_set_cfg in negative_cases:
            with self.subTest(fail_mode=fail_mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state_dir = root / 'state'
                state_dir.mkdir()
                (state_dir / 'catalog.json').write_text(json.dumps(catalog))
                exe = root / 'agy'
                exe.write_text('#!' + sys.executable + '\n' + fake_agy)
                exe.chmod(0o700)
                with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                                FAKE_AGY_STATE=str(state_dir), FAKE_AGY_MODE=fail_mode), \
                        patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                        patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                    client = c.connect('hermes')['client_id']
                    task = c.start(client, agent='agy', model=start_model, effort=start_effort,
                                   cwd=directory, prompt='turn1', write=True)
                    t1 = threading.Thread(target=lambda: run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1))
                    t1.start()
                    deadline = time.monotonic() + 5
                    while not (state_dir / 'active').exists() and t1.is_alive() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    c.intervene(client, task['id'], 'turn2 resume', command_id='cancel-resume-neg', expected_turn=1)
                    t1.join(5)
                    waited = c.wait(client, task['id'], timeout=0.1)
                    self.assertEqual(waited['task']['turn'], 2)
                    t2_task = c.store.get(task['id'])
                    rc2 = run_task(c.store.path, task['id'], turn=2, launch_id=t2_task['launch_id'],
                                   epoch=t2_task.get('control_epoch', 1))
                    self.assertEqual(rc2, 1)
                    st2 = c.status(client, task['id'])
                    self.assertEqual(st2['state'], 'unknown')
                    self.assertNotIn('native_cleanup', st2)
                    self.assertFalse((root / 'resumed.txt').exists())
                    rpcs = [json.loads(line) for line in (state_dir / 'rpcs.jsonl').read_text().splitlines()]
                    t2_rpcs = [r for r in rpcs if r.get('is_resume')]
                    close_idx = next((idx for idx, r in enumerate(t2_rpcs) if r['method'] == 'session/close'), len(t2_rpcs))
                    pre_close_t2_rpcs = t2_rpcs[:close_idx]
                    self.assertFalse(any(r['method'] == 'session/prompt' for r in t2_rpcs))
                    self.assertFalse(any(r['method'] == 'session/load' for r in t2_rpcs))
                    self.assertEqual(any(r['method'] == 'session/set_config_option' for r in pre_close_t2_rpcs), expect_set_cfg)
                    if expect_set_cfg:
                        self.assertIn('readback mismatch', st2.get('settlement_note', ''))
                    else:
                        self.assertIn('not advertised', st2.get('settlement_note', ''))
                    with self.assertRaisesRegex(ValueError, 'already owned'):
                        c.start(client, agent='omp', cwd=directory, prompt='other writer', write=True)
                    self.assertEqual(c.close(client, task['id'])['state'], 'unknown')

        # Direct unit check: two-step standalone -> family restoration on allow_load=False without owner.load()
        owner = object.__new__(NativeOwner)
        owner.task = dict(model='gemini-4-carbon', effort='low')
        owner.session_id = 'owned'
        calls = []

        def unit_req(method, params, **kw):
            calls.append((method, params))
            self.assertEqual(method, 'session/set_config_option')
            if params['configId'] == 'model':
                return {'configOptions': json.loads(json.dumps(catalog['initial']['configOptions']))}, []
            updated = json.loads(json.dumps(catalog['initial']['configOptions']))
            updated[1]['currentValue'] = params['value']
            return {'configOptions': updated}, []

        with patch.object(owner, 'request', side_effect=unit_req), patch.object(owner, 'load') as load_mock:
            res = owner.select(json.loads(json.dumps(catalog['selected']['gemini-pro-orchestrator'])), allow_load=False)
        load_mock.assert_not_called()
        self.assertEqual(res['model'], 'gemini-4-carbon-low')
        self.assertEqual(res['effort'], 'low')
        self.assertEqual([p['configId'] for _, p in calls], ['model', 'thought_level'])

        # Missing models.currentModelId in session/resume metadata must fail closed
        bad_meta = json.loads(json.dumps(catalog['selected']['gemini-pro-orchestrator']))
        bad_meta['models'].pop('currentModelId', None)
        owner2 = object.__new__(NativeOwner)
        owner2.task = dict(model='gemini-pro-orchestrator')
        owner2.session_id = 'owned'
        with patch.object(owner2, 'request') as req_mock, patch.object(owner2, 'load') as load_mock:
            with self.assertRaisesRegex(ProtocolError, 'readback'):
                owner2.select(bad_meta, allow_load=False)
        req_mock.assert_not_called()
        load_mock.assert_not_called()

    def test_resumed_turn_missing_boundary_degrades_preserving_live_outcomes_and_rejecting_stale_or_conflicting_replay(self):
        catalog = json.loads((Path(__file__).parent / 'fixtures/agy_orchestrator_catalog.json').read_text())
        fake_agy = '''import copy, json, os, sys
from pathlib import Path
state = Path(os.environ['FAKE_AGY_STATE'])
mode = os.environ.get('FAKE_AGY_MODE', 'positive_ocs')
catalog = json.loads((state / 'catalog.json').read_text())
sid = 'sess-resumed-missing-boundary-1'
standalone = {'gemini-pro-orchestrator', 'gemini-pro-orchestrator-argon'}
active_model = 'gemini-4-carbon'
active_effort = 'medium'
closed = False
is_resume = False

def send(obj):
    sys.stdout.write(json.dumps(obj) + '\\n')
    sys.stdout.flush()

def notify(u):
    send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': sid, 'update': u}})

def full_meta():
    if active_model in catalog['selected']:
        data = copy.deepcopy(catalog['selected'][active_model])
    else:
        data = copy.deepcopy(catalog['initial'])
        data['models']['currentModelId'] = active_model + '-' + (active_effort or 'medium')
        for opt in data['configOptions']:
            if opt['id'] == 'model':
                opt['currentValue'] = active_model
            elif opt['id'] == 'thought_level':
                opt['currentValue'] = active_effort
    data['sessionId'] = sid
    return data

def cfg_only_meta():
    return {'configOptions': full_meta()['configOptions']}

for line in sys.stdin:
    if not line.strip():
        continue
    req = json.loads(line)
    rid, method, params = req.get('id'), req.get('method'), req.get('params', {})
    with (state / 'rpcs.jsonl').open('a') as f:
        f.write(json.dumps({'is_resume': is_resume, 'method': method}) + '\\n')
    if method == 'initialize':
        send({'jsonrpc': '2.0', 'id': rid, 'result': {
            'protocolVersion': 1,
            'agentCapabilities': {'loadSession': True, 'sessionCapabilities': {'close': {}, 'resume': {}}},
        }})
    elif method == 'session/new':
        notify({'sessionUpdate': 'available_commands_update', 'availableCommands': [{'name': 'swarm'}]})
        send({'jsonrpc': '2.0', 'id': rid, 'result': full_meta()})
    elif method == 'session/resume':
        is_resume = True
        notify({'sessionUpdate': 'available_commands_update', 'availableCommands': [{'name': 'swarm'}]})
        send({'jsonrpc': '2.0', 'id': rid, 'result': full_meta()})
    elif method == 'session/set_config_option':
        if closed:
            send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32602, 'message': 'Session not found: ' + sid}})
        else:
            if params.get('configId') == 'model':
                active_model = params.get('value')
                active_effort = None if active_model in standalone else (active_effort or 'medium')
            elif params.get('configId') == 'thought_level':
                active_effort = params.get('value')
            res = cfg_only_meta()
            notify({'sessionUpdate': 'config_option_update', 'configOptions': res['configOptions']})
            send({'jsonrpc': '2.0', 'id': rid, 'result': res})
    elif method == 'session/load':
        # Omit Turn 2 prompt_text boundary while replaying Turn 1 failures, stale answer, and predecessor fatal.
        notify({'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': '/swarm\\nturn1'}})
        notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t1_ok_read', 'title': 'read_file', 'kind': 'read', 'status': 'completed'})
        notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t1_fail_read', 'title': 'read_file', 'kind': 'read', 'status': 'failed'})
        notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t1_edit', 'title': 'write_file', 'kind': 'edit', 'status': 'completed'})
        notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'CALLCLI_STAGE CANCEL_WINDOW_READY\\nOLD_TURN1_STALE_ANSWER\\n\\u26a0 Agent execution terminated due to error.\\n'}})
        notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t1_wait', 'title': 'run_command', 'kind': 'execute', 'status': 'failed'})
        notify({'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': '[compacted resume prompt without slash prefix]'}})
        if mode == 'turn2_tool_identity_conflict':
            notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t2_write', 'title': 'write_file', 'kind': 'read', 'status': 'completed'})
        elif mode == 'turn2_tool_status_regressed':
            notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t2_write', 'title': 'write_file', 'kind': 'edit', 'status': 'failed'})
        elif mode in {'positive_ocs', 'positive_dialogue'}:
            notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t2_plan', 'title': 'exit_plan_mode', 'kind': 'other', 'status': 'completed'})
            notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t2_write', 'title': 'write_file', 'kind': 'edit', 'status': 'completed'})
            if (state / 'dlg_sent').exists():
                notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Confirmed resumed write: RESUMED_CANCEL_V1.\\n'}})
        send({'jsonrpc': '2.0', 'id': rid, 'result': full_meta()})
    elif method == 'session/prompt':
        ptext = ''.join(p.get('text', '') for p in params.get('prompt', []))
        if not is_resume:
            notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t1_ok_read', 'title': 'read_file', 'kind': 'read', 'status': 'completed'})
            notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t1_fail_read', 'title': 'read_file', 'kind': 'read', 'status': 'failed'})
            (state.parent / 'draft.txt').write_text('DRAFT_CANCEL_V1')
            notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t1_edit', 'title': 'write_file', 'kind': 'edit', 'status': 'completed'})
            notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'CALLCLI_STAGE CANCEL_WINDOW_READY\\n'}})
            notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t1_wait', 'title': 'run_command', 'kind': 'execute', 'status': 'in_progress'})
            (state / 't1_active').touch()
            rline = sys.stdin.readline()
            if rline and json.loads(rline).get('method') == 'session/cancel':
                notify({'sessionUpdate': 'tool_call_update', 'toolCallId': 'call_t1_wait', 'status': 'failed'})
                send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'cancelled'}})
        elif ptext.startswith('CALLCLI_DIALOGUE '):
            (state / 'dlg_sent').touch()
            (state.parent / 'resumed.txt').write_text('RESUMED_CANCEL_V1')
            notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Confirmed resumed write: RESUMED_CANCEL_V1.\\n'}})
            send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'end_turn'}})
        else:
            if mode == 'positive_ocs':
                notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t2_plan', 'title': 'exit_plan_mode', 'kind': 'other', 'status': 'in_progress'})
                notify({'sessionUpdate': 'tool_call_update', 'toolCallId': 'call_t2_plan', 'status': 'completed'})
                (state.parent / 'resumed.txt').write_text('RESUMED_CANCEL_V1')
                notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t2_write', 'title': 'write_file', 'kind': 'edit', 'status': 'completed'})
                notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'CALLCLI_STAGE RESUMED_CANCEL_VERIFIED\\n### Actual results\\n- Preserved draft.txt unchanged.\\n- Created resumed.txt containing RESUMED_CANCEL_V1.\\n'}})
            elif mode == 'positive_dialogue':
                notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t2_plan', 'title': 'exit_plan_mode', 'kind': 'other', 'status': 'completed'})
                notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t2_write', 'title': 'write_file', 'kind': 'edit', 'status': 'in_progress'})
                notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'CALLCLI_QUESTION Q1 Confirm resumed write?\\n'}})
            elif mode == 'no_live_answer_old_replay':
                notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t2_read', 'title': 'read_file', 'kind': 'read', 'status': 'completed'})
                notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'CALLCLI_STAGE TURN2_STAGE_ONLY\\n'}})
            elif mode == 'board_nav_only_old_replay':
                notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Run /swarm board to open the live board.\\n'}})
            elif mode == 'turn2_live_fatal':
                notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Draft before fatal\\n\\u26a0 Agent execution terminated due to error.\\n'}})
            elif mode == 'turn2_unrecovered_tool_failure':
                notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Draft before failure\\n'}})
                notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t2_fail', 'title': 'run_command', 'kind': 'execute', 'status': 'failed'})
            elif mode in {'turn2_tool_identity_conflict', 'turn2_tool_status_regressed'}:
                notify({'sessionUpdate': 'tool_call', 'toolCallId': 'call_t2_write', 'title': 'write_file', 'kind': 'edit', 'status': 'completed'})
                notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Claimed answer despite conflict.\\n'}})
            elif mode == 'turn2_live_interrupted_diagnostic':
                notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'The stream was interrupted. Please continue the task you were working on.\\n'}})
            send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'end_turn'}})
    elif method == 'session/close':
        closed = True
        send({'jsonrpc': '2.0', 'id': rid, 'result': {}})
'''
        @contextlib.contextmanager
        def setup_resumed_cancel(mode):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state_dir = root / 'state'
                state_dir.mkdir()
                (state_dir / 'catalog.json').write_text(json.dumps(catalog))
                exe = root / 'agy'
                exe.write_text('#!' + sys.executable + '\n' + fake_agy)
                exe.chmod(0o700)
                with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                                FAKE_AGY_STATE=str(state_dir), FAKE_AGY_MODE=mode), \
                        patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                        patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                    client = c.connect('hermes')['client_id']
                    task = c.start(client, agent='agy', model='ocs', cwd=directory, prompt='turn1', write=True)
                    rc1 = []
                    t1 = threading.Thread(target=lambda: rc1.append(
                        run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)))
                    t1.start()
                    deadline = time.monotonic() + 5
                    while not (state_dir / 't1_active').exists() and t1.is_alive() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue((state_dir / 't1_active').exists())
                    c.intervene(client, task['id'], 'turn2 resume', command_id='cancel-resume-v1', expected_turn=1)
                    t1.join(5)
                    self.assertEqual(rc1, [0])
                    waited = c.wait(client, task['id'], timeout=0.1)
                    self.assertEqual(waited['task']['turn'], 2)
                    t2_task = c.store.get(task['id'])
                    yield root, state_dir, c, client, task, t2_task

        # Positive 1: Exact missing-boundary shape (a7e778fd204a402b) settles completed with Turn 2 live result
        with self.subTest(case='positive_ocs'), setup_resumed_cancel('positive_ocs') as (root, state_dir, c, client, task, t2_task):
            rc2 = run_task(c.store.path, task['id'], turn=2, launch_id=t2_task['launch_id'], epoch=t2_task.get('control_epoch', 1))
            st2 = c.status(client, task['id'])
            self.assertEqual(rc2, 0, st2.get('settlement_note'))
            self.assertEqual(st2['state'], 'completed', st2.get('settlement_note'))
            self.assertEqual((root / 'draft.txt').read_text(), 'DRAFT_CANCEL_V1')
            self.assertEqual((root / 'resumed.txt').read_text(), 'RESUMED_CANCEL_V1')
            self.assertIs(st2['native_observation']['history_degraded'], True)
            self.assertEqual(st2['native_observation']['history_diagnostic']['reason_code'], 'resumed_boundary_missing')
            self.assertIs(st2['native_cleanup']['session_unloaded'], True)
            self.assertIn('Created resumed.txt containing RESUMED_CANCEL_V1.', st2['native_result']['result'])
            self.assertNotIn('OLD_TURN1_STALE_ANSWER', st2['native_result']['result'])
            t2_events = [e for e in c.events(client, task['id']) if e.get('turn') == 2]
            t2_stages = [e['text'] for e in t2_events if e['kind'] == 'stage']
            self.assertEqual(t2_stages.count('RESUMED_CANCEL_VERIFIED'), 1)
            self.assertNotIn('CANCEL_WINDOW_READY', t2_stages)
            self.assertNotIn('OLD_TURN1_STALE_ANSWER', '\n'.join(e.get('text', '') for e in t2_events))
            self.assertEqual(c.close(client, task['id'])['state'], 'closed')

        # Positive 2: Turn 2 live question + dialogue settles under missing resumed boundary without adopting Turn 1 answer
        with self.subTest(case='positive_dialogue'), setup_resumed_cancel('positive_dialogue') as (root, state_dir, c, client, task, t2_task):
            rc2 = []
            t2 = threading.Thread(target=lambda: rc2.append(
                run_task(c.store.path, task['id'], turn=2, launch_id=t2_task['launch_id'], epoch=t2_task.get('control_epoch', 1))))
            t2.start()
            try:
                deadline = time.monotonic() + 5
                q_id = None
                while time.monotonic() < deadline and t2.is_alive():
                    st = c.status(client, task['id'])
                    if st.get('questions'):
                        q_id = st['questions'][0]
                        break
                    time.sleep(0.01)
                self.assertIsNotNone(q_id)
                st_q = c.status(client, task['id'])
                self.assertEqual(st_q['state'], 'running', st_q.get('settlement_note'))
                c.dialogue(client, task['id'], 'Proceed writing resumed.txt', expected_turn=2, command_id='dlg-resumed-1', question_id=q_id)
                t2.join(6)
                self.assertFalse(t2.is_alive())
                st2 = c.status(client, task['id'])
                self.assertEqual(rc2, [0], st2.get('settlement_note'))
                self.assertEqual(st2['state'], 'completed', st2.get('settlement_note'))
                self.assertEqual(st2['native_result']['interaction_seq'], 1)
                self.assertEqual(st2['native_result']['result'], 'Confirmed resumed write: RESUMED_CANCEL_V1.')
                self.assertNotIn('OLD_TURN1_STALE_ANSWER', st2['native_result']['result'])
                self.assertEqual((root / 'resumed.txt').read_text(), 'RESUMED_CANCEL_V1')
                self.assertEqual(c.close(client, task['id'])['state'], 'closed')
            finally:
                if t2.is_alive():
                    c.cancel(client, task['id'])
                    t2.join(3)

        # Negative controls: missing resumed boundary must NEVER adopt old Turn 1 replay or hide Turn 2 failures/conflicts
        neg_modes = (
            'no_live_answer_old_replay',
            'board_nav_only_old_replay',
            'turn2_live_fatal',
            'turn2_unrecovered_tool_failure',
            'turn2_tool_identity_conflict',
            'turn2_tool_status_regressed',
            'turn2_live_interrupted_diagnostic',
        )
        for neg_mode in neg_modes:
            with self.subTest(neg_mode=neg_mode), setup_resumed_cancel(neg_mode) as (root, state_dir, c, client, task, t2_task):
                rc2 = run_task(c.store.path, task['id'], turn=2, launch_id=t2_task['launch_id'], epoch=t2_task.get('control_epoch', 1))
                st2 = c.status(client, task['id'])
                self.assertEqual(rc2, 1)
                self.assertEqual(st2['state'], 'unknown')
                self.assertNotIn('native_result', st2)
                self.assertNotIn('native_cleanup', st2)
                self.assertNotIn('OLD_TURN1_STALE_ANSWER', json.dumps(st2))
                with self.assertRaisesRegex(ValueError, 'already owned'):
                    c.start(client, agent='omp', cwd=str(root), prompt='conflicting writer', write=True)
                self.assertEqual(c.close(client, task['id'])['state'], 'unknown')


