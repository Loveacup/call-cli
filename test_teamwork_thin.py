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
                expected_turn=1,command_id='interview-answer',blocker_interaction_seq=0)
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
            c.dialogue(client,task['id'],'Audience and checks supplied',expected_turn=1,command_id='answer-old',blocker_interaction_seq=0)
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
            self.assertEqual(status['state'],'cancelled')
            self.assertTrue(status['native_cancel']['session_unloaded'])
            self.assertFalse(status['native_cleanup']['all_descendants_proven_stopped'])
            requests=self.requests(root)
            self.assertEqual(sum(r.get('method')=='session/cancel' for r in requests),1)
            self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests),1)
            self.assertEqual(sum(r.get('method')=='session/close' for r in requests),1)
            self.assertEqual(c.close(client,task['id'])['state'],'closed')

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
            self.assertEqual(status['state'],'cancelled')
            self.assertEqual(status['commands']['answer']['status'],'cancelled')
            self.assertTrue(status['native_cancel']['session_unloaded'])
            self.assertFalse(status['native_cleanup']['all_descendants_proven_stopped'])
            requests=self.requests(root)
            self.assertEqual(sum(r.get('method')=='session/cancel' for r in requests),1)
            self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests),2)
            self.assertEqual(sum(r.get('method')=='session/close' for r in requests),1)
            self.assertEqual(c.close(client,task['id'])['state'],'closed')

    def test_replay_only_delegation_does_not_close_before_async_question(self):
        with self.workspace('replay-only') as (root,c,client,task,thread):
            self.until(lambda:any(e['kind']=='answer' for e in c.events(client,task['id'])))
            time.sleep(.2)
            self.assertEqual(c.status(client,task['id'])['state'],'running')
            (root/'interview').touch()
            self.until(lambda:c.status(client,task['id']).get('questions'))
            self.assertNotIn('native_cleanup',c.status(client,task['id']))

    def test_coexisting_formal_q1_q4_and_incomplete_question_never_settle_until_recovered_and_answered(self):
        with self.workspace('coexisting-q1-q4') as (root,c,client,task,thread):
            self.until(lambda:any(e['kind']=='answer' for e in c.events(client,task['id'])))
            (root/'interview').touch()
            self.until(lambda:len(c.status(client,task['id']).get('questions',[]))==4
                and bool(c.status(client,task['id']).get('native_observation',{}).get('question_blockers')))
            q1_4=list(c.status(client,task['id'])['questions'])
            self.assertEqual(len(q1_4),4)

            # 1) Answering Q1-Q4 + emitting a final text line clears formal questions/answers,
            # but incomplete_question at interaction_seq=0 MUST still block completion.
            c.dialogue(client,task['id'],'answers for Q1-Q4',expected_turn=1,command_id='ans-1-4',question_ids=q1_4)
            self.until(lambda:c.status(client,task['id'])['commands']['ans-1-4']['status']=='returned')
            self.until(lambda:any(e['kind']=='answer' and 'Summary after Q1-Q4.' in e['text']
                for e in c.events(client,task['id'])))
            thread.join(.3)
            st1=c.status(client,task['id'])
            self.assertEqual(st1['questions'],[])
            self.assertEqual(st1['answers'],{qid:'answers for Q1-Q4' for qid in q1_4})
            self.assertEqual(st1['state'],'running',st1.get('settlement_note'))
            blockers1=st1.get('native_observation',{}).get('question_blockers',[])
            self.assertEqual([(b['interaction_seq'],b['reason']) for b in blockers1],[(0,'incomplete_question')])
            self.assertFalse(any(r.get('method')=='session/close' for r in self.requests(root)))

            # 2) Unrelated subsequent unbound revision while questions==[] + final text MUST NOT clear blocker.
            c.dialogue(client,task['id'],'unrelated formatting note',expected_turn=1,command_id='unrelated-rev')
            self.until(lambda:c.status(client,task['id'])['commands']['unrelated-rev']['status']=='returned')
            self.until(lambda:any(e['kind']=='answer' and 'Unrelated revision note applied.' in e['text']
                for e in c.events(client,task['id'])))
            thread.join(.3)
            st2=c.status(client,task['id'])
            self.assertEqual(st2['state'],'running',st2.get('settlement_note'))
            blockers2=st2.get('native_observation',{}).get('question_blockers',[])
            self.assertEqual([(b['interaction_seq'],b['reason']) for b in blockers2],[(0,'incomplete_question')])
            self.assertFalse(any(r.get('method')=='session/close' for r in self.requests(root)))

            # 3) Prefix growth on slot 0 publishes Q5 under interaction_seq=0, clears blockers, and settles after answer.
            (root/'recover-q5').touch()
            self.until(lambda:len(c.status(client,task['id']).get('questions',[]))==1
                and c.status(client,task['id']).get('native_observation',{}).get('question_blockers')==[])
            q5_events=[e for e in c.events(client,task['id']) if e['kind']=='question' and e['text']=='Q5 Confirm scope?']
            self.assertEqual(len(q5_events),1)
            self.assertEqual(q5_events[0]['interaction_seq'],0)
            q5=q5_events[0]['token']
            c.dialogue(client,task['id'],'confirm Q5 scope',expected_turn=1,command_id='ans-5',question_id=q5)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            final=c.status(client,task['id'])
            self.assertEqual(final['state'],'completed',final.get('settlement_note'))
            self.assertEqual(final['questions'],[])
            self.assertEqual(final['native_observation']['question_blockers'],[])
            self.assertEqual(final['answers'],{**{qid:'answers for Q1-Q4' for qid in q1_4},q5:'confirm Q5 scope'})
            self.assertEqual(sum(r.get('method')=='session/close' for r in self.requests(root)),1)
            self.assertEqual(c.close(client,task['id'])['state'],'closed')
            self.assertEqual(c.close(client,task['id'])['state'],'closed')

    def test_formal_q1_q4_partial_and_new_q5_lifecycle_blocks_until_all_answered(self):
        with self.workspace('multi-q1-q4') as (root,c,client,task,thread):
            self.until(lambda:any(e['kind']=='answer' for e in c.events(client,task['id'])))
            (root/'interview').touch()
            self.until(lambda:len(c.status(client,task['id']).get('questions',[]))==4)
            q1_4=list(c.status(client,task['id'])['questions'])

            # Partial subset [q1, q2] keeps [q3, q4] blocking even with final text
            c.dialogue(client,task['id'],'partial 1-2',expected_turn=1,command_id='ans-1-2',question_ids=q1_4[:2])
            self.until(lambda:c.status(client,task['id'])['commands']['ans-1-2']['status']=='returned')
            self.until(lambda:any(e['kind']=='answer' and 'Partial summary after Q1-Q2.' in e['text']
                for e in c.events(client,task['id'])))
            thread.join(.3)
            st1=c.status(client,task['id'])
            self.assertEqual(st1['state'],'running')
            self.assertEqual(st1['questions'],q1_4[2:])
            self.assertEqual(st1['answers'],{q1_4[0]:'partial 1-2',q1_4[1]:'partial 1-2'})
            self.assertFalse(any(r.get('method')=='session/close' for r in self.requests(root)))

            # Answering [q3, q4] while new formal Q5 arrives still blocks completion until Q5 answered
            c.dialogue(client,task['id'],'partial 3-4',expected_turn=1,command_id='ans-3-4',question_ids=q1_4[2:])
            self.until(lambda:c.status(client,task['id'])['commands']['ans-3-4']['status']=='returned')
            self.until(lambda:len(c.status(client,task['id']).get('questions',[]))==1)
            thread.join(.3)
            st2=c.status(client,task['id'])
            self.assertEqual(st2['state'],'running')
            q5=st2['questions'][0]
            self.assertNotIn(q5,q1_4)
            self.assertFalse(any(r.get('method')=='session/close' for r in self.requests(root)))

            c.dialogue(client,task['id'],'confirm Q5',expected_turn=1,command_id='ans-5',question_id=q5)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            final=c.status(client,task['id'])
            self.assertEqual(final['state'],'completed',final.get('settlement_note'))
            self.assertEqual(final['questions'],[])
            self.assertEqual(final['answers'],{
                q1_4[0]:'partial 1-2',q1_4[1]:'partial 1-2',
                q1_4[2]:'partial 3-4',q1_4[3]:'partial 3-4',
                q5:'confirm Q5',
            })
            self.assertEqual(sum(r.get('method')=='session/close' for r in self.requests(root)),1)
            self.assertEqual(c.close(client,task['id'])['state'],'closed')
            self.assertEqual(c.close(client,task['id'])['state'],'closed')

    def test_same_interaction_unbound_dialogue_with_intervening_tool_call_supersedes_deferred_blocker(self):
        with self.workspace('deferred-tool-before-user') as (root,c,client,task,thread):
            self.until(lambda:any(e['kind']=='answer' for e in c.events(client,task['id'])))
            (root/'interview').touch()
            self.until(lambda:bool(c.status(client,task['id']).get('native_observation',{}).get('question_blockers')))
            self.assertEqual(c.status(client,task['id'])['questions'],[])
            c.dialogue(client,task['id'],'reviewed defaults despite tool before user chunk',
                expected_turn=1,command_id='reviewed-unbound',blocker_interaction_seq=0)
            thread.join(2.5)
            self.assertFalse(thread.is_alive(),'Explicit blocker supersede dialogue after intervening tool_call remained blocked')
            final=c.status(client,task['id'])
            self.assertEqual(final['state'],'completed',final.get('settlement_note'))
            self.assertEqual(final['native_observation']['question_blockers'],[])
            self.assertEqual(c.close(client,task['id'])['state'],'closed')
            self.assertEqual(c.close(client,task['id'])['state'],'closed')

    def test_unrelated_unbound_dialogue_same_and_cross_interaction_never_clears_fragment(self):
        with self.workspace('deferred-tool-before-user') as (root,c,client,task,thread):
            self.until(lambda:any(e['kind']=='answer' for e in c.events(client,task['id'])))
            (root/'interview').touch()
            self.until(lambda:bool(c.status(client,task['id']).get('native_observation',{}).get('question_blockers')))
            # 1) Same-interaction unrelated unbound revision must NOT clear blocker
            c.dialogue(client,task['id'],
                'Only change the output heading to BLUE. This is unrelated to the audience/scope question; do not answer or withdraw it.',
                expected_turn=1,command_id='unrelated-format')
            self.until(lambda:c.status(client,task['id'])['commands']['unrelated-format']['status']=='returned')
            self.until(lambda:any(e['kind']=='answer' and 'Final reviewed result after intervening tool.' in e['text']
                for e in c.events(client,task['id'])))
            thread.join(.3)
            st1=c.status(client,task['id'])
            self.assertEqual(st1['state'],'running',st1.get('settlement_note'))
            self.assertEqual([(b['interaction_seq'],b['reason']) for b in st1['native_observation']['question_blockers']],
                             [(0,'incomplete_question')])
            self.assertEqual(sum(r.get('method')=='session/close' for r in self.requests(root)),0)
            self.assertFalse(any('Incomplete question superseded' in e.get('text','') for e in c.events(client,task['id'])))
            with self.assertRaises(ValueError):
                c.close(client,task['id'])

            # 2) Cross-interaction unrelated unbound revision must also preserve blocker
            c.dialogue(client,task['id'],'Second unrelated formatting adjustment.',expected_turn=1,command_id='unrelated-second')
            self.until(lambda:c.status(client,task['id'])['commands']['unrelated-second']['status']=='returned')
            thread.join(.3)
            st2=c.status(client,task['id'])
            self.assertEqual(st2['state'],'running',st2.get('settlement_note'))
            self.assertEqual([(b['interaction_seq'],b['reason']) for b in st2['native_observation']['question_blockers']],
                             [(0,'incomplete_question')])
            self.assertEqual(sum(r.get('method')=='session/close' for r in self.requests(root)),0)

            # 3) Explicit blocker_interaction_seq=0 settles and allows idempotent close()
            c.dialogue(client,task['id'],'Reviewed earlier audience/scope fragment at interaction 0; apply defaults.',
                expected_turn=1,command_id='explicit-supersede',blocker_interaction_seq=0)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            final=c.status(client,task['id'])
            self.assertEqual(final['state'],'completed',final.get('settlement_note'))
            self.assertEqual(final.get('answers',{}),{})
            self.assertEqual(final['native_observation']['question_blockers'],[])
            self.assertEqual(sum(r.get('method')=='session/close' for r in self.requests(root)),1)
            self.assertEqual(c.close(client,task['id'])['state'],'closed')
            self.assertEqual(c.close(client,task['id'])['state'],'closed')

    def test_coexisting_formal_q1_q4_and_non_growing_fragment_settles_after_explicit_blocker_supersede(self):
        with self.workspace('coexisting-q1-q4') as (root,c,client,task,thread):
            self.until(lambda:any(e['kind']=='answer' for e in c.events(client,task['id'])))
            (root/'interview').touch()
            self.until(lambda:len(c.status(client,task['id']).get('questions',[]))==4
                and bool(c.status(client,task['id']).get('native_observation',{}).get('question_blockers')))
            ids=list(c.status(client,task['id'])['questions'])

            # 1) Answer formal Q1-Q4; blocker 0 remains and questions==[]
            c.dialogue(client,task['id'],'answers for Q1-Q4',expected_turn=1,command_id='ans-1-4',question_ids=ids)
            self.until(lambda:c.status(client,task['id'])['commands']['ans-1-4']['status']=='returned')
            self.until(lambda:any(e['kind']=='answer' and 'Summary after Q1-Q4.' in e['text']
                for e in c.events(client,task['id'])))
            thread.join(.3)
            st1=c.status(client,task['id'])
            self.assertEqual(st1['state'],'running')
            self.assertEqual(st1['questions'],[])
            self.assertEqual([(b['interaction_seq'],b['reason']) for b in st1['native_observation']['question_blockers']],
                             [(0,'incomplete_question')])

            # 2) Ordinary unbound dialogue even after formal questions==[] does NOT clear blocker 0
            c.dialogue(client,task['id'],'unrelated formatting pass',expected_turn=1,command_id='unrelated-rev')
            self.until(lambda:c.status(client,task['id'])['commands']['unrelated-rev']['status']=='returned')
            self.until(lambda:any(e['kind']=='answer' and 'Unrelated revision note applied.' in e['text']
                for e in c.events(client,task['id'])))
            thread.join(.3)
            st2=c.status(client,task['id'])
            self.assertEqual(st2['state'],'running')
            self.assertEqual([(b['interaction_seq'],b['reason']) for b in st2['native_observation']['question_blockers']],
                             [(0,'incomplete_question')])
            with self.assertRaises(ValueError):
                c.close(client,task['id'])

            # 3) Explicit blocker_interaction_seq=0 settles without synthetic token answer in answers
            c.dialogue(client,task['id'],'Explicit review of interaction 0 fragment: confirm Q1-Q5 scope defaults.',
                expected_turn=1,command_id='ans-5',blocker_interaction_seq=0)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            final=c.status(client,task['id'])
            self.assertEqual(final['state'],'completed',final.get('settlement_note'))
            self.assertEqual(final['questions'],[])
            self.assertEqual(final['native_observation']['question_blockers'],[])
            self.assertEqual(final['answers'],{qid:'answers for Q1-Q4' for qid in ids})
            self.assertEqual(sum(r.get('method')=='session/close' for r in self.requests(root)),1)
            # Settled command_id retry remains idempotent
            retried=c.dialogue(client,task['id'],'Explicit review of interaction 0 fragment: confirm Q1-Q5 scope defaults.',
                expected_turn=1,command_id='ans-5',blocker_interaction_seq=0)
            self.assertEqual(retried['state'],'completed')
            self.assertEqual(c.close(client,task['id'])['state'],'closed')
            self.assertEqual(c.close(client,task['id'])['state'],'closed')
