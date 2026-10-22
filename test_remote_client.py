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

if __name__=='__main__':unittest.main()
