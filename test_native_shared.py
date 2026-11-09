"""Opt-in real ACPX shared-owner tests using a synthetic ACP agent, not a model."""
import concurrent.futures,contextlib,io,json,os,pathlib,shlex,subprocess,sys,tempfile,time,unittest
from unittest.mock import patch
from events import EventDecoder
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
                    with patch('acpx_backend.AcpxBackend.control',side_effect=control),patch('updates.ensure_current',return_value={'decision':'integration-uses-existing'}),contextlib.redirect_stdout(io.StringIO()):
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

    def test_structured_prompt_auth_survives_queue_wrapping_without_replay(self):
        fixture=pathlib.Path(__file__).with_name('fixtures')/'native_shared_agent.py'
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()/'.hermes/cache/scratch') as tmp:
            work=pathlib.Path(tmp);home=work/'home';home.mkdir();trace=work/'wire.jsonl'
            agent=shlex.join([sys.executable,'-B',str(fixture),str(trace),'auth-fail'])
            def control(task,action,timeout=20):
                args=['acpx','--agent',agent,'--cwd',task['cwd'],'--format','json']
                if action=='new':args+=['sessions','new','--name',task['session']]
                elif action in ('close','show'):args+=['sessions',action,task['session']]
                else:args+=[action,'-s',task['session']]
                return subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,timeout=timeout)
            c=Collaboration(work/'tasks.sqlite3',control=control);client=c.connect('omp')['client_id'];origin=c.origin(client)
            task=c.store.create(origin,'local','omp',str(work),'SYNTHETIC AUTH DIAGNOSTIC ONLY',False)
            c.store.update(task['id'],shell='posix',timeout=35)
            with patch.dict(os.environ,{'HOME':str(home)}),patch('acpx_backend.AcpxBackend.control',side_effect=control),patch('updates.ensure_current',return_value={'decision':'existing-runtime'}):
                try:
                    output=io.StringIO()
                    with contextlib.redirect_stdout(output):rc=run_task(c.store.path,task['id'])
                    result=c.store.get(task['id']);events=c.store.events(task['id'],origin)
                    self.assertEqual((result['state'],rc),('unknown',1))
                    self.assertIn('watchSession prompt error.code=-32000/data.authRequired=true',result['settlement_note'])
                    self.assertEqual([e['text'] for e in events if e['kind']=='answer'],['SYNTHETIC_PARTIAL_RESULT'])
                    self.assertNotIn('PRIVATE_SYNTHETIC',output.getvalue()+json.dumps(result)+json.dumps(events))
                    wire=[json.loads(line) for line in trace.read_text().splitlines()]
                    self.assertEqual(sum(x.get('method')=='session/prompt' for x in wire),1)
                finally:
                    c.close(client,task['id'])
                    self.assertEqual(json.loads(control(c.store.get(task['id']),'status').stdout)['status'],'no-session')

    def test_native_adapter_intervention_tracks_target_without_replay(self):
        from test_plugin import load_plugin, Ctx
        mod = load_plugin()
        fixture = pathlib.Path(__file__).with_name('fixtures')/'native_shared_agent.py'
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()/'.hermes/cache/scratch') as tmp:
            work=pathlib.Path(tmp);home=work/'home';home.mkdir();trace=work/'wire.jsonl'
            agent=shlex.join([sys.executable,'-B',str(fixture),str(trace),'intervention'])
            def control(task,action,timeout=20):
                args=['acpx','--agent',agent,'--cwd',task['cwd'],'--format','json']
                if action=='new':args+=['sessions','new','--name',task['session']]
                elif action in ('close','show'):args+=['sessions',action,task['session']]
                else:args+=[action,'-s',task['session']]
                return subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,timeout=timeout)
            futures=[]
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                class SyntheticHost(Ctx):
                    def dispatch_tool(self,name,args,**kwargs):
                        argv=shlex.split(args['command'])
                        futures.append(pool.submit(run_task,argv[argv.index('--db')+1],argv[argv.index('--task')+1]))
                        return json.dumps({'session_id':'synthetic-process-'+str(len(futures))})
                env={'HERMES_SESSION_ID':'synthetic-origin','HERMES_SESSION_KEY':'synthetic-key'}
                c=mod.Coordinator(SyntheticHost(),work,lambda k,d='':env.get(k,d))
                with patch.dict(os.environ,{'HOME':str(home)}),patch('acpx_backend.AcpxBackend.control',side_effect=control),patch.object(mod,'control',side_effect=control),patch('updates.ensure_current',return_value={'decision':'existing-runtime'}),contextlib.redirect_stdout(io.StringIO()):
                    task=c.handle({'action':'start','agent':'omp','cwd':str(work),'prompt':'INITIAL_BLOCK','timeout':35})['task']
                    store=mod.Store(work/'call-cli/tasks.sqlite3')
                    origin=mod.capture_origin(lambda k,d='':env.get(k,d),'default')
                    args={'action':'intervene','task_id':task['id'],'command_id':'synthetic-stable','prompt':'SYNTHETIC_REVISION'}
                    try:
                        deadline=time.monotonic()+25
                        while time.monotonic()<deadline:
                            if any(e['kind']=='stage' for e in store.events(task['id'],origin)):break
                            time.sleep(.05)
                        else:self.fail('Synthetic initial turn never became active')
                        pending=c.handle(args)['task']
                        self.assertEqual((pending['state'],pending['intervention']['status']),('cancelling','pending'))
                        c.handle(args)
                        self.assertEqual(futures[0].result(timeout=35),0)
                        old=store.get(task['id']);record=old['acpx_record_id']
                        self.assertEqual((old['turn'],old['state']),(1,'cancelled'))
                        c.handle({'action':'status','task_id':task['id']})
                        c.handle(args)
                        self.assertEqual(futures[1].result(timeout=35),0)
                        final=c.handle({'action':'status','task_id':task['id']})['task']
                        self.assertEqual((final['state'],final['turn'],final['session'],final['acpx_record_id']),('completed',2,task['session'],record))
                        self.assertEqual(final['commands']['synthetic-stable']['status'],'target_completed')
                        self.assertEqual(final['intervention']['status'],'target_completed')
                        self.assertFalse(store.finish_turn(task['id'],1,'completed',0,'end_turn'))
                        c.handle(args);c.handle({'action':'events','task_id':task['id']});c.handle({'action':'status','task_id':task['id']})
                        self.assertEqual(len(futures),2)
                        events=store.events(task['id'],origin)
                        self.assertEqual([(e['turn'],e['kind']) for e in events if e['token'].endswith(':settled')],[(1,'cancelled'),(2,'completed')])
                        self.assertIn('SYNTHETIC_PARTIAL_RESULT',[e['text'] for e in events if e['kind']=='answer'])
                        self.assertIn('SYNTHETIC_TARGET_RESULT revision received',[e['text'] for e in events if e['kind']=='answer' and e['turn']==2])
                        wire=[json.loads(line) for line in trace.read_text().splitlines()]
                        self.assertEqual(sum(e.get('method')=='session/prompt' for e in wire),2)
                        self.assertEqual(sum(e.get('method')=='session/new' for e in wire),1)
                    finally:
                        current=store.get(task['id'])
                        if current['state'] in ('running','queued','cancelling'):
                            c.handle({'action':'cancel','task_id':task['id']})
                        for future in futures:future.result(timeout=35)
                        c.handle({'action':'close','task_id':task['id']})
                        self.assertEqual(json.loads(control(store.get(task['id']),'status').stdout)['status'],'no-session')
            print('NATIVE_INTERVENTION_EVIDENCE '+json.dumps(dict(turns=2,prompts=2,session_creations=1,duplicate_replayed=False,status='target_completed',closed=True,no_session=True,host='synthetic-native-adapter',model=False)))

    def test_numbered_restatement_and_independent_questions_continue_same_session(self):
        fixture=pathlib.Path(__file__).with_name('fixtures')/'native_shared_agent.py'
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()/'.hermes/cache/scratch') as tmp:
            work=pathlib.Path(tmp);home=work/'home';home.mkdir();trace=work/'wire.jsonl'
            agent=shlex.join([sys.executable,'-B',str(fixture),str(trace),'questions'])
            def control(task,action,timeout=20):
                args=['acpx','--agent',agent,'--cwd',task['cwd'],'--format','json']
                if action=='new':args+=['sessions','new','--name',task['session']]
                elif action in ('close','show'):args+=['sessions',action,task['session']]
                else:args+=[action,'-s',task['session']]
                return subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,timeout=timeout)
            c=Collaboration(work/'tasks.sqlite3',control=control,launch=lambda task:None)
            client=c.connect('omp')['client_id'];origin=c.origin(client)
            task=c.store.create(origin,'local','omp',str(work),'INITIAL_QUESTION',False)
            c.store.update(task['id'],shell='posix',timeout=35)
            with patch.dict(os.environ,{'HOME':str(home)}),patch('acpx_backend.AcpxBackend.control',side_effect=control),patch('updates.ensure_current',return_value={'decision':'existing-runtime'}):
                try:
                    with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(run_task(c.store.path,task['id']),0)
                    initial=c.status(client,task['id']);record=initial['acpx_record_id']
                    first=c.events(client,task['id']);questions=[e for e in first if e['kind']=='question']
                    self.assertEqual([e['text'] for e in questions],['Q1 Which format?'])
                    self.assertEqual(initial['questions'],[questions[0]['token']])
                    self.assertEqual([e['text'] for e in first if e['kind']=='answer'],['SYNTHETIC_PARTIAL_RESULT'])
                    self.assertEqual([e['text'] for e in first if e['kind']=='stage'],['synthetic protocol fixture started'])
                    self.assertEqual([e['text'] for e in first if e['kind']=='tool'],['question-tool other pending','question-tool other completed'])
                    cursor=first[-1]['seq']
                    self.assertEqual(c.reply(client,task['id'],questions[0]['token'],'REASK_QUESTION')['turn'],2)
                    with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(run_task(c.store.path,task['id']),0)
                    second=c.events(client,task['id'],cursor);q2=[e for e in second if e['kind']=='question']
                    self.assertEqual([e['text'] for e in q2],['Q1 Which format?'])
                    self.assertNotEqual(q2[0]['token'],questions[0]['token'])
                    c.reply(client,task['id'],q2[0]['token'],'INDEPENDENT_QUESTIONS')
                    with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(run_task(c.store.path,task['id']),0)
                    current=c.status(client,task['id']);self.assertEqual(len(current['questions']),5)
                    third=[e for e in c.events(client,task['id']) if e['turn']==3]
                    self.assertEqual([e['text'] for e in third if e['kind']=='answer'],['EARLY_RESULT','LATE_RESULT'])
                    third_questions=[e for e in third if e['kind']=='question']
                    self.assertIn('question_note',third_questions[2])
                    self.assertIn('question_note',third_questions[4])
                    self.assertEqual(third_questions[3]['text'],third_questions[4]['text'])
                    self.assertNotEqual(third_questions[3]['question_sha256'],third_questions[4]['question_sha256'])
                    for n,token in enumerate(current['questions']):
                        reply=c.reply(client,task['id'],token,'SYNTHETIC_REPLY_'+str(n))
                        self.assertEqual(reply['turn'],4 if n==4 else 3)
                    with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(run_task(c.store.path,task['id']),0)
                    final=c.status(client,task['id'])
                    self.assertEqual((final['state'],final['turn'],final['acpx_record_id'],final['session']),('completed',4,record,task['session']))
                    applied='\n'.join(e['text'] for e in c.events(client,task['id']) if e['kind']=='answer' and e['turn']==4)
                    for n in range(5):self.assertIn('SYNTHETIC_REPLY_'+str(n),applied)
                    wire=[json.loads(line) for line in trace.read_text().splitlines()]
                    self.assertEqual(sum(e.get('method')=='session/prompt' for e in wire),4)
                    self.assertEqual(sum(e.get('method')=='session/new' for e in wire),1)
                    print('NATIVE_QUESTION_EVIDENCE '+json.dumps(dict(turns=4,prompts=4,session_creations=1,one_reply_continued=True,independent_questions=5,conflict_continued=True,long_tail_preserved=True,state=final['state'])))
                finally:
                    c.close(client,task['id'])
                    self.assertEqual(json.loads(control(c.store.get(task['id']),'status').stdout)['status'],'no-session')

if __name__=='__main__':unittest.main()
