"""Public offline Swarm bridge with native ACP fixture, no provider."""
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

class SwarmThinTests(unittest.TestCase):
    @contextlib.contextmanager
    def workspace(self, fault='normal'):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            executable=root/'agy'
            executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            with patch.dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH'],AGY_NATIVE_FIXTURE=directory,
                    AGY_SWARM_THIN=fault), patch('updates.DEFAULT_LOCK_ROOT',root/'locks'), \
                    patch('updates.ensure_current',return_value={'decision':'fixture'}),contextlib.redirect_stdout(io.StringIO()):
                c=Collaboration(root/'tasks.db',launch=lambda task:None)
                client=c.connect('hermes')['client_id']
                task=c.start(client,agent='agy',model='ocs',cwd=directory,prompt='Write the requested short result',write=True)
                thread=threading.Thread(target=run_task,args=(c.store.path,task['id']),kwargs=dict(turn=1,launch_id=task['launch_id'],epoch=1))
                thread.start()
                try:yield root,c,client,task,thread
                finally:
                    if thread.is_alive():c.cancel(client,task['id']);thread.join(5)
                    self.assertFalse(thread.is_alive())

    def test_native_public_answer_without_member_receipts_completes_and_closes(self):
        with self.workspace() as (root,c,client,task,thread):
            thread.join(5)
            self.assertFalse(thread.is_alive())
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'completed',status.get('settlement_note'))
            self.assertEqual((root/'result.txt').read_text(),'initial')
            self.assertEqual([e['text'] for e in c.events(client,task['id']) if e['kind']=='answer'],['Native final initial'])
            prompt=(root/'prompt').read_text()
            self.assertTrue(prompt.startswith('/swarm\n'))
            self.assertNotIn('CALLCLI_RESULT',prompt)
            self.assertNotIn('report to @coordinator',prompt)
            self.assertNotIn('agy_worker_model',status)
            self.assertFalse(status['native_cleanup']['all_descendants_proven_stopped'])
            self.assertEqual(c.close(client,task['id'])['state'],'closed')

    def test_question_answer_and_revision_stay_on_original_owner(self):
        with self.workspace('question') as (root,c,client,task,thread):
            deadline=time.monotonic()+4
            while not c.status(client,task['id']).get('questions') and time.monotonic()<deadline: time.sleep(.01)
            question=c.status(client,task['id'])['questions'][0]
            c.dialogue(client,task['id'],'use revised label',expected_turn=1,command_id='revision',question_id=question)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'completed',status.get('settlement_note'))
            self.assertEqual(status['questions'],[])
            self.assertEqual(status['commands']['revision']['status'],'returned')
            self.assertEqual((root/'result.txt').read_text(),'revised')
            self.assertEqual(status['native_result']['interaction_seq'],1)
            self.assertEqual(c.close(client,task['id'])['state'],'closed')
            requests=[json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
            self.assertEqual(sum(r.get('method')=='session/new' for r in requests),1)
            self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests),2)
            self.assertFalse(any(r.get('method')=='session/cancel' for r in requests))

    def test_cancel_is_forwarded_once_without_business_or_member_cleanup_replay(self):
        with self.workspace('block') as (root,c,client,task,thread):
            deadline=time.monotonic()+4
            while not (root/'active').exists() and time.monotonic()<deadline: time.sleep(.01)
            self.assertTrue((root/'active').exists())
            c.cancel(client,task['id'])
            thread.join(5)
            self.assertFalse(thread.is_alive())
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'unknown')
            self.assertIn('stop unverified',status.get('settlement_note',''))
            self.assertIs(status['native_owner_exit']['forced'],False)
            self.assertEqual(status['native_owner_exit']['worker_fate'],'unknown')
            requests=[json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
            self.assertEqual(sum(r.get('method')=='session/cancel' for r in requests),1)
            self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests),1)
            self.assertFalse(any(r.get('method')=='session/close' for r in requests))
            with self.assertRaises(ValueError): c.start(client,prompt='conflicting writer',cwd=str(root),write=True)

    def test_fatal_and_empty_response_never_become_success(self):
        for fault in ('fatal','empty'):
            with self.subTest(fault=fault),self.workspace(fault) as (root,c,client,task,thread):
                thread.join(5)
                self.assertFalse(thread.is_alive())
                status=c.status(client,task['id'])
                self.assertEqual(status['state'],'unknown')
                self.assertNotIn('native_cleanup',status)
                self.assertEqual(c.close(client,task['id'])['state'],'unknown')

    def test_missing_swarm_command_never_sends_plain_prompt(self):
        with self.workspace('no-command') as (root,c,client,task,thread):
            thread.join(4)
            self.assertFalse(thread.is_alive())
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'unknown')
            self.assertIn('not advertised',status.get('settlement_note',''))
            requests=[json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
            self.assertFalse(any(r.get('method')=='session/prompt' for r in requests))

    def test_active_public_tool_retains_owner_and_writer_until_result(self):
        with self.workspace('async') as (root,c,client,task,thread):
            deadline=time.monotonic()+4
            while not any(e['kind']=='stage' for e in c.events(client,task['id'])) and time.monotonic()<deadline: time.sleep(.01)
            self.assertEqual(c.status(client,task['id'])['state'],'running')
            other=c.connect('hermes')['client_id']
            with self.assertRaises(PermissionError): c.events(other,task['id'])
            with self.assertRaises(ValueError): c.start(client,prompt='writer conflict',cwd=str(root),write=True)
            (root/'finish').touch()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(c.status(client,task['id'])['state'],'completed')
            self.assertEqual((root/'result.txt').read_text(),'initial')

    def test_enriched_old_tool_does_not_erase_complete_public_answer(self):
        with self.workspace('enrich') as (root,c,client,task,thread):
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(c.status(client,task['id'])['state'],'completed')

    def test_late_old_answer_cannot_complete_unanswered_revision(self):
        with self.workspace('late-old') as (root,c,client,task,thread):
            deadline=time.monotonic()+4
            while not c.status(client,task['id']).get('questions') and time.monotonic()<deadline: time.sleep(.01)
            q=c.status(client,task['id'])['questions'][0]
            c.dialogue(client,task['id'],'new requirement',expected_turn=1,command_id='new',question_id=q)
            deadline=time.monotonic()+4
            while c.status(client,task['id'])['commands']['new']['status']!='returned' and time.monotonic()<deadline: time.sleep(.01)
            time.sleep(.3)
            self.assertEqual(c.status(client,task['id'])['state'],'running')
            self.assertNotIn('native_cleanup',c.status(client,task['id']))
            c.cancel(client,task['id']);thread.join(4)

    def test_unterminated_background_final_answer_is_published_before_completion(self):
        with self.workspace('replay-no-newline') as (root,c,client,task,thread):
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(c.status(client,task['id'])['state'],'completed')
            self.assertEqual([e['text'] for e in c.events(client,task['id']) if e['kind']=='answer'],['Native final replay'])

    def test_board_working_progress_keeps_owner_and_accepts_revision_until_final_result(self):
        with self.workspace('board-working') as (root,c,client,task,thread):
            deadline=time.monotonic()+4
            while not any('Run /swarm board' in e['text'] for e in c.events(client,task['id'])) and time.monotonic()<deadline: time.sleep(.01)
            time.sleep(.2)
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'running')
            self.assertNotIn('native_result',status)
            self.assertNotIn('native_cleanup',status)
            self.assertTrue(status['native_input']['accepting'])
            requests=[json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
            self.assertFalse(any(r.get('method')=='session/close' for r in requests))
            c.dialogue(client,task['id'],'return actual revised result',expected_turn=1,command_id='actual')
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(c.status(client,task['id'])['state'],'completed')
            self.assertEqual((root/'result.txt').read_text(),'revised')
            self.assertEqual(c.close(client,task['id'])['state'],'closed')

    def test_structured_pending_and_board_only_each_keep_dialogue_open(self):
        for fault in ('status-working','board-only'):
            with self.subTest(fault=fault),self.workspace(fault) as (root,c,client,task,thread):
                deadline=time.monotonic()+4
                while not c.events(client,task['id']) or not any(e['kind']=='answer' for e in c.events(client,task['id'])):
                    if time.monotonic()>deadline: self.fail('Native progress missing')
                    time.sleep(.01)
                time.sleep(.15)
                status=c.status(client,task['id'])
                self.assertEqual(status['state'],'running')
                self.assertTrue(status['native_input']['accepting'])
                self.assertNotIn('native_cleanup',status)
                c.dialogue(client,task['id'],'return final revised result',expected_turn=1,command_id='finish')
                thread.join(5)
                self.assertEqual(c.status(client,task['id'])['state'],'completed')
                self.assertEqual((root/'result.txt').read_text(),'revised')

    def test_normal_final_answer_may_mention_board_instruction(self):
        with self.workspace('final-board-mention') as (root,c,client,task,thread):
            thread.join(4)
            self.assertFalse(thread.is_alive())
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'completed')
            self.assertIn('Run /swarm board',status['native_result']['result'])
            self.assertEqual(c.close(client,task['id'])['state'],'closed')

    def test_new_status_observation_supersedes_historical_working(self):
        with self.workspace('status-new-id') as (root,c,client,task,thread):
            deadline=time.monotonic()+4
            while not any(e['kind']=='answer' for e in c.events(client,task['id'])) and time.monotonic()<deadline: time.sleep(.01)
            time.sleep(.15)
            self.assertEqual(c.status(client,task['id'])['state'],'running')
            c.dialogue(client,task['id'],'return final revised result',expected_turn=1,command_id='finish')
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(c.status(client,task['id'])['state'],'completed')
            self.assertEqual(c.close(client,task['id'])['state'],'closed')
