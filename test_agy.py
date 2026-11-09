import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from collab import Collaboration
from runner import control
from backends import backend_for


class AgyTests(unittest.TestCase):
    def test_approval_is_persistent_and_explicit_reads_overrides_write(self):
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            client = c.connect('codex')['client_id']
            task = c.start(client, agent='agy', cwd=root, prompt='inspect', model='advertised',
                           write=True, agy_approval='approve-reads')
            c.store.update(task['id'], state='completed')
            task = c.intervene(client, task['id'], 'continue')
            task.update(acpx_record_id='11111111-1111-4111-8111-111111111111', execution_version='0.22.0')
            options = json.loads(backend_for(task['agent']).transport(task)[1])['options']
            self.assertEqual(options['permissionMode'], 'approve-reads')
            self.assertEqual(c.status(client, task['id'])['agy_approval'], 'approve-reads')

    def test_new_agy_approval_and_legacy_other_agents_remain_distinct(self):
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            client = c.connect('codex')['client_id']
            task = c.start(client, agent='agy', cwd=root, prompt='inspect', model='advertised')
            self.assertEqual(task['agy_approval'], 'approve-all')
            for agent in ('omp', 'codex', 'pi'):
                with self.assertRaises(ValueError):
                    c.start(client, agent=agent, cwd=root, prompt='inspect', agy_approval='approve-all')
                other = c.start(client, agent=agent, cwd=root, prompt='inspect')
                self.assertNotIn('agy_approval', other)
            for agent in ('agy', 'omp', 'codex', 'pi'):
                for write in (False, True):
                    legacy = dict(task, agent=agent, write=write)
                    legacy.pop('agy_approval')
                    legacy.update(acpx_record_id='11111111-1111-4111-8111-111111111111', execution_version='0.22.0')
                    options = json.loads(backend_for(legacy['agent']).transport(legacy)[1])['options']
                    self.assertEqual(options['permissionMode'], 'approve-all' if write else 'approve-reads')
            with self.assertRaises(ValueError):
                c.start(client, agent='agy', cwd=root, prompt='inspect', model='advertised', agy_approval='typo')

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
            _, payload, _ = backend_for(t['agent']).transport(t)
            self.assertEqual(json.loads(payload)['options']['model'], t['model'])
            self.assertEqual(json.loads(payload)['options']['effort'], 'low')
            with patch('acpx_backend.subprocess.run') as run:
                control(t, 'new')
                args = run.call_args.args[0]
                self.assertIn('agy acp', args)
                self.assertNotIn('agy', args)
                self.assertEqual(args[args.index('--model')+1], t['model'])

    def test_selection_controls_and_readback_fail_closed(self):
        from types import SimpleNamespace
        from acpx_backend import AcpxBackend
        backend = AcpxBackend()
        task = {'model':'live-family', 'effort':'low', 'session':'owned'}
        def response(t, action, **kw):
            if action == 'show':
                return SimpleNamespace(returncode=0, stdout=json.dumps({'name':'owned','acpx':{'current_model_id':'live-family','config_options':[{'id':'thought_level','currentValue':'low'}]}}).encode())
            return SimpleNamespace(returncode=0)
        with patch.object(backend, 'control', side_effect=response) as ctl:
            self.assertEqual(backend.apply_selection(task), {'model':'live-family','effort':'low'})
            self.assertEqual([c.args[1] for c in ctl.call_args_list], ['model','effort','show'])
        with patch.object(backend, 'control', return_value=SimpleNamespace(returncode=1)) as ctl:
            with self.assertRaises(RuntimeError): backend.apply_selection(task)
            self.assertEqual(ctl.call_count, 1)
        self.assertEqual(backend.apply_selection({'session':'legacy'}), {})

    def test_approval_does_not_reserve_read_task_but_declared_writers_conflict_across_clis(self):
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            a = c.connect('omp')['client_id']
            b = c.connect('pi')['client_id']
            writer = c.start(a, agent='omp', cwd=root, prompt='edit', write=True)
            reader = c.start(b, agent='agy', cwd=root, prompt='inspect', model='advertised')
            self.assertEqual((reader['agy_approval'], reader['write']), ('approve-all', False))
            with self.assertRaisesRegex(ValueError, writer['id']):
                c.start(b, agent='agy', cwd=root, prompt='edit', model='advertised', write=True)

    def test_agy_requires_initiator_model_choice(self):
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            client = c.connect('codex')['client_id']
            with self.assertRaisesRegex(ValueError, 'model'):
                c.start(client, agent='agy', cwd=root, prompt='inspect')
