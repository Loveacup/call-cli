"""A non-ACP adapter exercises the same public task lifecycle as native backends."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace


class BackendContractTests(unittest.TestCase):
    def test_non_acp_backend_questions_intervention_isolation_and_close(self):
        from backends import BACKENDS, TurnResult
        from collab import Collaboration
        from runner import run_task
        active = threading.Event()
        class FakeBackend:
            def prepare_start(self, model, effort, approval):
                return {'model': model} if model else {}

            def run(self, store, task, emit):
                if task['prompt'] == 'ask':
                    emit('question', 'Which output?')
                    return TurnResult('waiting_reply', 0, 'end_turn')
                if task['prompt'] == 'block':
                    active.set()
                    while store.get(task['id'])['state'] != 'cancelling':
                        threading.Event().wait(.01)
                    return TurnResult('cancelled', 0, 'cancelled')
                emit('answer', 'revised output: '+task['prompt'])
                return TurnResult('completed', 0, 'end_turn')

            def control(self, task, action, timeout=20):
                return SimpleNamespace(returncode=0, stdout=b'{"action":"status_snapshot","status":"no-session"}')

        BACKENDS['fixture'] = FakeBackend()
        self.addCleanup(BACKENDS.pop, 'fixture')
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            owner = c.connect('omp')['client_id']
            other = c.connect('pi')['client_id']
            task = c.start(owner, agent='fixture', cwd=root, prompt='ask', model='fixture-model', write=True)
            tid = task['id']
            with self.assertRaises(PermissionError):
                c.events(other, tid)
            with self.assertRaises(ValueError):
                c.start(other, agent='omp', cwd=root, prompt='conflicting writer', write=True)
            self.assertEqual(run_task(c.store.path, tid), 0)
            first = c.wait(owner, tid, timeout=0)
            question = next(e for e in first['events'] if e['kind'] == 'question')
            c.reply(owner, tid, question['token'], 'block')
            with ThreadPoolExecutor(max_workers=1) as pool:
                worker = pool.submit(run_task, c.store.path, tid)
                self.assertTrue(active.wait(2))
                c.intervene(owner, tid, 'human revision', command_id='revision')
                self.assertEqual(worker.result(timeout=3), 0)
            c.wait(owner, tid, after=first['after'], timeout=0)
            self.assertEqual(run_task(c.store.path, tid), 0)
            final = c.wait(owner, tid, after=first['after'], timeout=0)
            self.assertEqual((final['task']['state'], final['task']['turn']), ('completed', 3))
            self.assertEqual(final['task']['session'], task['session'])
            self.assertEqual(final['task']['model'], 'fixture-model')
            self.assertEqual([e['text'] for e in final['events'] if e['kind']=='answer'], ['revised output: human revision'])
            self.assertEqual(c.close(owner, tid)['state'], 'closed')
