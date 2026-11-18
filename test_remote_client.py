import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path

class RemoteClientTests(unittest.TestCase):
    def test_ssh_json_reply_is_returned_without_waiting_for_transport_exit(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);fake=p/'ssh'
            fake.write_text('#!'+sys.executable+'\nimport sys,json,time\nprint(json.dumps({"argv":sys.argv[1:]}),flush=True)\ntime.sleep(10)\n')
            fake.chmod(0o700)
            r=subprocess.run(['node',str(Path(__file__).with_name('remote-client.cjs')),'--host','existing-host','--entry','/workspace/collab.py','--db','/workspace/task db.sqlite3','--ssh-bin',str(fake),'--timeout-ms','1200','--','connect','--runtime','pi'],capture_output=True,text=True,timeout=4)
            self.assertEqual(r.returncode,0,r.stderr)
            argv=json.loads(r.stdout)['argv']
            self.assertIn('existing-host',argv)
            self.assertIn("'/workspace/task db.sqlite3'",argv[-1])
            self.assertIn("'connect' '--runtime' 'pi'",argv[-1])

    def test_unknown_transport_result_never_replays_the_command(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);fake=p/'ssh';calls=p/'calls'
            fake.write_text('#!'+sys.executable+'\nimport pathlib,time\np=pathlib.Path('+repr(str(calls))+')\np.write_text(p.read_text()+"x" if p.exists() else "x")\ntime.sleep(10)\n')
            fake.chmod(0o700)
            r=subprocess.run(['node',str(Path(__file__).with_name('remote-client.cjs')),'--host','existing-host','--entry','/workspace/collab.py','--db','/workspace/tasks.sqlite3','--ssh-bin',str(fake),'--timeout-ms','1000','--','list'],capture_output=True,text=True,timeout=4)
            self.assertEqual(r.returncode,1)
            self.assertTrue(json.loads(r.stdout)['do_not_replay'])
            self.assertEqual(calls.read_text(),'x')

    def test_ssh_forwarding_preserves_question_ids_raw_bytes_and_spaces(self):
        import shlex
        import collab
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);fake=p/'ssh'
            fake.write_text('#!'+sys.executable+'\nimport sys,json,time\nprint(json.dumps({"argv":sys.argv[1:]}),flush=True)\ntime.sleep(10)\n')
            fake.chmod(0o700)
            r=subprocess.run(['node',str(Path(__file__).with_name('remote-client.cjs')),'--host','existing-host','--entry','/workspace/collab.py','--db','/workspace/tasks.sqlite3','--ssh-bin',str(fake),'--timeout-ms','5000','--','--client','cid','dialogue','--task','t1','--expected-turn','1','--command-id','d1','--prompt','shared','--question-ids','1:2',' 1:3 '],capture_output=True,text=True,timeout=10)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            remote_cmd=json.loads(r.stdout)['argv'][-1]
            self.assertIn("'--question-ids' '1:2' ' 1:3 '",remote_cmd)
            _, _, exec_tail = remote_cmd.partition('; exec ')
            self.assertEqual(shlex.split(exec_tail),['python3','/workspace/collab.py','--db','/workspace/tasks.sqlite3','--client','cid','dialogue','--task','t1','--expected-turn','1','--command-id','d1','--prompt','shared','--question-ids','1:2',' 1:3 '])

            db_path=p/'tasks.sqlite3'
            c=collab.Collaboration(db_path,launch=lambda task:None)
            cid=c.connect('omp')['client_id']
            task=c.start(cid,agent='agy',model='ocs',prompt='owned',cwd='/project')
            tid=task['id']
            c.store.update(tid,state='running',agy_swarm='native',runner_pid=123)
            c.store.native_input_open(tid,1,task['launch_id'],1)
            c.store.event(tid,'1:2','question','Q1?',turn=1,interaction_seq=0)
            c.store.event(tid,' 1:3 ','question','Q2?',turn=1,interaction_seq=0)

            exec_fake=p/'ssh-exec'
            exec_fake.write_text('#!'+sys.executable+'\nimport subprocess,sys,time\nres=subprocess.run(["/bin/sh","-c",sys.argv[-1]],capture_output=True,text=True)\nsys.stdout.write(res.stdout)\nsys.stdout.flush()\ntime.sleep(10)\n')
            exec_fake.chmod(0o700)
            r2=subprocess.run(['node',str(Path(__file__).with_name('remote-client.cjs')),'--host','existing-host','--entry',str(Path(collab.__file__).resolve()),'--db',str(db_path),'--python',sys.executable,'--ssh-bin',str(exec_fake),'--timeout-ms','5000','--','--client',cid,'dialogue','--task',tid,'--expected-turn','1','--command-id','d1','--prompt','shared','--question-ids','1:2',' 1:3 '],capture_output=True,text=True,timeout=10)
            self.assertEqual(r2.returncode,0,r2.stdout+r2.stderr)
            reply=json.loads(r2.stdout)
            self.assertEqual(reply['commands']['d1']['status'],'accepted')
            self.assertEqual(reply['commands']['d1']['question_ids'],['1:2',' 1:3 '])
            self.assertEqual(c.status(cid,tid)['commands']['d1']['question_ids'],['1:2',' 1:3 '])

    def test_ssh_forwarding_preserves_blocker_interaction_seq_zero(self):
        import collab
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            db_path=p/'tasks.sqlite3'
            c=collab.Collaboration(db_path,launch=lambda task:None)
            cid=c.connect('omp')['client_id']
            task=c.start(cid,agent='agy',model='ocs',prompt='owned',cwd='/project')
            tid=task['id']
            c.store.update(tid,state='running',agy_swarm='native',runner_pid=123,
                           native_observation=dict(turn=1,input_available=True,question_ids=[],
                               question_blockers=[dict(interaction_seq=0,reason='incomplete_question',
                                                       available_actions=['observe','dialogue','cancel'],blocks_completion=True)]))
            c.store.native_input_open(tid,1,task['launch_id'],1)
            exec_fake=p/'ssh-exec'
            exec_fake.write_text('#!'+sys.executable+'\nimport subprocess,sys,time\nres=subprocess.run(["/bin/sh","-c",sys.argv[-1]],capture_output=True,text=True)\nsys.stdout.write(res.stdout)\nsys.stdout.flush()\ntime.sleep(10)\n')
            exec_fake.chmod(0o700)
            r=subprocess.run(['node',str(Path(__file__).with_name('remote-client.cjs')),'--host','existing-host','--entry',str(Path(collab.__file__).resolve()),'--db',str(db_path),'--python',sys.executable,'--ssh-bin',str(exec_fake),'--timeout-ms','5000','--','--client',cid,'dialogue','--task',tid,'--expected-turn','1','--command-id','d-blk0','--prompt','reviewed 0','--blocker-interaction-seq','0'],capture_output=True,text=True,timeout=10)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            reply=json.loads(r.stdout)
            self.assertEqual(reply['commands']['d-blk0']['status'],'accepted')
            self.assertEqual(reply['commands']['d-blk0']['blocker_interaction_seq'],0)
            self.assertEqual(c.status(cid,tid)['commands']['d-blk0']['blocker_interaction_seq'],0)

if __name__=='__main__':unittest.main()
