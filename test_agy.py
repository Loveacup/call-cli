import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from collab import Collaboration
from runner import control, shared_prompt_transport


class AgyTests(unittest.TestCase):
    def test_agy_model_survives_continuation_and_transport(self):
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            client = c.connect('codex')['client_id']
            t = c.start(client, agent='agy', cwd=root, prompt='inspect', model='advertised-family', effort='low')
            c.store.update(t['id'], state='completed')
            t = c.intervene(client, t['id'], 'continue')
            self.assertEqual(t['model'], 'advertised-family')
            self.assertEqual(t['effort'], 'low')
            t.update(acpx_record_id='11111111-1111-4111-8111-111111111111', execution_version='0.22.0')
            _, payload, _ = shared_prompt_transport(t)
            self.assertEqual(json.loads(payload)['options']['model'], t['model'])
            self.assertEqual(json.loads(payload)['options']['effort'], 'low')
            with patch('runner.subprocess.run') as run:
                control(t, 'new')
                args = run.call_args.args[0]
                self.assertIn('agy acp', args)
                self.assertNotIn('agy', args)
                self.assertEqual(args[args.index('--model')+1], t['model'])

    def test_selection_controls_and_readback_fail_closed(self):
        from types import SimpleNamespace
        import runner
        task = {'model':'live-family', 'effort':'low', 'session':'owned'}
        def response(t, action, **kw):
            if action == 'show':
                return SimpleNamespace(returncode=0, stdout=json.dumps({'name':'owned','acpx':{'current_model_id':'live-family','config_options':[{'id':'thought_level','currentValue':'low'}]}}).encode())
            return SimpleNamespace(returncode=0)
        with patch('runner.control', side_effect=response) as ctl:
            self.assertEqual(runner.apply_selection(task), {'model':'live-family','effort':'low'})
            self.assertEqual([c.args[1] for c in ctl.call_args_list], ['model','effort','show'])
        with patch('runner.control', return_value=SimpleNamespace(returncode=1)) as ctl:
            with self.assertRaises(RuntimeError): runner.apply_selection(task)
            self.assertEqual(ctl.call_count, 1)
        self.assertEqual(runner.apply_selection({'session':'legacy'}), {})

    def test_agy_requires_initiator_model_choice(self):
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            client = c.connect('codex')['client_id']
            with self.assertRaisesRegex(ValueError, 'model'):
                c.start(client, agent='agy', cwd=root, prompt='inspect')
