"""Opt-in installed ACPX + synthetic endpoint; never the real task or model."""
import dataclasses
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from release import NativeBackend
from test_release_workflow import fixture


@unittest.skipUnless(os.environ.get('RUN_NATIVE_ACPX_TESTS') == '1', 'opt-in synthetic native backend')
class NativeReleaseTests(unittest.TestCase):
    def test_exact_public_close_and_associated_no_session_without_prompt_or_wrong_record(self):
        scratch = Path(os.environ['CALLCLI_TEST_SCRATCH'])
        with tempfile.TemporaryDirectory(dir=scratch, prefix='native-release-') as td:
            home = Path(td)
            release, owner, _ = fixture(home)
            trace = home / 'synthetic-trace.jsonl'
            agent = shlex.join([sys.executable, '-B', str(Path(__file__).resolve().parent / 'fixtures/native_shared_agent.py'), str(trace), 'final-answer'])
            env = dict(os.environ, HOME=td, HERMES_HOME=td)
            def cli(*args):
                result = subprocess.run(['acpx','--agent',agent,'--cwd',td,'--format','json',*args],
                                        env=env,capture_output=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr.decode())
                return json.loads(result.stdout)
            first = cli('sessions','new','--name',release.policy['session'])
            second = cli('sessions','new','--name','fixture-other-session')
            release.policy.update(record=first['acpxRecordId'], acp=first['acpxSessionId'], agent=agent)
            # Native last_request_id is set by one real synthetic turn, never by editing ACPX state.
            loader = "const{loadRuntime}=require('./shared-prompt.cjs');(async()=>{const p=JSON.parse(process.argv[1]);const s=await loadRuntime('0.22.0');const r=s.createSharedAcpRuntime({cwd:p.cwd,permissionMode:'approve-reads'});const h={backend:'acpx-shared',acpxRecordId:p.record,sessionKey:p.session,runtimeSessionName:p.record,cwd:p.cwd};const t=r.startTurn({handle:h,text:'SYNTHETIC ONLY',requestId:p.task+':'+p.turn,mode:'prompt'});for await(const e of t.events){};console.log(JSON.stringify(await t.result));await r.shutdown();})().catch(()=>process.exitCode=1);"
            try:
                turn = subprocess.run(['node','-e',loader,json.dumps(release.policy)],env=env,capture_output=True,timeout=35)
                self.assertEqual(turn.returncode,0,turn.stderr.decode())
                release.store.update(release.policy['task'], acpx_record_id=first['acpxRecordId'], agent=agent)
                release.backend = NativeBackend()
                before_release = trace.read_text().splitlines()
                with patch.dict(os.environ,env), patch('updates.DEFAULT_LOCK_ROOT',home/'locks'):
                    receipt = release.release(release.prepare()['nonce'])
                self.assertEqual(receipt['state'],'released',receipt)
                with patch.dict(os.environ,env), patch('updates.DEFAULT_LOCK_ROOT',home/'locks'):
                    observation = release.backend.probe(release.policy)
                self.assertEqual((observation['record']['closed'],observation['status']), (True,'no-session'))
                self.assertFalse(observation['arbitraryDetachedDescendantsProven'])
                self.assertEqual(cli('status','-s',release.policy['session'])['status'],'no-session')
                self.assertEqual(cli('sessions','show','fixture-other-session')['acpxRecordId'],second['acpxRecordId'])
                methods=[json.loads(line).get('method') for line in trace.read_text().splitlines()[len(before_release):]]
                self.assertNotIn('session/prompt',methods)
                self.assertNotIn('session/load',methods)
                self.assertNotIn('session/new',methods)
                # A captured raw SDK handle has no Call-CLI Store admission. Establish
                # the provider limitation rather than claiming a backend tombstone.
                before_replay = trace.read_text().splitlines()
                replay = subprocess.run(['node','-e',loader,json.dumps(dict(release.policy,turn=5))],env=env,
                                        capture_output=True,timeout=35)
                self.assertEqual(replay.returncode,0)
                reopened=cli('status','-s',release.policy['session'])
                print('NATIVE_POSTCLOSE_TURN '+replay.stdout.decode().strip()+' status='+reopened['status'])
                self.assertEqual(json.loads(replay.stdout), {
                    'status':'failed', 'error': {'message':
                        'Shared session is closed; ensure it again before submitting a turn.',
                        'code':'RUNTIME'}})
                replay_methods=[json.loads(line).get('method') for line in
                                trace.read_text().splitlines()[len(before_replay):]]
                for method in ('session/prompt','session/load','session/new'):
                    self.assertNotIn(method,replay_methods)
                self.assertEqual(reopened['status'],'no-session')
            finally:
                cli('sessions','close','fixture-other-session')
                # In failure cases clean only this synthetic target, never production.
                status=cli('status','-s',release.policy['session'])
                if status['status']!='no-session':cli('sessions','close',release.policy['session'])


    def test_configured_omp_alias_uses_same_command_scope_as_cli(self):
        scratch = Path(os.environ['CALLCLI_TEST_SCRATCH'])
        with tempfile.TemporaryDirectory(dir=scratch, prefix='native-alias-') as td:
            root=Path(td); bin_dir=root/'bin';bin_dir.mkdir()
            trace=root/'trace.jsonl'
            wrapper=bin_dir/'omp'
            wrapper.write_text('#!/bin/sh\nshift\nexec '+shlex.join([
                sys.executable,'-B',str(Path(__file__).resolve().parent/'fixtures/native_shared_agent.py'),
                str(trace),'final-answer'])+'\n')
            wrapper.chmod(0o700)
            config=root/'.acpx';config.mkdir()
            (config/'config.json').write_text(json.dumps({'agents':{'omp':{'command':'omp acp'}}}))
            env={k:os.environ[k] for k in ('PATH','TMPDIR','LANG') if k in os.environ}
            env.update(HOME=td,HERMES_HOME=td,PATH=str(bin_dir)+os.pathsep+env['PATH'])
            def cli(*args):
                result=subprocess.run(['acpx','--cwd',td,'--format','json','omp',*args],
                                      env=env,capture_output=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr.decode())
                return json.loads(result.stdout)
            made=cli('sessions','new','--name','fixture-alias')
            target=dict(task='fixture-alias-task',turn=1,record=made['acpxRecordId'],
                        acp=made['acpxSessionId'],cwd=td,session='fixture-alias',agent='omp')
            probe="""const {loadRuntime}=require('./shared-prompt.cjs');
                (async()=>{const p=JSON.parse(process.argv[1]);const s=await loadRuntime('0.22.0');
                const r=s.createSharedAcpRuntime({cwd:p.cwd,permissionMode:'approve-reads'});
                try{const alias=await r.findSession({sessionKey:p.session,agent:'omp',cwd:p.cwd});
                const exact=await r.findSession({sessionKey:p.session,agent:'omp acp',cwd:p.cwd});
                if(alias!==undefined||exact?.acpxRecordId!==p.record||exact.backendSessionId!==p.acp||
                   exact.cwd!==p.cwd||exact.sessionKey!==p.session)throw Error('ALIAS_REPRO_MISMATCH');
                const t=r.startTurn({handle:exact,text:'SYNTHETIC ONLY',requestId:p.task+':1',mode:'prompt'});
                for await(const e of t.events){};if((await t.result).status!=='completed')throw Error('TURN_FAILED');
                }finally{await r.shutdown();}
                const traceBefore=require('node:fs').readFileSync(process.argv[2],'utf8');
                for(const wrong of [{...p,agent:'fixture-wrong-agent'}, {...p,session:'wrong-name'}]){
                  let rejected=false;try{await require('./release-backend.cjs').closeTarget(wrong);}
                  catch(e){if(e.message!=='NAME_SCOPE_MISMATCH')throw e;rejected=true;}
                  if(!rejected)throw Error('WRONG_SCOPE_ACCEPTED');
                }
                if(require('node:fs').readFileSync(process.argv[2],'utf8')!==traceBefore)
                  throw Error('WRONG_SCOPE_EXECUTED_AGENT');
                const result=await require('./release-backend.cjs').closeTarget(p);
                console.log(JSON.stringify({aliasMissing:true,exactCommandMatched:true,verified:result.verified}));
                })().catch(e=>{console.error(e.message);process.exitCode=1;});"""
            try:
                result=subprocess.run(['node','-e',probe,json.dumps(target),str(trace)],env=env,
                                      capture_output=True,timeout=45)
                self.assertEqual(result.returncode,0,result.stderr.decode())
                self.assertEqual(json.loads(result.stdout),dict(aliasMissing=True,exactCommandMatched=True,verified=True))
                self.assertEqual(cli('status','-s',target['session'])['status'],'no-session')
            finally:
                if cli('status','-s',target['session'])['status']!='no-session':
                    cli('sessions','close',target['session'])

if __name__ == '__main__':unittest.main()
