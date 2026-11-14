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
if shared and pathlib.Path(sys.argv[1]).name=='release-backend.cjs':
    root=pathlib.Path(os.environ['ACPX_FAKE_ROOT'])
    operation=sys.argv[2]
    with (root/'calls.jsonl').open('a') as f:f.write(json.dumps([operation,'-s',payload['session']])+'\n')
    if operation=='resolve':print(json.dumps({'acp':'fixture-acp'}));raise SystemExit(0)
    if operation=='close':print(json.dumps({'verified':True}));raise SystemExit(0)
    mode=os.environ.get('ACPX_STATUS_MODE','no-session')
    if mode=='transport':raise SystemExit(255)
    if mode=='malformed':print('not JSON no-session');raise SystemExit(0)
    binding=dict(acpxRecordId=payload['record'],acpxSessionId=payload['acp'],session=payload['session'],agent=payload['agent'],cwd=payload['cwd'])
    print(json.dumps(dict(verified=mode=='no-session',binding=binding,record=dict(closed=mode=='no-session',lastRequestId=payload.get('request_id',payload['task']+':'+str(payload['turn']))),status=mode,source='shared-runtime-exact-record-probe',arbitraryDetachedDescendantsProven=False)));raise SystemExit(0)
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
if shared and not os.environ.get('ACPX_FAKE_NO_START'):
    print(json.dumps({'type':'callcli.shared.event','requestId':payload['options']['requestId'],'event':{'type':'prompt_started'}}),flush=True)
if 'BLOCK' in text:
    while not (root/('cancel-'+session)).exists():
        activity=root/('activity-'+session)
        if activity.exists():
            value=activity.read_text();activity.unlink()
            output({'params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':value}}}})
        tool=root/('tool-'+session)
        if tool.exists():
            tool.unlink()
            output({'params':{'update':{'sessionUpdate':'tool_call_update','toolCallId':'read1','title':'read_file','kind':'read','status':'in_progress'}}})
        time.sleep(.03)
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
                    __import__('test_dispatch').run_dispatched(self.store.path, task['id'])
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

    def test_runtime_diagnostics_require_exact_category_source_and_keep_unknown(self):
        import contextlib, io, json, os
        from unittest.mock import patch
        from runner import run_task
        for category, source, expected in [
            ('timeout', 'turn_result_code', 'runtime-reported prompt deadline'),
            ('connection_lost', 'turn_result_detail', 'runtime-reported agent connection loss'),
            ('timeout', 'turn_result_detail', None),
            ('connection_lost', 'turn_result_code', None),
            ('timeout', None, None),
            ('auth_required', 'PRIVATE_SOURCE', None),
            ('PRIVATE_CATEGORY', 'turn_result_code', None)
        ]:
            with self.subTest(category=category, source=source):
                task = self.task('normal task')
                frame = dict(status='failed', stopReason=None, errorCategory=category, errorSource=source, message='PRIVATE_MESSAGE')
                output = io.StringIO()
                with patch.dict(os.environ, {'ACPX_FAKE_RESULT_FRAME':json.dumps(frame)}), contextlib.redirect_stdout(output):
                    __import__('test_dispatch').run_dispatched(self.store.path, task['id'])
                result = self.store.get(task['id'])
                self.assertEqual(result['state'], 'unknown')
                note = result.get('settlement_note') or ''
                if expected:
                    self.assertIn(expected, note)
                    self.assertIn('reconcile', note)
                    self.assertIn('no automatic replay', note)
                    if category == 'timeout':
                        self.assertIn('configured timeout 30s', note)
                    else:
                        self.assertIn('cause unknown', note)
                else:
                    self.assertNotIn('runtime-reported', note)
                events = self.store.events(task['id'], self.origin)
                self.assertNotIn('PRIVATE', output.getvalue()+json.dumps(result)+json.dumps(events))
                calls = [json.loads(line) for line in (self.root/'calls.jsonl').read_text().splitlines()]
                self.assertEqual(sum('prompt' in c and task['session'] in c for c in calls), 1)

    def test_runtime_diagnostics_ignore_foreign_and_success_frames(self):
        from events import EventDecoder
        for category, source in [('timeout','turn_result_code'), ('connection_lost','turn_result_detail')]:
            for status, request_id in [('failed','foreign:1'), ('completed','owned:1'), ('cancelled','owned:1')]:
                with self.subTest(category=category, status=status, request_id=request_id):
                    decoder = EventDecoder(shared_request_id='owned:1')
                    decoder.feed(dict(type='callcli.shared.result', requestId=request_id, status=status,
                                      stopReason={'completed':'end_turn','cancelled':'cancelled'}.get(status),
                                      errorCategory=category, errorSource=source))
                    self.assertIsNone(decoder.error_category)
                    self.assertIsNone(decoder.error_source)

    def test_review_checkpoints_share_quiet_cooldown_and_never_cancel(self):
        self._timed_turn_scenario('review')

    def test_cancel_committed_before_review_insertion_suppresses_notice(self):
        import contextlib, io, json, time
        from concurrent.futures import ThreadPoolExecutor
        from types import SimpleNamespace
        from unittest.mock import patch
        from bridge import Store
        from runner import run_task
        task = self.task('BLOCK')
        self.store.update(task['id'], timeout=None)
        now = [0.0]
        original = Store.event
        def cancel_before_notice(store, task_id, token, kind, text='', **metadata):
            if metadata.get('quiet_notice'):
                store.request_cancel(task_id, self.origin)
            return original(store, task_id, token, kind, text, **metadata)
        output = io.StringIO()
        with patch('acpx_backend.time', SimpleNamespace(monotonic=lambda: now[0])), \
                patch.object(Store, 'event', cancel_before_notice), contextlib.redirect_stdout(output), \
                ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(__import__('test_dispatch').run_dispatched, self.store.path, task['id'])
            try:
                until = time.monotonic()+5
                while not (self.root/'shared-options.jsonl').exists() and time.monotonic()<until:
                    time.sleep(.02)
                time.sleep(.35)
                now[0] = 601
                self.assertEqual(future.result(timeout=5), 0)
            finally:
                if not future.done():
                    self.store.request_cancel(task['id'], self.origin)
                    future.result(timeout=5)
        self.assertEqual(self.store.get(task['id'])['state'], 'cancelled')
        self.assertFalse(any(e.get('quiet_notice') for e in self.store.events(task['id'], self.origin)))
        self.assertFalse(any(json.loads(line).get('quiet_notice') for line in output.getvalue().splitlines()))

    def test_terminal_receipt_precedes_review_insertion(self):
        import contextlib, io, json, threading
        from types import SimpleNamespace
        from unittest.mock import patch
        from runner import run_task
        task=self.task('normal task');self.store.update(task['id'],timeout=1000)
        parsing=threading.Event();release=threading.Event();loads=json.loads;ticks=[0]
        def parse(value,*args,**kwargs):
            if threading.current_thread().name=='callcli-transport-reader' and 'callcli.shared.result' in value:
                parsing.set();self.assertTrue(release.wait(3))
            return loads(value,*args,**kwargs)
        def clock():
            ticks[0]+=1
            if ticks[0]==1:return 0
            self.assertTrue(parsing.wait(3))
            if ticks[0]==2:threading.Timer(.2,release.set).start()
            return 601
        with patch('acpx_backend.json.loads',side_effect=parse), \
                patch('acpx_backend.time',SimpleNamespace(monotonic=clock)), \
                contextlib.redirect_stdout(io.StringIO()):
            rc=__import__('test_dispatch').run_dispatched(self.store.path, task['id'])
        self.assertEqual((self.store.get(task['id'])['state'],rc),('completed',0))
        self.assertFalse(any(e.get('quiet_notice') for e in self.store.events(task['id'],self.origin)))

    def test_unbudgeted_startup_is_bounded_without_claiming_runtime_timeout(self):
        self._timed_turn_scenario('startup')

    def test_explicit_outer_deadline_keeps_existing_cancellation(self):
        self._timed_turn_scenario('explicit')

    def test_duplicate_tool_updates_do_not_postpone_quiet_reminder(self):
        self._timed_turn_scenario('duplicate_tool')

    def test_large_payload_cancel_is_supervised_before_stdin_finishes(self):
        self._blocked_payload_scenario(False)

    def test_large_payload_startup_deadline_is_supervised_before_stdin_finishes(self):
        self._blocked_payload_scenario(True)

    def test_blocked_payload_cancel_failure_converges_transport(self):
        self._blocked_payload_scenario(False, True)

    def _blocked_payload_scenario(self, startup, cancel_failure=False):
        import contextlib, io, json, subprocess, sys, threading, time
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from types import SimpleNamespace
        from runner import run_task
        task=self.task('x'*2000000)
        self.store.update(task['id'],timeout=None)
        marker=self.root/'pipe-child-started'
        (self.root/'node').write_text('#!'+sys.executable+'\nimport pathlib,time\npathlib.Path('+repr(str(marker))+').touch()\ntime.sleep(10)\n')
        children=[];popen=subprocess.Popen
        def launch(*args,**kwargs):
            child=popen(*args,**kwargs)
            if args[0][0]=='node':children.append(child)
            return child
        before=set(threading.enumerate())
        from backends import backend_for
        backend=backend_for('omp');control=backend.control
        def cancel_control(current,action,timeout=20):
            result=control(current,action,timeout)
            if action=='cancel' and cancel_failure:result.returncode=1
            return result
        now=[0.0]
        with patch('acpx_backend.subprocess.Popen',side_effect=launch), \
                patch('acpx_backend.time',SimpleNamespace(monotonic=lambda:now[0])), \
                patch.object(backend,'control',side_effect=cancel_control), \
                contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(__import__('test_dispatch').run_dispatched, self.store.path, task['id'])
            try:
                until=time.monotonic()+3
                while not marker.exists() and time.monotonic()<until:time.sleep(.01)
                self.assertTrue(marker.exists())
                if startup:now[0]=121
                else:self.store.request_cancel(task['id'],self.origin)
                until=time.monotonic()+2
                cancel=self.root/('cancel-'+task['session'])
                while not cancel.exists() and not future.done() and time.monotonic()<until:time.sleep(.01)
                self.assertTrue(cancel.exists(),'backpressure prevented cancel control')
                future.result(timeout=2)
                result=self.store.get(task['id'])
                self.assertEqual(result['state'],'unknown')
                if startup:self.assertIn('prompt acceptance not observed',result['settlement_note'])
                self.assertTrue(children and all(child.poll() is not None for child in children))
            finally:
                for child in children:
                    if child.poll() is None:child.terminate()
                future.result(timeout=5)
        self.assertFalse([t for t in threading.enumerate() if t not in before and t.name.startswith('callcli-transport-')])

    def test_received_acceptance_prevents_delayed_startup_cancellation(self):
        self._received_frame_scenario(None)

    def test_received_terminal_preserves_failure_diagnostics_at_startup_deadline(self):
        for category,source in [('timeout','turn_result_code'),('connection_lost','turn_result_detail')]:
            with self.subTest(category=category):self._received_frame_scenario((category,source))

    def _received_frame_scenario(self, diagnostic):
        import contextlib, io, json, os, queue, threading
        from types import SimpleNamespace
        from unittest.mock import patch
        from runner import run_task
        task=self.task('normal task');self.store.update(task['id'],timeout=None)
        received=threading.Event();base=queue.Queue
        class ObservedQueue(base):
            def put(self,item,*args,**kwargs):
                result=super().put(item,*args,**kwargs)
                if isinstance(item,bytes) and (b'callcli.shared.result' if diagnostic else b'prompt_started') in item:received.set()
                return result
        ticks=[0]
        def clock():
            ticks[0]+=1
            if ticks[0]==1:return 0
            self.assertTrue(received.wait(3),'reader must receive the request fact before deadline decision')
            return 121
        env={'ACPX_FAKE_NO_START':'1' if diagnostic else ''}
        if diagnostic:env['ACPX_FAKE_RESULT_FRAME']=json.dumps(dict(status='failed',stopReason=None,errorCategory=diagnostic[0],errorSource=diagnostic[1]))
        with patch('acpx_backend.time',SimpleNamespace(monotonic=clock)), patch('acpx_backend.queue.Queue',ObservedQueue), \
                patch.dict(os.environ,env),contextlib.redirect_stdout(io.StringIO()):
            rc=__import__('test_dispatch').run_dispatched(self.store.path, task['id'])
        self.assertFalse((self.root/('cancel-'+task['session'])).exists(),'received fact lost to deadline decision')
        result=self.store.get(task['id']);note=result.get('settlement_note') or ''
        self.assertEqual((result['state'],rc),('unknown',1) if diagnostic else ('completed',0))
        self.assertNotIn('startup cancellation',note)
        if diagnostic:self.assertIn('runtime-reported',note)

    def test_terminal_after_startup_cancel_preserves_runtime_diagnostic(self):
        import contextlib, io, json, os, time
        from types import SimpleNamespace
        from unittest.mock import patch
        from backends import backend_for
        from runner import run_task
        task=self.task('BLOCK');self.store.update(task['id'],timeout=None)
        backend=backend_for('omp');control=backend.control
        def cancel(current,action,timeout=20):
            if action=='cancel':
                until=time.monotonic()+3
                while not (self.root/'shared-options.jsonl').exists() and time.monotonic()<until:time.sleep(.01)
                time.sleep(.1)
            return control(current,action,timeout)
        ticks=[0]
        def clock():
            ticks[0]+=1
            return 0 if ticks[0]==1 else 121
        frame=dict(status='failed',stopReason=None,errorCategory='connection_lost',errorSource='turn_result_detail')
        with patch('acpx_backend.time',SimpleNamespace(monotonic=clock)),patch.object(backend,'control',side_effect=cancel), \
                patch.dict(os.environ,{'ACPX_FAKE_NO_START':'1','ACPX_FAKE_RESULT_FRAME':json.dumps(frame)}), \
                contextlib.redirect_stdout(io.StringIO()):rc=__import__('test_dispatch').run_dispatched(self.store.path, task['id'])
        result=self.store.get(task['id'])
        self.assertEqual((result['state'],rc),('unknown',1))
        self.assertIn('runtime-reported agent connection loss',result['settlement_note'])
        self.assertNotIn('startup cancellation',result['settlement_note'])

    def test_output_backlog_cannot_starve_cancel_supervision(self):
        import contextlib, io, queue
        from unittest.mock import patch
        from runner import run_task
        task=self.task('normal task');self.store.update(task['id'],timeout=None)
        base=queue.Queue;store=self.store;origin=self.origin;drains=[0]
        class BackloggedQueue(base):
            def get_nowait(self):
                drains[0]+=1
                if drains[0]==1:store.request_cancel(task['id'],origin)
                if drains[0]<10000:
                    return b'{}\n'
                return super().get_nowait()
        with patch('acpx_backend.queue.Queue',BackloggedQueue),contextlib.redirect_stdout(io.StringIO()):
            __import__('test_dispatch').run_dispatched(self.store.path, task['id'])
        self.assertTrue((self.root/('cancel-'+task['session'])).exists())
        self.assertLess(drains[0],10000,'unbounded output drain postponed supervision until producer stopped')

    def test_reader_frame_parse_and_startup_decision_are_serialized(self):
        import contextlib, io, json, threading
        from types import SimpleNamespace
        from unittest.mock import patch
        from runner import run_task
        task=self.task('normal task');self.store.update(task['id'],timeout=None)
        parsing=threading.Event();release=threading.Event();loads=json.loads;ticks=[0]
        def parse(value,*args,**kwargs):
            if threading.current_thread().name=='callcli-transport-reader' and 'prompt_started' in value:
                parsing.set();self.assertTrue(release.wait(3))
            return loads(value,*args,**kwargs)
        def clock():
            ticks[0]+=1
            if ticks[0]==1:return 0
            if ticks[0]==2:
                self.assertTrue(parsing.wait(3))
                threading.Timer(.1,release.set).start()
            return 121
        with patch('acpx_backend.json.loads',side_effect=parse),patch('acpx_backend.time',SimpleNamespace(monotonic=clock)), \
                contextlib.redirect_stdout(io.StringIO()):rc=__import__('test_dispatch').run_dispatched(self.store.path, task['id'])
        self.assertEqual((self.store.get(task['id'])['state'],rc),('completed',0))
        self.assertFalse((self.root/('cancel-'+task['session'])).exists())

    def test_terminal_is_drained_when_writer_completion_signal_is_delayed(self):
        import contextlib, io, json, os, threading, time
        from types import SimpleNamespace
        from unittest.mock import patch
        from backends import backend_for
        from runner import run_task
        task=self.task('BLOCK');self.store.update(task['id'],timeout=None)
        completion=threading.Event();terminal=threading.Event();created=[0];ticks=[0]
        class DelayedCompletion:
            def __init__(self):self.event=threading.Event()
            def set(self):completion.wait(3);self.event.set()
            def is_set(self):return self.event.is_set()
        def event():
            created[0]+=1
            return DelayedCompletion() if created[0]==1 else threading.Event()
        def clock():
            ticks[0]+=1
            return 0 if ticks[0]==1 else 121
        backend=backend_for('omp');control=backend.control
        def cancel(current,action,timeout=20):
            if action!='cancel':return control(current,action,timeout)
            until=time.monotonic()+3
            while not (self.root/'shared-options.jsonl').exists() and time.monotonic()<until:time.sleep(.01)
            result=control(current,action,timeout)
            self.assertTrue(terminal.wait(3))
            threading.Timer(.2,completion.set).start()
            return result
        import queue
        base=queue.Queue
        class ObservedQueue(base):
            def put(self,item,*args,**kwargs):
                result=super().put(item,*args,**kwargs)
                if isinstance(item,bytes) and b'callcli.shared.result' in item:terminal.set()
                return result
        frame=dict(status='failed',stopReason=None,errorCategory='connection_lost',errorSource='turn_result_detail')
        transport_threads=SimpleNamespace(Event=event,Lock=threading.Lock,Thread=threading.Thread)
        with patch('acpx_backend.threading',transport_threads),patch('acpx_backend.time',SimpleNamespace(monotonic=clock)), \
                patch('acpx_backend.queue.Queue',ObservedQueue),patch.object(backend,'control',side_effect=cancel), \
                patch.dict(os.environ,{'ACPX_FAKE_NO_START':'1','ACPX_FAKE_RESULT_FRAME':json.dumps(frame)}), \
                contextlib.redirect_stdout(io.StringIO()):rc=__import__('test_dispatch').run_dispatched(self.store.path, task['id'])
        result=self.store.get(task['id'])
        self.assertEqual((result['state'],rc),('unknown',1))
        self.assertIn('runtime-reported agent connection loss',result['settlement_note'])
        self.assertNotIn('startup cancellation',result['settlement_note'])

    def test_transport_reads_success_with_high_numbered_stdout_pipe(self):
        import contextlib, fcntl, io, os, subprocess
        from types import SimpleNamespace
        from unittest.mock import patch
        from runner import run_task
        task=self.task('normal task');self.store.update(task['id'],timeout=1000)
        popen=subprocess.Popen;ticks=[0]
        def launch(*args,**kwargs):
            child=popen(*args,**kwargs)
            if args[0][0]=='node':
                high=fcntl.fcntl(child.stdout.fileno(),fcntl.F_DUPFD,1100)
                child.stdout.close();child.stdout=os.fdopen(high,'rb')
            return child
        def clock():
            ticks[0]+=1
            return 0 if ticks[0]==1 else 121
        with patch('acpx_backend.subprocess.Popen',side_effect=launch), \
                patch('acpx_backend.time',SimpleNamespace(monotonic=clock)),contextlib.redirect_stdout(io.StringIO()):
            rc=__import__('test_dispatch').run_dispatched(self.store.path, task['id'])
        self.assertEqual((self.store.get(task['id'])['state'],rc),('completed',0))

    def test_descendant_held_pipes_do_not_block_cancel_failure_cleanup(self):
        self._held_transport_scenario('descendant')

    def test_terminal_without_transport_exit_has_bounded_cleanup(self):
        self._held_transport_scenario('completed')

    def test_failed_terminal_without_exit_keeps_runtime_diagnostic(self):
        self._held_transport_scenario('connection_lost')

    def _held_transport_scenario(self, mode):
        import contextlib, io, json, os, signal, subprocess, sys, threading, time
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from runner import run_task
        from backends import backend_for
        task=self.task('x'*2000000 if mode=='descendant' else 'normal task')
        self.store.update(task['id'],timeout=None,write=True)
        marker=self.root/'held-transport.json'
        source='import os,sys,json,pathlib,time\n'
        if mode=='descendant':
            source+='child=os.fork()\nif child==0:\n time.sleep(12);os._exit(0)\n'
            source+='pathlib.Path('+repr(str(marker))+').write_text(json.dumps([os.getpid(),child]))\ntime.sleep(12)\n'
        else:
            frame=dict(type='callcli.shared.result',status='completed',stopReason='end_turn')
            if mode!='completed':frame.update(status='failed',stopReason=None,errorCategory='connection_lost',errorSource='turn_result_detail')
            source+='p=json.loads(sys.stdin.read());r=p["options"]["requestId"]\n'
            source+='print(json.dumps(dict(type="callcli.shared.event",requestId=r,event=dict(type="text_delta",stream="output",tag="agent_message_chunk",text="SYNTHETIC_RESULT\\n"))),flush=True)\n'
            source+='f='+repr(frame)+';f["requestId"]=r;print(json.dumps(f),flush=True)\n'
            source+='pathlib.Path('+repr(str(marker))+').write_text(json.dumps([os.getpid()]))\ntime.sleep(12)\n'
        (self.root/'node').write_text('#!'+sys.executable+'\n'+source)
        backend=backend_for('omp');control=backend.control
        def cancel(current,action,timeout=20):
            result=control(current,action,timeout)
            if action=='cancel' and mode=='descendant':result.returncode=1
            return result
        before=set(threading.enumerate());pids=[];start=time.monotonic()
        with patch.object(backend,'control',side_effect=cancel), \
                patch('acpx_backend.TRANSPORT_CLEANUP_TIMEOUT',.3,create=True), \
                contextlib.redirect_stdout(io.StringIO()),ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(__import__('test_dispatch').run_dispatched, self.store.path, task['id'])
            try:
                until=time.monotonic()+3
                while not marker.exists() and time.monotonic()<until:time.sleep(.01)
                self.assertTrue(marker.exists());pids=json.loads(marker.read_text())
                if mode=='descendant':self.store.request_cancel(task['id'],self.origin)
                until=time.monotonic()+3
                while not future.done() and time.monotonic()<until:time.sleep(.01)
                self.assertTrue(future.done(),'transport cleanup waited for inherited pipes or EOF')
                self.assertEqual(future.result(),1)
                result=self.store.get(task['id']);self.assertEqual(result['state'],'unknown')
                if mode!='descendant':
                    self.assertIn('transport cleanup',result['settlement_note'])
                    self.assertFalse((self.root/('cancel-'+task['session'])).exists())
                if mode=='connection_lost':self.assertIn('runtime-reported agent connection loss',result['settlement_note'])
                with self.assertRaises(ValueError):self.store.create(self.origin,'local','omp',str(self.root),'conflicting writer',True)
                self.assertFalse([t for t in threading.enumerate() if t not in before and t.name.startswith('callcli-transport-')])
                if mode=='descendant':os.kill(pids[1],0)  # IO shutdown must not kill the pipe holder.
            finally:
                for pid in pids:
                    try:os.kill(pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                future.result(timeout=6)

    def test_terminal_diagnostic_survives_slow_public_event_backlog(self):
        import contextlib, io, json, os, threading, time
        from unittest.mock import patch
        from bridge import Store
        from runner import run_task
        task=self.task('normal task');self.store.update(task['id'],timeout=None)
        event=Store.event
        def slow(store,task_id,token,kind,text='',**metadata):
            if kind=='stage':time.sleep(.01)
            return event(store,task_id,token,kind,text,**metadata)
        frame=dict(status='failed',stopReason=None,errorCategory='connection_lost',errorSource='turn_result_detail')
        with patch.object(Store,'event',slow),patch('acpx_backend.TRANSPORT_CLEANUP_TIMEOUT',.02), \
                patch.dict(os.environ,{'ACPX_FAKE_RESULT_FRAME':json.dumps(frame),
                                     'ACPX_FAKE_CHUNKS':json.dumps(['CALLCLI_STAGE row'+str(n)+'\n' for n in range(400)])}), \
                contextlib.redirect_stdout(io.StringIO()):rc=__import__('test_dispatch').run_dispatched(self.store.path, task['id'])
        result=self.store.get(task['id'])
        self.assertEqual((result['state'],rc),('unknown',1))
        self.assertIn('runtime-reported agent connection loss',result.get('settlement_note') or '')

    def test_successful_transport_preserves_slow_answer_tail_in_order(self):
        self._ordered_tail_scenario(False)

    def test_successful_transport_preserves_question_handles_after_backlog(self):
        self._ordered_tail_scenario(True)

    def _ordered_tail_scenario(self, questions):
        import contextlib, io, json, os, time
        from unittest.mock import patch
        from bridge import Store
        from runner import run_task
        task=self.task('normal task');self.store.update(task['id'],timeout=None)
        event=Store.event
        def slow(store,task_id,token,kind,text='',**metadata):
            if kind=='stage':time.sleep(.02)
            return event(store,task_id,token,kind,text,**metadata)
        tail=['FINAL_TAIL_MUST_SURVIVE\n']
        if questions:tail+=['CALLCLI_QUESTION Q1 First choice?\n','CALLCLI_QUESTION Q2 Second choice?\n']
        chunks=['CALLCLI_STAGE row'+str(n)+'\n' for n in range(400)]+tail
        with patch.object(Store,'event',slow),patch.dict(os.environ,{'ACPX_FAKE_CHUNKS':json.dumps(chunks)}), \
                contextlib.redirect_stdout(io.StringIO()):rc=__import__('test_dispatch').run_dispatched(self.store.path, task['id'])
        result=self.store.get(task['id']);events=self.store.events(task['id'],self.origin)
        self.assertEqual((result['state'],rc),('waiting_reply',0) if questions else ('completed',0))
        self.assertFalse(result.get('settlement_note'))
        self.assertEqual([e['text'] for e in events if e['kind']=='stage'],['row'+str(n) for n in range(400)])
        self.assertEqual([e['text'] for e in events if e['kind']=='answer'],['FINAL_TAIL_MUST_SURVIVE'])
        if questions:
            pending=[e for e in events if e['kind']=='question']
            self.assertEqual([e['text'] for e in pending],['Q1 First choice?','Q2 Second choice?'])
            self.assertEqual(result['questions'],[e['token'] for e in pending])
            self.assertLess(next(e['seq'] for e in events if e['kind']=='answer'),pending[0]['seq'])
            self.assertLess(pending[0]['seq'],pending[1]['seq'])

    def _timed_turn_scenario(self, mode):
        import contextlib, io, json, os, time
        from concurrent.futures import ThreadPoolExecutor
        from types import SimpleNamespace
        from unittest.mock import patch
        from runner import run_task
        task = self.task('BLOCK')
        self.store.update(task['id'], timeout=30 if mode=='explicit' else None)
        now = [0.0]
        output = io.StringIO()
        def wait_for(predicate):
            until = time.monotonic()+5
            while time.monotonic()<until:
                if predicate():return
                time.sleep(.02)
            self.fail('timed turn did not reach expected public state')
        def events():return self.store.events(task['id'], self.origin)
        def reminders():return [e for e in events() if e.get('quiet_notice')]
        with patch('acpx_backend.time', SimpleNamespace(monotonic=lambda:now[0])), \
                patch.dict(os.environ, {'ACPX_FAKE_NO_START':'1' if mode=='startup' else ''}), \
                contextlib.redirect_stdout(output), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(__import__('test_dispatch').run_dispatched, self.store.path, task['id'])
            try:
                wait_for(lambda:(self.root/'shared-options.jsonl').exists())
                time.sleep(.35)
                if mode=='review':
                    now[0]=590
                    (self.root/('activity-'+task['session'])).write_text('CALLCLI_STAGE active work\n')
                    wait_for(lambda:any(e['text']=='active work' for e in events()))
                    now[0]=601
                    wait_for(lambda:len(reminders())==1)
                    first = reminders()[0]
                    self.assertEqual((first['notice_kind'], first['elapsed'], first['turn']),
                                     ('checkpoint', 601, 1))
                    self.assertIn('active work', first['text'])
                    self.assertEqual(self.store.get(task['id'])['state'],'running')
                    self.assertFalse((self.root/('cancel-'+task['session'])).exists())
                    now[0]=1191;time.sleep(.35)
                    self.assertEqual(len(reminders()),1)
                    now[0]=1202
                    wait_for(lambda:len(reminders())==2)
                    self.assertEqual(reminders()[1]['notice_kind'], 'quiet')
                    self.assertFalse((self.root/('cancel-'+task['session'])).exists())
                    now[0]=1210
                    (self.root/('activity-'+task['session'])).write_text('CALLCLI_STAGE resumed\n')
                    wait_for(lambda:any(e['text']=='resumed' for e in events()))
                    now[0]=1803
                    wait_for(lambda:len(reminders())==3)
                    self.assertEqual(reminders()[2]['notice_kind'], 'checkpoint')
                    frames = [json.loads(line) for line in output.getvalue().splitlines()]
                    published = [e for e in frames if e.get('quiet_notice')]
                    self.assertEqual([e['event_id'] for e in published], [e['token'] for e in reminders()])
                    self.assertEqual([e['seq'] for e in published], [e['seq'] for e in reminders()])
                    self.assertTrue(all(e['task_id']==task['id'] for e in published))
                    self.store.request_cancel(task['id'],self.origin)
                    now[0]=2404
                    time.sleep(.35)
                    self.assertEqual(len(reminders()),3)
                elif mode=='duplicate_tool':
                    trigger=self.root/('tool-'+task['session'])
                    trigger.touch()
                    wait_for(lambda:any(e['kind']=='tool' for e in events()))
                    now[0]=599;trigger.touch()
                    wait_for(lambda:not trigger.exists());time.sleep(.1)
                    now[0]=601
                    wait_for(lambda:len(reminders())==1)
                    self.assertEqual(sum(e['kind']=='tool' for e in events()),1)
                    self.assertFalse((self.root/('cancel-'+task['session'])).exists())
                    self.store.request_cancel(task['id'],self.origin)
                else:now[0]=121
                rc=future.result(timeout=5)
                result=self.store.get(task['id'])
                if mode=='startup':
                    self.assertEqual((result['state'],rc),('unknown',1))
                    self.assertIn('prompt acceptance not observed within 120s',result['settlement_note'])
                    self.assertNotIn('TIMEOUT',result['settlement_note'])
                else:self.assertEqual((result['state'],rc),('cancelled',0))
                options=json.loads((self.root/'shared-options.jsonl').read_text().splitlines()[0])
                if mode=='explicit':self.assertEqual(options['timeoutMs'],30000)
                else:self.assertNotIn('timeoutMs',options)
            finally:
                if not future.done():
                    self.store.request_cancel(task['id'],self.origin)
                    future.result(timeout=5)

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
            self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, task['id']), 0)
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
            self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, task['id']), 0)
        resumed_events = self.store.events(task['id'], self.origin, terminal['seq'])
        self.assertTrue(all(e['turn'] == 2 for e in resumed_events))
        self.assertEqual([e['text'] for e in resumed_events if e['kind'] == 'answer'], ['FAKE_ANSWER'])
        final = self.store.get(task['id'])
        self.assertEqual((final['state'], final['turn'], final['session']), ('completed', 2, task['session']))
        self.assertEqual(final['intervention']['target_turn'], 2)
        self.assertEqual(final['commands']['revision']['status'], 'target_completed')
        self.assertEqual(final['intervention']['status'], 'target_completed')
        self.assertEqual([e['kind'] for e in self.store.events(task['id'], self.origin)].count('cancelled'), 1)

    def test_command_claim_and_uncertain_target_do_not_replay(self):
        import contextlib, io
        from unittest.mock import patch
        from backends import TurnResult, backend_for
        from runner import run_task
        for outcome in ('unknown', 'cancelled', 'exception'):
            with self.subTest(outcome=outcome):
                task = self.task('initial')
                self.store.intervene(task['id'], self.origin, 'adjust', 'stable')
                def target(store, current, emit):
                    observed = store.get(task['id'])
                    self.assertEqual(observed['commands']['stable']['status'], 'target_started')
                    self.assertEqual(observed['intervention']['status'], 'target_started')
                    emit('answer', 'preserved partial result')
                    if outcome == 'exception':
                        raise RuntimeError('synthetic interruption')
                    return TurnResult(outcome, 0 if outcome == 'cancelled' else 1,
                                      'cancelled' if outcome == 'cancelled' else None)
                with patch.object(backend_for('omp'), 'run', target), contextlib.redirect_stdout(io.StringIO()):
                    __import__('test_dispatch').run_dispatched(self.store.path, task['id'])
                final, launch = self.store.intervene(task['id'], self.origin, 'adjust', 'stable')
                self.assertFalse(launch)
                self.assertEqual(final['turn'], 1)
                self.assertEqual(final['intervention']['status'],
                                 'target_cancelled' if outcome == 'cancelled' else 'target_unknown')
                self.assertEqual(final['commands']['stable']['status'], final['intervention']['status'])
                self.assertEqual([e['text'] for e in self.store.events(task['id'], self.origin) if e['kind']=='answer'],
                                 ['preserved partial result'])

    def test_new_command_during_target_does_not_steal_previous_settlement(self):
        import contextlib, io
        from unittest.mock import patch
        from backends import TurnResult, backend_for
        from runner import run_task
        task = self.task('initial')
        self.store.intervene(task['id'], self.origin, 'first', 'one')
        def revise_again(store, current, emit):
            if current['turn'] == 2:
                self.assertEqual(current['prompt'], 'second')
                return TurnResult('completed', 0, 'end_turn')
            self.store.intervene(task['id'], self.origin, 'second', 'two')
            self.assertFalse(self.store.intervene(task['id'], self.origin, 'first', 'one')[1])
            return TurnResult('cancelled', 0, 'cancelled')
        with patch.object(backend_for('omp'), 'run', revise_again), contextlib.redirect_stdout(io.StringIO()):
            __import__('test_dispatch').run_dispatched(self.store.path, task['id'])
        final = self.store.get(task['id'])
        self.assertEqual(final['commands']['one']['status'], 'target_cancelled')
        self.assertEqual(final['commands']['two']['status'], 'pending')
        resumed, launch = self.store.advance_intervention(task['id'], self.origin)
        self.assertTrue(launch)
        self.assertEqual((resumed['turn'], resumed['session']), (2, task['session']))
        with patch.object(backend_for('omp'), 'run', revise_again), contextlib.redirect_stdout(io.StringIO()):
            __import__('test_dispatch').run_dispatched(self.store.path, task['id'])
        final = self.store.get(task['id'])
        self.assertEqual(final['commands']['one']['status'], 'target_cancelled')
        self.assertEqual(final['commands']['two']['status'], 'target_completed')
        self.assertEqual(final['intervention']['status'], 'target_completed')

    def test_question_reply_turns_never_redispatch_controller_command(self):
        import contextlib, io
        from runner import run_task
        task = self.task('initial')
        self.store.intervene(task['id'], self.origin, 'ASK', 'stable')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, task['id']), 0)
        first = self.store.get(task['id'])
        self.assertEqual((first['state'], first['commands']['stable']['status']),
                         ('waiting_reply', 'target_completed'))
        for answer in ('ASK again', 'final answer'):
            current = self.store.get(task['id'])
            resumed = self.store.reply(task['id'], self.origin, current['question_id'], answer)
            replay, launch = self.store.intervene(task['id'], self.origin, 'ASK', 'stable')
            self.assertFalse(launch)
            self.assertEqual((replay['turn'], replay['prompt']), (resumed['turn'], answer))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, task['id']), 0)
        final = self.store.get(task['id'])
        self.assertEqual((final['turn'], final['state'], final['session']), (3, 'completed', task['session']))
        self.assertEqual((final['commands']['stable']['target_turn'], final['intervention']['status']),
                         (1, 'target_completed'))
        self.assertEqual([(e['turn'], e['kind']) for e in self.store.events(task['id'], self.origin)
                          if e['token'].endswith(':settled')],
                         [(1, 'waiting_reply'), (2, 'waiting_reply'), (3, 'completed')])

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
                    self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, task['id']), 1 if raises else 0)
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
            self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, task['id']), 0)
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
            self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, task['id']), 0)
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
            self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, task['id']), 0)
        resumed = self.store.events(task['id'], self.origin, cursor)
        self.assertTrue(all(e['turn']==2 for e in resumed))
        self.assertEqual([e['text'] for e in resumed if e['kind']=='answer'], ['FAKE_ANSWER'])
        self.assertIn('FAKE_ANSWER', json.loads(output.getvalue().splitlines()[-1])['answer_tail'])


    def test_prompt_uses_shared_runtime_and_persists_native_record_identity(self):
        import json
        from runner import run_task
        t = self.task('normal task')
        self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, t['id']), 0)
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
        self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, t['id']), 0)
        state = self.store.get(t['id'])
        self.store.reply(t['id'], self.origin, state['question_id'], 'JSON please')
        with patch.dict(os.environ, {'ACPX_FAKE_VERSION': '0.20.1'}):
            self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, t['id']), 0)
        frames = [json.loads(x) for x in (self.root / 'shared-options.jsonl').read_text().splitlines()]
        self.assertEqual(frames[1]['expectedVersion'], '0.20.1')
        self.assertEqual(frames[1]['sessionId'], frames[0]['sessionId'])
        self.assertEqual(self.store.get(t['id'])['version_check']['installed'], '0.20.0')
        self.assertEqual(self.store.get(t['id'])['execution_version'], '0.20.1')

    def test_revised_first_turn_creates_session_if_old_worker_never_started(self):
        from runner import run_task
        t=self.task('revised task')
        self.store.update(t['id'],turn=2,needs_session=True,acpx_created=False)
        self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, t['id']),0)
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
            self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, t['id']), 0)
            state = self.store.get(t['id'])
            self.store.reply(t['id'], self.origin, state['question_id'], 'continue')
            self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, t['id']), 0)
            self.assertEqual(check.call_count, 1)

    def test_stage_only_turn_is_unknown_for_every_supported_cli(self):
        import contextlib, io, json, os
        from unittest.mock import patch
        from backends import BACKENDS
        from runner import run_task
        for agent in BACKENDS:
            with self.subTest(agent=agent):
                task = self.task('synthetic stage only')
                self.store.update(task['id'], agent=agent, write=True)
                with patch.dict(os.environ, {'ACPX_FAKE_CHUNKS':json.dumps([
                        'CALLCLI_STA', 'GE checking version and protocol support'])}), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    rc = __import__('test_dispatch').run_dispatched(self.store.path, task['id'])
                result = self.store.get(task['id'], self.origin)
                events = self.store.events(task['id'], self.origin)
                self.assertEqual((result['state'], rc, result['exit_code'], result['stop_reason']),
                                 ('unknown', 1, 0, 'end_turn'))
                self.assertEqual([e['text'] for e in events if e['kind']=='stage'],
                                 ['checking version and protocol support'])
                self.assertEqual(events[-1]['kind'], 'unknown')
                self.assertEqual(events[-1]['answer_tail'], '')
                self.assertIn('no final answer', result['settlement_note'])
                if agent=='omp':
                    terminal=events[-1]
                    self.assertTrue(terminal['attention_required'])
                    self.assertEqual(terminal['notification_scope'],'originating-session')
                    self.assertIn('Do not replay',terminal['next_action'])
                    frames=[json.loads(line) for line in output.getvalue().splitlines()]
                    published=[f for f in frames if f['event']=='unknown']
                    self.assertEqual(len(published),1)
                    self.assertEqual((published[0]['text'],published[0]['turn'],published[0]['seq']),
                        (terminal['text'],result['turn'],terminal['seq']))
                    self.assertIn(task['id'],terminal['text'])
                    self.assertIn('needs initiator attention',terminal['text'])
                    self.assertFalse(self.store.finish_turn(task['id'],result['turn'],'unknown',0,'end_turn'))
                    self.assertEqual(len([e for e in self.store.events(task['id'],self.origin) if e['kind']=='unknown']),1)
                with self.assertRaises(ValueError):
                    self.store.create(self.origin, 'local', agent, str(self.root), 'conflict', True)
                before = (self.root/'calls.jsonl').read_text()
                with self.assertRaises(ValueError):
                    __import__('test_dispatch').run_dispatched(self.store.path, task['id'])
                self.assertEqual((self.root/'calls.jsonl').read_text(), before)
                # Release only this synthetic reservation before the next case.
                self.store.update(task['id'], write=False)

    def test_public_output_and_protocol_outcomes_remain_distinct_across_clis(self):
        import contextlib, io, json, os
        from unittest.mock import patch
        from backends import BACKENDS
        from runner import run_task
        tool = {'toolCallId':'read1', 'kind':'read', 'status':'completed'}
        cases = [
            ('answer', ['CALLCLI_STAGE checking\n', 'Result: 42'], {}, 'completed', 0),
            ('literal progress prose', ['Checking version and protocol support'], {}, 'completed', 0),
            ('skill only', ['CALLCLI_SKILL skill-1 adopted read resources\n'], {}, 'unknown', 1),
            ('empty', [], {}, 'unknown', 1),
            ('tool only', [], {'ACPX_FAKE_TOOLS':json.dumps([tool])}, 'unknown', 1),
            ('partial before tool', ['Partial result\n'], {'ACPX_FAKE_TRAILING_TOOLS':json.dumps([tool])}, 'unknown', 1),
            ('late duplicate tool', ['Partial result\n'], {'ACPX_FAKE_TOOLS':json.dumps([tool]),
                 'ACPX_FAKE_TRAILING_TOOLS':json.dumps([tool])}, 'unknown', 1),
            ('structured answer with receipts', ['0\nfalse\n[]\n{"ok":true}\nCALLCLI_SKILL skill-1 adopted used\nCALLCLI_STAGE verified\n'],
                 {'ACPX_FAKE_TOOLS':json.dumps([tool])}, 'completed', 0),
            ('tool then answer', ['Final result: 42'], {'ACPX_FAKE_TOOLS':json.dumps([tool])}, 'completed', 0),
            ('tool then question', ['CALLCLI_QUESTION Q1 Which file?'], {'ACPX_FAKE_TOOLS':json.dumps([tool])}, 'waiting_reply', 0),
            ('failure after answer', ['Partial result\n'], {'ACPX_FAKE_RESULT_FAILED':'1'}, 'unknown', 1),
            ('nonzero after answer', ['Partial result\n'], {'ACPX_FAKE_EXIT':'1'}, 'unknown', 1),
        ]
        for agent in BACKENDS:
            for name, chunks, extra, expected, code in cases:
                with self.subTest(agent=agent, case=name):
                    task = self.task('synthetic output classification')
                    self.store.update(task['id'], agent=agent)
                    with patch.dict(os.environ, dict(extra, ACPX_FAKE_CHUNKS=json.dumps(chunks))), \
                            contextlib.redirect_stdout(io.StringIO()):
                        rc = __import__('test_dispatch').run_dispatched(self.store.path, task['id'])
                    result = self.store.get(task['id'], self.origin)
                    self.assertEqual((result['state'], rc), (expected, code))
                    events = self.store.events(task['id'], self.origin)
                    self.assertEqual(events[-1]['kind'], expected)
                    if name in {'partial before tool', 'late duplicate tool'}:
                        self.assertEqual(events[-1]['answer_tail'], 'Partial result\n')
                    if name == 'late duplicate tool':
                        self.assertEqual([e['text'] for e in events if e['kind']=='tool'], ['read1 read completed'])
                    if expected == 'waiting_reply':
                        self.assertEqual(result['question_id'], [e['token'] for e in events if e['kind']=='question'][0])
                    if name in {'failure after answer', 'nonzero after answer'}:
                        self.assertNotIn('no final answer', result.get('settlement_note', ''))

    def test_real_runner_rejects_empty_exit_zero_and_does_not_replay(self):
        from runner import run_task
        t=self.task('EMPTY')
        self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, t['id']),1)
        self.assertEqual(self.store.get(t['id'])['state'],'unknown')
        before=(self.root/'calls.jsonl').read_text()
        with self.assertRaises(ValueError):__import__('test_dispatch').run_dispatched(self.store.path, t['id'])
        self.assertEqual((self.root/'calls.jsonl').read_text(),before)

    def test_question_then_answer_runs_same_session_in_distinct_turns(self):
        from runner import run_task
        t=self.task('ASK')
        self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, t['id']),0)
        state=self.store.get(t['id']);self.assertEqual(state['state'],'waiting_reply')
        reply=self.store.reply(t['id'],self.origin,state['question_id'],'JSON please')
        self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, t['id']),0)
        self.assertEqual(self.store.get(t['id'])['state'],'completed')
        self.assertEqual(reply['session'],t['session'])
        calls=[__import__('json').loads(l) for l in (self.root/'calls.jsonl').read_text().splitlines()]
        self.assertEqual(sum('new' in c for c in calls),1)
        self.assertEqual(sum('prompt' in c for c in calls),2)

    def test_conflicting_numbered_questions_preserve_stream_and_continue(self):
        import contextlib, io, json, os
        from unittest.mock import patch
        from runner import run_task
        task = self.task('normal task')
        chunk = ('EARLY_RESULT\nCALLCLI_QUESTION Q1 Format?\n'
                 'CALLCLI_QUESTION Q1 Format, JSON or CSV?\nLATE_RESULT\n')
        with patch.dict(os.environ, {'ACPX_FAKE_CHUNKS':json.dumps([chunk])}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, task['id']), 0)
        result = self.store.get(task['id'])
        events = self.store.events(task['id'], self.origin)
        self.assertEqual([e['text'] for e in events if e['kind']=='answer'], ['EARLY_RESULT', 'LATE_RESULT'])
        self.assertEqual(result['state'], 'waiting_reply')
        questions = [e for e in events if e['kind']=='question']
        self.assertEqual([e['text'] for e in questions], ['Q1 Format?', 'Q1 Format, JSON or CSV?'])
        self.assertIn('question_note', questions[1])
        first = self.store.reply(task['id'], self.origin, questions[0]['token'], 'JSON')
        self.assertEqual((first['turn'], first['state']), (1, 'waiting_reply'))
        self.store.reply(task['id'], self.origin, questions[1]['token'], 'CSV')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, task['id']), 0)
        final = self.store.get(task['id'])
        self.assertEqual((final['state'], final['session'], final['acpx_record_id']),
                         ('completed', task['session'], result['acpx_record_id']))

    def test_long_question_identity_precedes_public_truncation(self):
        import contextlib, io, json, os
        from unittest.mock import patch
        from runner import run_task
        for suffixes, expected in [('AB', 2), ('AA', 1)]:
            with self.subTest(suffixes=suffixes):
                task = self.task('normal task')
                lines = ['CALLCLI_QUESTION Q1 '+'x'*7997+suffix+'\n' for suffix in suffixes]
                output = io.StringIO()
                with patch.dict(os.environ, {'ACPX_FAKE_CHUNKS':json.dumps(lines)}), contextlib.redirect_stdout(output):
                    self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, task['id']), 0)
                questions = [e for e in self.store.events(task['id'], self.origin) if e['kind']=='question']
                self.assertEqual(len(questions), expected)
                self.assertTrue(all(len(e['text']) == 8000 for e in questions))
                self.assertNotIn('x'*7997+'A', output.getvalue()+json.dumps(questions))
                for n, question in enumerate(questions):
                    reply = self.store.reply(task['id'], self.origin, question['token'], 'answer '+str(n))
                    self.assertEqual(reply['turn'], 2 if n == expected-1 else 1)

    def test_over_limit_question_is_retained_without_claiming_complete_identity(self):
        import contextlib, io, json, os
        from unittest.mock import patch
        from runner import run_task
        task = self.task('normal task')
        chunks = ['CALLCLI_QUESTION Q1 ', 'x'*40000, 'x'*40000, 'A\n']*2+['LATE_RESULT\n']
        with patch.dict(os.environ, {'ACPX_FAKE_CHUNKS':json.dumps(chunks)}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, task['id']), 0)
        events = self.store.events(task['id'], self.origin)
        questions = [e for e in events if e['kind']=='question']
        self.assertEqual(len(questions), 2)
        self.assertTrue(all('question_sha256' not in e and 'question_note' in e for e in questions))
        self.assertEqual([e['text'] for e in events if e['kind']=='answer'], ['LATE_RESULT'])

    # Exact AGY wire chunk observed in real failed turns (prompt result was end_turn).
    AGY_FATAL = '\n\u26a0 Agent execution terminated due to error.\n'

    def run_agy(self, chunks, agent='agy'):
        import json, os
        from unittest.mock import patch
        from runner import run_task
        t = self.store.create(self.origin, 'local', agent, str(self.root), 'normal task', False)
        t = self.store.update(t['id'], shell='posix', timeout=30)
        with patch.dict(os.environ, {'ACPX_FAKE_CHUNKS': json.dumps(chunks)}):
            rc = __import__('test_dispatch').run_dispatched(self.store.path, t['id'])
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
            __import__('test_dispatch').run_dispatched(self.store.path, state['id'])
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
                    __import__('test_dispatch').run_dispatched(self.store.path, state['id'])

    def test_agy_transport_failure_keeps_direct_failure_explanation(self):
        import os, json
        from unittest.mock import patch
        for transport in ({'ACPX_FAKE_EXIT':'1'}, {'ACPX_FAKE_RESULT_FAILED':'1'}):
            with self.subTest(transport=transport), patch.dict(os.environ, dict(transport,
                    ACPX_FAKE_TOOLS=json.dumps([{'toolCallId':'shell1','kind':'execute','status':'failed'}]))):
                rc, state, events = self.run_agy(['CALLCLI_STAGE attempting shell\n'])
                self.assertEqual((rc, state['state']), (1, 'unknown'))
                self.assertIsNone(state.get('settlement_note'))
                terminal = events[-1]
                self.assertEqual(terminal['text'], 'AGY task '+state['id']+' turn '+str(state['turn'])+
                    ' needs initiator attention: Prompt exited '+str(state['exit_code']))
                self.assertEqual((terminal['kind'], terminal['turn']), ('unknown', state['turn']))
                self.assertTrue(terminal['attention_required'])
                self.assertEqual(terminal['notification_scope'], 'originating-session')
                self.assertIn('Do not replay', terminal['next_action'])
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
        self.assertEqual(__import__('test_dispatch').run_dispatched(self.store.path, state['id']), 0)
        self.assertEqual(self.store.get(state['id'])['state'], 'completed')


    def test_cancelling_one_parallel_task_does_not_cancel_the_other(self):
        import concurrent.futures,time
        from runner import run_task
        a=self.task('BLOCK');b=self.task('BLOCK')
        with concurrent.futures.ThreadPoolExecutor() as pool:
            fa=pool.submit(__import__('test_dispatch').run_dispatched, self.store.path, a['id']);fb=pool.submit(__import__('test_dispatch').run_dispatched, self.store.path, b['id'])
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
