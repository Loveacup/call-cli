"""Synthetic public ACP contracts, not real Teamwork phase evidence."""
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


class TeamworkThinTests(unittest.TestCase):
    @contextlib.contextmanager
    def workspace(self, fault='interview', explicit=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            executable=root/'agy'
            executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            with patch.dict(os.environ,PATH=str(root)+os.pathsep+os.environ['PATH'],
                    AGY_NATIVE_FIXTURE=directory,AGY_TEAMWORK_THIN=fault), \
                    patch('updates.DEFAULT_LOCK_ROOT',root/'locks'), \
                    patch('updates.ensure_current',return_value={'decision':'fixture'}),contextlib.redirect_stdout(io.StringIO()):
                c=Collaboration(root/'tasks.db',launch=lambda task:None)
                client=c.connect('hermes')['client_id']
                fields={'model':'gemini-pro-orchestrator','agy_mode':'teamwork'} if explicit else {'model':'oct'}
                task=c.start(client,agent='agy',cwd=directory,prompt='Prepare a brief and review it using native Teamwork',write=True,**fields)
                thread=threading.Thread(target=run_task,args=(c.store.path,task['id']),
                    kwargs=dict(turn=1,launch_id=task['launch_id'],epoch=1))
                thread.start()
                try: yield root,c,client,task,thread
                finally:
                    if thread.is_alive(): c.cancel(client,task['id']);thread.join(5)
                    self.assertFalse(thread.is_alive())

    def until(self, predicate):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            if predicate(): return
            time.sleep(.01)
        self.fail('Expected public state did not arrive')

    def requests(self, root):
        return [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]

    def test_delegation_waits_for_async_interview_replies_revision_and_final_close(self):
        with self.workspace() as (root,c,client,task,thread):
            self.until(lambda:any(e['kind']=='answer' for e in c.events(client,task['id'])))
            time.sleep(.2)
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'running',status.get('settlement_note'))
            self.assertTrue(status['native_input']['accepting'])
            self.assertNotIn('native_result',status)
            self.assertNotIn('native_cleanup',status)
            self.assertFalse(any(r.get('method')=='session/close' for r in self.requests(root)))
            other=c.connect('hermes')['client_id']
            with self.assertRaises(PermissionError):
                c.dialogue(other,task['id'],'wrong source',expected_turn=1,command_id='foreign')
            with self.assertRaises(ValueError):
                c.start(client,prompt='conflicting writer',cwd=str(root),write=True)
            (root/'interview').touch()
            self.until(lambda:c.status(client,task['id']).get('questions'))
            question=c.status(client,task['id'])['questions'][0]
            c.dialogue(client,task['id'],'initial label',expected_turn=1,question_id=question,command_id='answer')
            self.until(lambda:c.status(client,task['id'])['commands']['answer']['status']=='returned')
            self.assertEqual(c.status(client,task['id'])['state'],'running')
            c.dialogue(client,task['id'],'use revised label',expected_turn=1,command_id='revision')
            self.until(lambda:c.status(client,task['id'])['commands']['revision']['status']=='returned')
            self.assertEqual(c.status(client,task['id'])['state'],'running')
            (root/'finish').touch()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'completed',status.get('settlement_note'))
            self.assertEqual(status['questions'],[])
            self.assertEqual((root/'brief.txt').read_text(),'use revised label')
            self.assertEqual(status['native_result']['interaction_seq'],2)
            self.assertIn('Final review',status['native_result']['result'])
            self.assertEqual(status['native_cleanup']['mode'],'teamwork')
            self.assertFalse(status['native_cleanup']['all_descendants_proven_stopped'])
            answers=[e['text'] for e in c.events(client,task['id']) if e['kind']=='answer']
            self.assertEqual(sum('Final review' in text for text in answers),1)
            self.assertEqual(c.close(client,task['id'])['state'],'closed')
            self.assertEqual(c.close(client,task['id'])['state'],'closed')
            requests=self.requests(root)
            prompts=[r for r in requests if r.get('method')=='session/prompt']
            self.assertEqual(len(prompts),3)
            self.assertTrue(prompts[0]['params']['prompt'][0]['text'].startswith('/teamwork\n'))
            self.assertEqual(len({p['params']['sessionId'] for p in prompts}),1)
            self.assertEqual(sum(r.get('method')=='session/new' for r in requests),1)
            self.assertEqual(sum(r.get('method')=='session/close' for r in requests),1)

    def test_reviewed_unterminated_interview_allows_public_failure_settlement(self):
        # The public report omitted the Q1 body; this is a synthetic equivalent.
        with self.workspace('unterminated-failure') as (root,c,client,task,thread):
            (root/'interview').touch()
            self.until(lambda:c.status(client,task['id']).get('native_observation',{}).get('question_blockers'))
            self.assertEqual(c.status(client,task['id'])['questions'],[])
            c.dialogue(client,task['id'],'First-time maintainers; delegation, revisions, artifacts and close',
                expected_turn=1,command_id='interview-answer')
            self.until(lambda:c.status(client,task['id'])['commands']['interview-answer']['status']=='returned')
            c.dialogue(client,task['id'],'label: revised; report failure without further delegation',
                expected_turn=1,command_id='revision')
            self.until(lambda:c.status(client,task['id'])['commands']['revision']['status']=='returned')
            (root/'finish').touch()
            thread.join(5)
            self.assertFalse(thread.is_alive(),'Old incomplete interview blocked the returned failure report')
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'completed',status.get('settlement_note'))
            self.assertEqual(status['native_result']['interaction_seq'],2)
            self.assertIn('60s timeout',status['native_result']['result'])
            self.assertEqual(status['native_observation']['question_blockers'],[])
            self.assertEqual(status.get('answers',{}),{},'Reviewed dialogue is not a token answer')
            self.assertFalse((root/'brief.md').exists())
            self.assertEqual(c.close(client,task['id'])['state'],'closed')
            requests=self.requests(root)
            self.assertEqual(sum(r.get('method')=='session/close' for r in requests),1)
            self.assertFalse(any(r.get('method')=='session/cancel' for r in requests))

    def test_returned_old_interview_reply_does_not_answer_new_question(self):
        with self.workspace('unterminated-failure') as (root,c,client,task,thread):
            (root/'followup').touch()
            (root/'interview').touch()
            self.until(lambda:c.status(client,task['id']).get('native_observation',{}).get('question_blockers'))
            c.dialogue(client,task['id'],'Audience and checks supplied',expected_turn=1,command_id='answer-old')
            self.until(lambda:c.status(client,task['id'])['commands']['answer-old']['status']=='returned')
            self.until(lambda:c.status(client,task['id'])['questions'])
            question=[e for e in c.events(client,task['id']) if e['kind']=='question'][0]
            self.assertEqual((question['text'],question['interaction_seq']),('Q2 Confirm failure report?',1))
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'running')
            self.assertIn(question['token'],status['questions'])
            self.assertNotIn(question['token'],status.get('answers',{}))
            self.assertNotIn('native_cleanup',status)
            c.dialogue(client,task['id'],'Confirm failure',expected_turn=1,command_id='answer-new',question_id=question['token'])
            self.until(lambda:c.status(client,task['id'])['commands']['answer-new']['status']=='returned')
            (root/'finish').touch()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'completed',status.get('settlement_note'))
            self.assertEqual(status['answers'],{question['token']:'Confirm failure'})
            self.assertEqual(c.close(client,task['id'])['state'],'closed')

    def test_missing_teamwork_advertisement_never_falls_back_to_swarm(self):
        with self.workspace('no-command',explicit=True) as (root,c,client,task,thread):
            thread.join(4)
            self.assertFalse(thread.is_alive())
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'unknown')
            self.assertIn('teamwork command not advertised',status.get('settlement_note',''))
            self.assertFalse(any(r.get('method')=='session/prompt' for r in self.requests(root)))

    def test_delegation_without_public_question_or_final_remains_observable_and_cancel_is_single(self):
        with self.workspace() as (root,c,client,task,thread):
            self.until(lambda:any(e['kind']=='answer' for e in c.events(client,task['id'])))
            time.sleep(.2)
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'running',status.get('settlement_note'))
            self.assertEqual(status['native_observation']['response_pending_reason'],'native_handoff_only')
            c.cancel(client,task['id'])
            thread.join(5)
            self.assertFalse(thread.is_alive())
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'unknown')
            self.assertIs(status['native_owner_exit']['forced'],False)
            requests=self.requests(root)
            self.assertEqual(sum(r.get('method')=='session/cancel' for r in requests),1)
            self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests),1)
            self.assertNotIn('native_cleanup',status)

    def test_cancel_during_dialogue_forwards_once_without_resume_or_close(self):
        with self.workspace('cancel-dialogue') as (root,c,client,task,thread):
            (root/'interview').touch()
            self.until(lambda:c.status(client,task['id']).get('questions'))
            question=c.status(client,task['id'])['questions'][0]
            c.dialogue(client,task['id'],'updated brief',expected_turn=1,
                question_id=question,command_id='answer')
            self.until(lambda:(root/'dialogue-active').exists())
            c.cancel(client,task['id'])
            thread.join(5)
            self.assertFalse(thread.is_alive())
            status=c.status(client,task['id'])
            self.assertEqual(status['state'],'unknown')
            self.assertEqual(status['commands']['answer']['status'],'cancelled')
            self.assertIs(status['native_owner_exit']['forced'],False)
            requests=self.requests(root)
            self.assertEqual(sum(r.get('method')=='session/cancel' for r in requests),1)
            self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests),2)
            self.assertFalse(any(r.get('method')=='session/close' for r in requests))
            self.assertNotIn('native_cleanup',status)

    def test_replay_only_delegation_does_not_close_before_async_question(self):
        with self.workspace('replay-only') as (root,c,client,task,thread):
            self.until(lambda:any(e['kind']=='answer' for e in c.events(client,task['id'])))
            time.sleep(.2)
            self.assertEqual(c.status(client,task['id'])['state'],'running')
            (root/'interview').touch()
            self.until(lambda:c.status(client,task['id']).get('questions'))
            self.assertNotIn('native_cleanup',c.status(client,task['id']))
