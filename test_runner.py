import unittest
from runner import command, protocol_prompt


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
        stop=message['result']['stopReason']
        print(json.dumps({'type':'callcli.shared.result','requestId':request,'status':'failed'if failed else ('cancelled'if stop=='cancelled'else'completed'),'stopReason':stop}),flush=True);return
    update=message['params']['update']
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
answer='CALLCLI_QUESTION Which format?\n' if 'ASK' in text else 'CALLCLI_STAGE ready\nFAKE_ANSWER\n'
for chunk in [answer[:8],answer[8:]]:
    output({'params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':chunk}}}})
output({'result':{'stopReason':'end_turn'}})
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
