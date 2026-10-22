"""Opt-in real ACPX shared-owner tests using a synthetic ACP agent, not a model."""
import concurrent.futures,contextlib,io,json,os,pathlib,shlex,subprocess,sys,tempfile,time,unittest
from unittest.mock import patch
from bridge import EventDecoder
from collab import Collaboration
from runner import run_task

@unittest.skipUnless(os.environ.get('RUN_NATIVE_ACPX_TESTS')=='1','opt-in real ACPX queue-owner integration; no model')
class NativeSharedTests(unittest.TestCase):
    def test_native_collision_prompt_failure_and_cancel(self):
        fixture=pathlib.Path(__file__).with_name('fixtures')/'native_shared_agent.py'
        evidence=[]
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()/'.hermes/cache/scratch')as tmp:
            root=pathlib.Path(tmp)
            for mode,expected in [('recover','completed'),('auth','completed'),('fail','unknown'),('block','cancelled')]:
                work=root/mode;work.mkdir();trace=work/'wire-shapes.jsonl'
                home=work/'isolated-home';home.mkdir()
                if mode=='auth':
                    (work/'.acpxrc.json').write_text(json.dumps({'authPolicy':'fail','auth':{'CALLCLI_FIXTURE_AUTH':'SYNTHETIC_AUTH_ONLY'}}))
                agent=shlex.join([sys.executable,'-B',str(fixture),str(trace),mode])
                def control(task,action,timeout=20):
                    args=['acpx','--agent',agent,'--cwd',task['cwd'],'--format','json']
                    if action=='new':args+=['sessions','new','--name',task['session']]
                    elif action in ['close','show']:args+=['sessions',action,task['session']]
                    else:args+=[action,'-s',task['session']]
                    return subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,timeout=timeout)
                c=Collaboration(work/'tasks.sqlite3',control=control);client=c.connect('hermes')['client_id'];origin=c.origin(client)
                t=c.store.create(origin,'local','omp',str(work),'SYNTHETIC PROTOCOL TEST ONLY',False)
                c.store.update(t['id'],shell='posix',timeout=35)
                # Synthetic endpoints must never receive the user's real auth config.
                environment=patch.dict(os.environ,{'HOME':str(home)});environment.start()
                try:
                    with patch('runner.control',side_effect=control),patch('updates.ensure_current',return_value={'decision':'integration-uses-existing'}),contextlib.redirect_stdout(io.StringIO()):
                        if mode=='block':
                            with concurrent.futures.ThreadPoolExecutor(max_workers=1)as pool:
                                future=pool.submit(run_task,c.store.path,t['id']);deadline=time.monotonic()+25
                                while time.monotonic()<deadline:
                                    if any(x['kind']=='stage'for x in c.store.events(t['id'],origin)):break
                                    time.sleep(.05)
                                else:self.fail('Synthetic prompt never became active')
                                c.store.request_cancel(t['id'],origin);rc=future.result(timeout=35)
                        else:rc=run_task(c.store.path,t['id'])
                    outcome=c.store.get(t['id'],origin);self.assertEqual(outcome['state'],expected)
                    self.assertEqual(rc,1 if mode=='fail'else 0)
                    wire=[json.loads(line)for line in trace.read_text().splitlines()];prompts=[x for x in wire if x.get('method')=='session/prompt'];self.assertEqual(len(prompts),1)
                    legacy=EventDecoder()
                    for frame in wire:legacy.feed(frame)
                    collision=False
                    if mode in ['recover','auth']:
                        pid=prompts[0]['id'];reads=[x for x in wire if x.get('method')=='fs/read_text_file'];errors=[x for x in wire if x.get('error')]
                        collision=bool(reads and errors and reads[0]['id']==errors[0]['id']==pid)
                        self.assertTrue(collision);self.assertTrue(legacy.failed)
                    if mode=='auth':
                        checks=[x['synthetic_auth_seen']for x in wire if 'synthetic_auth_seen'in x]
                        self.assertTrue(checks);self.assertTrue(all(checks))
                    evidence.append({'mode':mode,'state':outcome['state'],'exit':rc,'stop_reason':outcome.get('stop_reason'),'backend_prompt_count':len(prompts),'real_bidirectional_id_collision':collision,'legacy_decoder_failed':legacy.failed})
                finally:
                    try:
                        state=c.store.get(t['id'],origin)['state']
                        if state in ['completed','unknown','cancelled','waiting_reply']:
                            closed=c.close(client,t['id']);self.assertEqual(closed['state'],'closed')
                    finally:environment.stop()
        print('NATIVE_SHARED_EVIDENCE '+json.dumps(evidence))

if __name__=='__main__':unittest.main()
