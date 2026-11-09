import unittest
from transport import command, protocol_prompt


class RunnerContractTests(unittest.TestCase):
    def test_local_command_does_not_need_shell_or_put_prompt_in_argv(self):
        args = ['acpx','--cwd','/space name','omp','prompt','--file','-']
        self.assertEqual(command('local', 'posix', args), args)
        self.assertNotIn('SECRET_PROMPT', str(command('local','posix',args)))

    def test_posix_remote_is_quoted_and_windows_uses_encoded_command(self):
        args = ['acpx','--cwd','/a b','omp','prompt','--file','-']
        cmd = command('worker-laptop','posix',args)
        self.assertEqual(cmd[:2], ['ssh','-T'])
        self.assertIn("'/a b'",cmd[-1])
        cmd = command('worker-pc','powershell',['acpx','--cwd','C:\\work & one','pi','prompt','--file','-'])
        self.assertIn('EncodedCommand',cmd[-1])
        self.assertNotIn('C:\\work & one',cmd[-1])
        with self.assertRaises(ValueError):
            command('-oProxyCommand=bad','posix',args)

    def test_prompt_explains_stage_and_question_protocol(self):
        text = protocol_prompt('inspect only')
        self.assertIn('CALLCLI_STAGE ', text)
        self.assertIn('CALLCLI_QUESTION ', text)
        self.assertIn('inspect only', text)
        self.assertNotIn('Hermes delegated', text)
        self.assertIn('CLI collaboration', text)

class RunnerBehaviorTests(unittest.TestCase):
    def setUp(self):
        import os, tempfile, pathlib, sys
        from unittest.mock import patch
        self.tmp=tempfile.TemporaryDirectory(dir=pathlib.Path.home()/'.hermes/cache/scratch')
        self.root=pathlib.Path(self.tmp.name)
        fake=self.root/'acpx'
        fake.write_text('#!'+sys.executable+'\n'+r'''
import sys,json,os,time,pathlib
shared=pathlib.Path(sys.argv[0]).name=='node'
payload=json.loads(sys.stdin.read()) if shared else None
args=['prompt','-s',payload['options']['sessionName']] if shared else sys.argv[1:]
root=pathlib.Path(os.environ['ACPX_FAKE_ROOT'])
if shared:
    with (root/'shared-options.jsonl').open('a')as f:f.write(json.dumps(payload['options'])+'\n')
failed=False
def output(message):
    global failed
    if not shared:
        print(json.dumps(message),flush=True);return
    request=payload['options']['requestId']
    if 'error'in message:failed=True;return
    if 'result'in message:
        if os.environ.get('ACPX_FAKE_RESULT_FRAME'):
            frame=json.loads(os.environ['ACPX_FAKE_RESULT_FRAME'])
            frame.setdefault('requestId',request)
            print(json.dumps(dict(frame,type='callcli.shared.result')),flush=True);return
        stop=message['result']['stopReason']
        print(json.dumps({'type':'callcli.shared.result','requestId':request,'status':'failed'if failed else ('cancelled'if stop=='cancelled'else'completed'),'stopReason':stop}),flush=True);return
    update=message['params']['update']
    if update.get('sessionUpdate') in {'tool_call', 'tool_call_update'}:
        print(json.dumps({'type':'callcli.shared.event','requestId':request,'event':dict(update,type='tool_call',title=update.get('title',''))}),flush=True);return
    print(json.dumps({'type':'callcli.shared.event','requestId':request,'event':{'type':'text_delta','stream':'output','tag':'agent_message_chunk','text':update['content']['text']}}),flush=True)
with (root/'calls.jsonl').open('a') as f:f.write(json.dumps(args)+'\n')
if '--version' in args:
    print(os.environ.get('ACPX_FAKE_VERSION','0.20.0'));raise SystemExit(0)
if 'new' in args:
    print(json.dumps({'acpxRecordId':'11111111-1111-4111-8111-111111111111'}));raise SystemExit(0)
if 'close' in args:raise SystemExit(0)
if 'status' in args:
    mode=os.environ.get('ACPX_STATUS_MODE','no-session')
    if mode == 'transport':raise SystemExit(255)
    if mode == 'malformed':print('not JSON no-session');raise SystemExit(0)
    print(json.dumps({'action':'status_snapshot','status':mode,'summary':'no active session' if mode == 'no-session' else 'running'}))
    raise SystemExit(0)
if 'cancel' in args:
    session=args[args.index('-s')+1]
    (root/('cancel-'+session)).touch()
    raise SystemExit(0)
text=payload['prompt'] if shared else sys.stdin.read()
session=args[args.index('-s')+1]
if 'BLOCK' in text:
    while not (root/('cancel-'+session)).exists():time.sleep(.03)
    mode=os.environ.get('ACPX_CANCEL_MODE','cancelled')
    if mode == 'failed':output({'error':{'code':-1,'message':'failed'}})
    if mode not in {'eof','exit255'}:
        output({'result':{'stopReason':'end_turn' if mode == 'end_turn' else 'cancelled'}})
    raise SystemExit(255 if mode == 'exit255' else 0)
if 'EMPTY' in text:raise SystemExit(0)
for tool in json.loads(os.environ.get('ACPX_FAKE_TOOLS','[]')):
    output({'params':{'update':dict(tool,sessionUpdate='tool_call_update')}})
answer='CALLCLI_QUESTION Which format?\n' if 'ASK' in text else 'CALLCLI_STAGE ready\nFAKE_ANSWER\n'
chunks=json.loads(os.environ['ACPX_FAKE_CHUNKS']) if os.environ.get('ACPX_FAKE_CHUNKS') else [answer[:8],answer[8:]]
for chunk in chunks:
    output({'params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':chunk}}}})
for tool in json.loads(os.environ.get('ACPX_FAKE_TRAILING_TOOLS','[]')):
    output({'params':{'update':dict(tool,sessionUpdate='tool_call_update')}})
if os.environ.get('ACPX_FAKE_RESULT_FAILED'):output({'error':{'code':-1,'message':'transport failed'}})
output({'result':{'stopReason':'end_turn'}})
raise SystemExit(int(os.environ.get('ACPX_FAKE_EXIT','0')))
''')
        fake.chmod(0o700)
        node=self.root/'node';node.write_bytes(fake.read_bytes());node.chmod(0o700)
        self.env=patch.dict(os.environ,{'PATH':str(self.root)+os.pathsep+os.environ['PATH'],'ACPX_FAKE_ROOT':str(self.root)})
        self.env.start()
        self.update=patch('updates.ensure_current',return_value={'decision':'current'})
        self.update.start()
        self.locks=patch('updates.DEFAULT_LOCK_ROOT',self.root/'locks')
        self.locks.start()
        from bridge import Store,Origin
        self.store=Store(self.root/'tasks.sqlite3')
        self.origin=Origin('test','s1','key1')

    def tearDown(self):
        self.locks.stop();self.update.stop();self.env.stop();self.tmp.cleanup()

    def task(self,prompt):
        t=self.store.create(self.origin,'local','omp',str(self.root),prompt,False)
        return self.store.update(t['id'],shell='posix',timeout=30)

    def test_auth_diagnostic_is_task_bound_and_preserves_unknown_results(self):
        import contextlib, io, json, os
        from unittest.mock import patch
        from runner import run_task
        for extra, expected in [
            ({'errorCategory':'auth_required'}, True),
            ({'errorCategory':'auth_required', 'errorSource':'session_watch'}, True),
            ({'errorCategory':'auth_required', 'requestId':'another:1'}, False),
            ({'errorCategory':'PRIVATE_CATEGORY'}, False),
            ({}, False),
            ({'status':'completed', 'stopReason':'end_turn', 'errorCategory':'auth_required'}, False)
        ]:
            with self.subTest(extra=extra):
                task = self.task('normal task')
                frame = dict(status='failed', stopReason=None, message='PRIVATE_MESSAGE')
                frame.update(extra)
                output = io.StringIO()
                with patch.dict(os.environ, {'ACPX_FAKE_RESULT_FRAME':json.dumps(frame)}), contextlib.redirect_stdout(output):
                    run_task(self.store.path, task['id'])
                result = self.store.get(task['id'])
                self.assertEqual(result['state'], 'completed' if frame['status']=='completed' else 'unknown')
                note = result.get('settlement_note') or ''
                self.assertEqual('runtime-reported authentication required' in note, expected)
                if expected:
                    self.assertIn('watchSession prompt error.code=-32000/data.authRequired=true'
                                  if extra.get('errorSource') == 'session_watch'
                                  else 'turn.result.error.detailCode=AUTH_REQUIRED', note)
                    self.assertIn('authentication origin unknown', note)
                    self.assertIn('no automatic replay', note)
                events = self.store.events(task['id'], self.origin)
                self.assertEqual([e['text'] for e in events if e['kind']=='answer'], ['FAKE_ANSWER'])
                self.assertNotIn('PRIVATE', output.getvalue()+json.dumps(result)+json.dumps(events))
                calls = [json.loads(line) for line in (self.root/'calls.jsonl').read_text().splitlines()]
                self.assertEqual(sum('prompt' in c and task['session'] in c for c in calls), 1)

    def test_intervention_advance_keeps_settlement_event_and_cursor(self):
        import contextlib, io, json
        from unittest.mock import patch
        from backends import TurnResult, backend_for
        from bridge import Store
        from runner import run_task
        task = self.task('old task')
        self.store.update(task['id'], needs_session=True, acpx_created=False)
        finish = Store.finish_turn
        observed = []
        def cancelled(store, current, emit):
            self.store.intervene(task['id'], self.origin, 'normal task', 'revision')
            return TurnResult('cancelled', 0, 'cancelled')
        def advance_after_commit(store, *args, **kwargs):
            settled = finish(store, *args, **kwargs)
            observed.extend(self.store.events(task['id'], self.origin))
            resumed, launch = self.store.advance_intervention(task['id'], self.origin)
            self.assertTrue(launch)
            self.assertEqual((resumed['state'], resumed['turn']), ('queued', 2))
            return settled
        output = io.StringIO()
        with patch.object(backend_for('omp'), 'run', cancelled), \
                patch.object(Store, 'finish_turn', advance_after_commit), contextlib.redirect_stdout(output):
            self.assertEqual(run_task(self.store.path, task['id']), 0)
        self.assertEqual([(e['turn'], e['kind']) for e in observed], [(1, 'started'), (1, 'cancelled')])
        frames = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([f['event'] for f in frames], ['cancelled'])
        terminal = frames[0]
        persisted = self.store.events(task['id'], self.origin, terminal['seq']-1)
        self.assertEqual([(e['seq'], e['token'], e['kind']) for e in persisted],
                         [(terminal['seq'], terminal['event_id'], 'cancelled')])
        self.assertIn('resume', terminal['next_action'])
        self.assertFalse(self.store.finish_turn(task['id'], 1, 'cancelled', 0, 'cancelled'))
        with self.assertRaises(ValueError):
            self.store.reply(task['id'], self.origin, '', 'bypass')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_task(self.store.path, task['id']), 0)
        resumed_events = self.store.events(task['id'], self.origin, terminal['seq'])
        self.assertTrue(all(e['turn'] == 2 for e in resumed_events))
        self.assertEqual([e['text'] for e in resumed_events if e['kind'] == 'answer'], ['FAKE_ANSWER'])
        final = self.store.get(task['id'])
        self.assertEqual((final['state'], final['turn'], final['session']), ('completed', 2, task['session']))
        self.assertEqual(final['intervention']['target_turn'], 2)
        self.assertEqual([e['kind'] for e in self.store.events(task['id'], self.origin)].count('cancelled'), 1)

    def test_rejected_runner_settlement_never_broadcasts_a_terminal_event(self):
        import contextlib, io
        from unittest.mock import patch
        from backends import TurnResult, backend_for
        from runner import run_task
        for raises in (False, True):
            with self.subTest(raises=raises):
                task = self.task('old task')
                def already_settled(store, current, emit):
                    store.finish_turn(task['id'], 1, 'completed', 0, 'end_turn')
                    if raises:
                        raise RuntimeError('late error')
                    return TurnResult('cancelled', 0, 'cancelled')
                output = io.StringIO()
                with patch.object(backend_for('omp'), 'run', already_settled), contextlib.redirect_stdout(output):
                    self.assertEqual(run_task(self.store.path, task['id']), 1 if raises else 0)
                self.assertEqual(output.getvalue(), '')
                self.assertEqual(self.store.get(task['id'])['state'], 'completed')
                self.assertEqual([e['kind'] for e in self.store.events(task['id'], self.origin)],
                                 ['started', 'completed'])

    def test_concurrent_close_does_not_replace_terminal_settlement_notification(self):
        import contextlib, io, json
        from unittest.mock import patch
        from bridge import Store
        from runner import run_task
        task = self.task('normal task')
        finish = Store.finish_turn
        def close_after_settlement(store, *args, **kwargs):
            settled = finish(store, *args, **kwargs)
            store.begin_close(task['id'], self.origin)
            return settled
        output = io.StringIO()
        with patch.object(Store, 'finish_turn', close_after_settlement), contextlib.redirect_stdout(output):
            self.assertEqual(run_task(self.store.path, task['id']), 0)
        self.assertEqual(self.store.get(task['id'])['state'], 'closing')
        terminal = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(terminal['event'], 'completed')
        self.assertIn('FAKE_ANSWER', terminal['answer_tail'])

    def test_notifications_resume_by_cursor_and_keep_questions_answers_and_tools(self):
        import contextlib, io, json, os
        from unittest.mock import patch
        from runner import run_task
        task = self.task('ASK')
        tool = {'toolCallId':'read1','title':'read_file','kind':'read','status':'in_progress'}
        output = io.StringIO()
        with patch.dict(os.environ, {'ACPX_FAKE_TOOLS':json.dumps([tool, tool, dict(tool,status='completed')])}), contextlib.redirect_stdout(output):
            self.assertEqual(run_task(self.store.path, task['id']), 0)
        frames = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(any(f['event']=='tool' and 'read_file' in f['text'] for f in frames))
        for frame in frames:
            persisted = self.store.events(task['id'], self.origin, frame['seq']-1)[0]
            self.assertEqual((persisted['seq'], persisted['token']), (frame['seq'], frame['event_id']))
        tools = [e for e in self.store.events(task['id'], self.origin) if e['kind']=='tool']
        self.assertEqual([e['text'] for e in tools], ['read1 read_file read in_progress', 'read1 read_file read completed'])
        question = next(f for f in frames if f['event']=='question')
        self.assertEqual(frames[-1]['question_ids'], [question['event_id']])
        cursor = frames[-1]['seq']
        self.store.reply(task['id'], self.origin, question['event_id'], 'normal task')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(run_task(self.store.path, task['id']), 0)
        resumed = self.store.events(task['id'], self.origin, cursor)
        self.assertTrue(all(e['turn']==2 for e in resumed))
        self.assertEqual([e['text'] for e in resumed if e['kind']=='answer'], ['FAKE_ANSWER'])
        self.assertIn('FAKE_ANSWER', json.loads(output.getvalue().splitlines()[-1])['answer_tail'])


    def test_prompt_uses_shared_runtime_and_persists_native_record_identity(self):
        import json
        from runner import run_task
        t = self.task('normal task')
        self.assertEqual(run_task(self.store.path, t['id']), 0)
        captured = self.root / 'shared-options.jsonl'
        self.assertTrue(captured.exists(), 'Prompt still used lossy raw CLI output')
        options = json.loads(captured.read_text().splitlines()[0])
        self.assertEqual(options['sessionId'], '11111111-1111-4111-8111-111111111111')
        self.assertEqual(options['requestId'], t['id'] + ':1')
        self.assertEqual(options['sessionName'], t['session'])
        self.assertFalse(options['write'])
        self.assertEqual(self.store.get(t['id'])['acpx_record_id'], options['sessionId'])

    def test_reply_uses_current_locked_version_after_compatible_upgrade(self):
        import json, os
        from unittest.mock import patch
        from runner import run_task
        t = self.task('ASK')
        self.store.update(t['id'], version_check={'installed': '0.20.0', 'latest': '0.20.0'})
        self.assertEqual(run_task(self.store.path, t['id']), 0)
        state = self.store.get(t['id'])
        self.store.reply(t['id'], self.origin, state['question_id'], 'JSON please')
        with patch.dict(os.environ, {'ACPX_FAKE_VERSION': '0.20.1'}):
            self.assertEqual(run_task(self.store.path, t['id']), 0)
        frames = [json.loads(x) for x in (self.root / 'shared-options.jsonl').read_text().splitlines()]
        self.assertEqual(frames[1]['expectedVersion'], '0.20.1')
        self.assertEqual(frames[1]['sessionId'], frames[0]['sessionId'])
        self.assertEqual(self.store.get(t['id'])['version_check']['installed'], '0.20.0')
        self.assertEqual(self.store.get(t['id'])['execution_version'], '0.20.1')

    def test_revised_first_turn_creates_session_if_old_worker_never_started(self):
        from runner import run_task
        t=self.task('revised task')
        self.store.update(t['id'],turn=2,needs_session=True,acpx_created=False)
        self.assertEqual(run_task(self.store.path,t['id']),0)
        calls=[__import__('json').loads(l) for l in (self.root/'calls.jsonl').read_text().splitlines()]
        self.assertEqual(sum('new' in c for c in calls),1)
        self.assertTrue(self.store.get(t['id'])['acpx_created'])
        self.assertFalse(self.store.get(t['id'])['needs_session'])

    def test_revised_unlaunched_task_checks_versions_once(self):
        from unittest.mock import patch
        from runner import run_task
        t = self.task('ASK')
        self.store.update(t['id'], turn=2, needs_session=True, acpx_created=False)
        with patch('updates.ensure_current', return_value={'decision':'current'}) as check:
            self.assertEqual(run_task(self.store.path, t['id']), 0)
            state = self.store.get(t['id'])
            self.store.reply(t['id'], self.origin, state['question_id'], 'continue')
            self.assertEqual(run_task(self.store.path, t['id']), 0)
            self.assertEqual(check.call_count, 1)

    def test_real_runner_rejects_empty_exit_zero_and_does_not_replay(self):
        from runner import run_task
        t=self.task('EMPTY')
        self.assertEqual(run_task(self.store.path,t['id']),1)
        self.assertEqual(self.store.get(t['id'])['state'],'unknown')
        before=(self.root/'calls.jsonl').read_text()
        with self.assertRaises(ValueError):run_task(self.store.path,t['id'])
        self.assertEqual((self.root/'calls.jsonl').read_text(),before)

    def test_question_then_answer_runs_same_session_in_distinct_turns(self):
        from runner import run_task
        t=self.task('ASK')
        self.assertEqual(run_task(self.store.path,t['id']),0)
        state=self.store.get(t['id']);self.assertEqual(state['state'],'waiting_reply')
        reply=self.store.reply(t['id'],self.origin,state['question_id'],'JSON please')
        self.assertEqual(run_task(self.store.path,t['id']),0)
        self.assertEqual(self.store.get(t['id'])['state'],'completed')
        self.assertEqual(reply['session'],t['session'])
        calls=[__import__('json').loads(l) for l in (self.root/'calls.jsonl').read_text().splitlines()]
        self.assertEqual(sum('new' in c for c in calls),1)
        self.assertEqual(sum('prompt' in c for c in calls),2)

    # Exact AGY wire chunk observed in real failed turns (prompt result was end_turn).
    AGY_FATAL = '\n\u26a0 Agent execution terminated due to error.\n'

    def run_agy(self, chunks, agent='agy'):
        import json, os
        from unittest.mock import patch
        from runner import run_task
        t = self.store.create(self.origin, 'local', agent, str(self.root), 'normal task', False)
        t = self.store.update(t['id'], shell='posix', timeout=30)
        with patch.dict(os.environ, {'ACPX_FAKE_CHUNKS': json.dumps(chunks)}):
            rc = run_task(self.store.path, t['id'])
        return rc, self.store.get(t['id']), self.store.events(t['id'], self.origin)

    def assert_agy_fatal_not_completed(self, chunks):
        from runner import run_task
        rc, state, events = self.run_agy(chunks)
        self.assertEqual(rc, 1)
        self.assertEqual(state['state'], 'unknown')
        # Protocol ended normally; business outcome is explicitly not accepted.
        self.assertEqual(state['stop_reason'], 'end_turn')
        self.assertEqual(state['exit_code'], 0)
        self.assertIn('AGY reported a fatal execution error', state['settlement_note'])
        final = events[-1]
        self.assertEqual(final['kind'], 'unknown')
        self.assertIn('not completed', final['text'])
        self.assertIn('no automatic replay', final['text'])
        self.assertNotIn('completed', [e['kind'] for e in events])
        # Not replayable: the settled turn cannot be claimed again.
        before = (self.root / 'calls.jsonl').read_text()
        with self.assertRaises(ValueError):
            run_task(self.store.path, state['id'])
        self.assertEqual((self.root / 'calls.jsonl').read_text(), before)
        # Unknown fate refuses same-session continuation but still allows owned close.
        with self.assertRaises(ValueError):
            self.store.intervene(state['id'], self.origin, 'retry it', 'cmd-1')
        with self.assertRaises(ValueError):
            self.store.reply(state['id'], self.origin, state.get('question_id', ''), 'answer')
        self.assertEqual(self.store.begin_close(state['id'], self.origin)['state'], 'closing')
        return state, events

    def test_agy_fatal_error_message_is_not_completed(self):
        self.assert_agy_fatal_not_completed([self.AGY_FATAL])

    def test_agy_fatal_error_split_across_chunks_is_not_completed(self):
        self.assert_agy_fatal_not_completed(['\n\u26a0 Agent exec', 'ution terminated due', ' to error.', '\n'])

    def test_agy_fatal_error_after_partial_output_is_not_completed(self):
        self.assert_agy_fatal_not_completed(['CALLCLI_STAGE editing\npartial result', self.AGY_FATAL])

    def test_agy_fatal_error_after_question_is_not_waiting_reply(self):
        state, _ = self.assert_agy_fatal_not_completed(['CALLCLI_QUESTION Which format?\n', self.AGY_FATAL])
        self.assertNotEqual(state['state'], 'waiting_reply')

    def test_agy_normal_and_similar_text_still_complete(self):
        cases = [
            ['CALLCLI_STAGE ready\n', 'FAKE_ANSWER\n'],
            ['The old log showed "\u26a0 Agent execution terminated due to error." and is fixed.\n'],
            ['\u26a0 Agent execution terminated due to error.\n', 'Recovered after rerun of tests; DONE\n'],
            ['\u26a0 Agent execution terminated due to error. Retried successfully.\n'],
            ['Agent execution terminated due to errors in the fixture, now fixed.\n'],
        ]
        for chunks in cases:
            with self.subTest(chunks=chunks):
                rc, state, events = self.run_agy(chunks)
                self.assertEqual((rc, state['state']), (0, 'completed'))
                self.assertIsNone(state.get('settlement_note'))

    def test_fatal_marker_is_scoped_to_agy(self):
        rc, state, _ = self.run_agy([self.AGY_FATAL], agent='omp')
        self.assertEqual((rc, state['state']), (0, 'completed'))
    def test_agy_failed_tool_without_final_answer_is_unknown_not_completed(self):
        import os, json
        from unittest.mock import patch
        from runner import run_task
        for chunks in ([], ['CALLCLI_STAGE attempting shell\n']):
            with self.subTest(chunks=chunks), patch.dict(os.environ, {'ACPX_FAKE_TOOLS':json.dumps([
                    {'toolCallId':'shell1','title':'run_command','kind':'execute','status':'failed',
                     'rawInput':'PRIVATE_INPUT','rawOutput':'PRIVATE_OUTPUT'}])}):
                rc, state, events = self.run_agy(chunks)
                self.assertEqual((rc, state['state'], state['stop_reason'], state['exit_code']),
                                 (1, 'unknown', 'end_turn', 0))
                self.assertIn('tool failed without a final answer', state['settlement_note'])
                self.assertIn('permission decisions are not observable', state['settlement_note'])
                tools = [e['text'] for e in events if e['kind']=='tool']
                self.assertTrue(any('run_command' in text and 'execute' in text and 'failed' in text for text in tools))
                self.assertNotIn('PRIVATE', json.dumps(events))
                with self.assertRaises(ValueError):
                    run_task(self.store.path, state['id'])

    def test_agy_transport_failure_keeps_direct_failure_explanation(self):
        import os, json
        from unittest.mock import patch
        for transport in ({'ACPX_FAKE_EXIT':'1'}, {'ACPX_FAKE_RESULT_FAILED':'1'}):
            with self.subTest(transport=transport), patch.dict(os.environ, dict(transport,
                    ACPX_FAKE_TOOLS=json.dumps([{'toolCallId':'shell1','kind':'execute','status':'failed'}]))):
                rc, state, events = self.run_agy(['CALLCLI_STAGE attempting shell\n'])
                self.assertEqual((rc, state['state']), (1, 'unknown'))
                self.assertIsNone(state.get('settlement_note'))
                self.assertEqual(events[-1]['text'], 'Prompt exited '+str(state['exit_code']))
                self.assertNotIn('permission', events[-1]['text'])

    def test_agy_fatal_line_retains_precedence_on_nonzero_transport_exit(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {'ACPX_FAKE_EXIT':'1'}):
            rc, state, _ = self.run_agy([self.AGY_FATAL])
        self.assertEqual((rc, state['state'], state['exit_code']), (1, 'unknown', 1))
        self.assertIn('fatal execution error', state['settlement_note'])

    def test_duplicate_tool_status_after_answer_still_invalidates_final_answer_window(self):
        import os, json
        from unittest.mock import patch
        failed = {'toolCallId':'shell1','kind':'execute','status':'failed'}
        with patch.dict(os.environ, {'ACPX_FAKE_TOOLS':json.dumps([failed]),
                                    'ACPX_FAKE_TRAILING_TOOLS':json.dumps([failed])}):
            rc, state, events = self.run_agy(['Partial result before repeated tool update.\n'])
        self.assertEqual((rc, state['state']), (1, 'unknown'))
        self.assertIn('tool failed without a final answer', state['settlement_note'])
        self.assertEqual([e['text'] for e in events if e['kind']=='tool'], ['shell1 execute failed'])

    def test_agy_tool_failure_can_recover_with_final_answer(self):
        import os, json
        from unittest.mock import patch
        with patch.dict(os.environ, {'ACPX_FAKE_TOOLS':json.dumps([
                {'toolCallId':'read1','title':'read_file','kind':'read','status':'failed'},
                {'toolCallId':'read2','title':'read_file','kind':'read','status':'completed'}])}):
            rc, state, _ = self.run_agy(['Recovered using alternate file; result is 42.\n'])
            self.assertEqual((rc, state['state']), (0, 'completed'))
            self.assertIsNone(state.get('settlement_note'))

    def test_agy_failed_tool_can_ask_and_receive_reply(self):
        import os, json
        from unittest.mock import patch
        from runner import run_task
        with patch.dict(os.environ, {'ACPX_FAKE_TOOLS':json.dumps([
                {'toolCallId':'read1','title':'read_file','kind':'read','status':'failed'}])}):
            rc, state, _ = self.run_agy(['CALLCLI_QUESTION Which file should I read?\n'])
        self.assertEqual((rc, state['state']), (0, 'waiting_reply'))
        self.store.reply(state['id'], self.origin, state['question_id'], 'Use the supplied answer instead')
        self.assertEqual(run_task(self.store.path, state['id']), 0)
        self.assertEqual(self.store.get(state['id'])['state'], 'completed')


    def test_cancelling_one_parallel_task_does_not_cancel_the_other(self):
        import concurrent.futures,time
        from runner import run_task
        a=self.task('BLOCK');b=self.task('BLOCK')
        with concurrent.futures.ThreadPoolExecutor() as pool:
            fa=pool.submit(run_task,self.store.path,a['id']);fb=pool.submit(run_task,self.store.path,b['id'])
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if (self.root/'calls.jsonl').exists() and (self.root/'calls.jsonl').read_text().count('prompt')>=2:break
                time.sleep(.03)
            self.store.update(a['id'],state='cancelling')
            self.assertEqual(fa.result(timeout=5),0)
            self.assertEqual(self.store.get(a['id'])['state'],'cancelled')
            self.assertFalse(fb.done())
            self.store.update(b['id'],state='cancelling')
            self.assertEqual(fb.result(timeout=5),0)


if __name__ == '__main__':
    unittest.main()
