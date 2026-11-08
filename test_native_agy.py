"""Opt-in live AGY smoke; uses existing login, no tools or file edits requested."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from collab import Collaboration
from runner import control, run_task


@unittest.skipUnless(os.environ.get('RUN_NATIVE_AGY_TESTS') == '1', 'opt-in live model')
class NativeAgyTests(unittest.TestCase):
    def test_explicit_model_effort_and_same_session_continuation(self):
        model = os.environ['CALLCLI_AGY_MODEL']  # caller selects; never a fallback
        effort = os.environ.get('CALLCLI_AGY_EFFORT')
        with tempfile.TemporaryDirectory() as root:
            discovered = json.loads(subprocess.check_output(['node', str(Path(__file__).with_name('models.cjs')), root], timeout=45))
            self.assertIn(model, discovered['availableModelIds'])
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            client = c.connect('codex')['client_id']  # declared, not native Codex provenance
            task = c.start(client, agent='agy', cwd=root, model=model, effort=effort,
                           prompt='Do not use tools. Reply exactly CALLCLI_AGY_READY.', timeout=90)
            try:
                self.assertEqual(run_task(c.store.path, task['id']), 0)
                first = c.status(client, task['id'])
                c.intervene(client, task['id'], 'Do not use tools. Reply exactly CALLCLI_AGY_CONTINUED.')
                self.assertEqual(run_task(c.store.path, task['id']), 0)
                last = c.status(client, task['id'])
                self.assertEqual(first['acpx_record_id'], last['acpx_record_id'])
                events = c.events(client, task['id'])
                self.assertEqual(sum(e['kind']=='version' for e in events), 1)
                selected = [json.loads(e['text']) for e in events if e['kind']=='model']
                self.assertEqual(len(selected), 2)
                self.assertTrue(all(s['model']==model for s in selected))
                answers = '\n'.join(e['text'] for e in events if e['kind']=='answer')
                self.assertIn('CALLCLI_AGY_READY', answers)
                self.assertIn('CALLCLI_AGY_CONTINUED', answers)
                result = control(last, 'show')
                self.assertEqual(result.returncode, 0)
                state = json.loads(result.stdout)['acpx']
                self.assertEqual(state['current_model_id'], model)
                if effort:
                    self.assertEqual(next(o['currentValue'] for o in state['config_options'] if o['id']=='thought_level'), effort)
            finally:
                self.assertEqual(c.close(client, task['id'])['state'], 'closed')
