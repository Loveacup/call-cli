"""Resource recovery through public collaboration and host entry points only."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from collab import Collaboration
from runner import run_task


class PreparationRecoveryTests(unittest.TestCase):
    def test_required_staging_failure_unloads_without_prompt_and_frees_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'agy'
            executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            skill = root/'selected-skill'
            skill.mkdir()
            (skill/'SKILL.md').write_text('Synthetic required skill')
            with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], AGY_NATIVE_FIXTURE=directory), \
                    patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), \
                    patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                    patch('skills.target_request', side_effect=RuntimeError('synthetic staging failure')), \
                    contextlib.redirect_stdout(io.StringIO()):
                c = Collaboration(root/'tasks.db', launch=lambda task: None)
                client = c.connect('omp')['client_id']
                task = c.start(client, agent='agy', cwd=directory, prompt='must not submit', write=True,
                               skills=[dict(mode='required', path=str(skill), location='origin')])
                self.assertEqual(run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1), 1)
                result = c.status(client, task['id'])
                self.assertEqual(result['state'], 'unknown')
                self.assertEqual(result.get('resource_state'), 'released')
                self.assertEqual(c.events(client, task['id'])[-1]['recovery']['reservation']['state'], 'released')
                self.assertIn('staging failed', result['settlement_note'])
                requests = [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
                self.assertFalse(any(r.get('method') == 'session/prompt' for r in requests))
                self.assertTrue(result['native_cleanup']['session_unloaded'])
                successor = c.start(client, agent='agy', cwd=directory, prompt='new authorized work', write=True)
                self.assertEqual(successor['state'], 'queued')
                self.assertFalse(c.store.event(task['id'], 'late', 'answer', 'late', turn=1))
                self.assertFalse(c.store.finish_turn(task['id'], 1, 'completed', 0, 'end_turn'))
                self.assertEqual(c.status(client, successor['id'])['state'], 'queued')


class ExplicitReleaseTests(unittest.TestCase):
    def test_native_business_unknown_can_be_abandoned_by_current_real_client(self):
        from omp_bridge import HostBridge
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'agy'
            executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            c = Collaboration(root/'tasks.db', launch=lambda task: None)
            original = c.connect('pi')['client_id']
            task = c.start(original, agent='agy', cwd=directory, prompt='real synthetic business', write=True)
            host = dict(session_id='current-host', agent_id='main')
            bridge = HostBridge(c.store.path, launch=lambda task: None)
            capability = bridge.bind(host)
            def release(turn=1, abandon=True):
                return bridge.handle(host, capability, dict(action='release_only', task_id=task['id'], expected_turn=turn, abandon=abandon))
            with self.assertRaises(ValueError):
                release()  # queued is still a real writer reservation
            with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'],
                            AGY_NATIVE_FIXTURE=directory, AGY_NATIVE_FATAL='foreground'), \
                    patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), \
                    patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1), 1)
            before = c.status(original, task['id'])
            self.assertTrue(before['native_prompt_submitted'])
            with self.assertRaises(ValueError):
                c.start(original, agent='agy', cwd=directory, prompt='blocked', write=True)
            with self.assertRaises(ValueError):
                release(2)
            self.assertEqual(release(abandon=False)['task']['close_receipt']['state'], 'unknown')
            result = release()['task']
            repeated = release()['task']
            self.assertEqual(result['state'], 'unknown')
            self.assertEqual(result['resource_state'], 'released')
            self.assertEqual(result['close_receipt'], repeated['close_receipt'])
            self.assertIn('may still be active', result['close_receipt']['risk'])
            self.assertEqual(c.status(original, task['id'])['origin'], before['origin'])
            self.assertEqual(c.status(original, task['id'])['controller'], before['controller'])
            successor = c.start(original, agent='agy', cwd=directory, prompt='authorized successor', write=True)
            self.assertFalse(c.store.event(task['id'], 'late', 'answer', 'late', turn=1))
            self.assertFalse(c.store.finish_turn(task['id'], 1, 'completed', 0, 'end_turn'))
            self.assertEqual(c.store.finish_launch(task['id'], 1, task['launch_id'], process_id='late')['process_id'], before['process_id'])
            with self.assertRaises(PermissionError):
                bridge.handle(host, capability, dict(action='intervene', task_id=task['id'], prompt='no takeover', command_id='x'))
            self.assertEqual(c.status(original, successor['id'])['state'], 'queued')


    def test_strict_failure_requires_explicit_abandon_and_cli_releases_idempotently(self):
        import collab
        from fixtures.close_backend import CloseBackend
        with tempfile.TemporaryDirectory() as directory:
            c = Collaboration(Path(directory)/'tasks.db', launch=lambda task: None)
            old = c.connect('pi')['client_id']
            current = c.connect('codex')['client_id']
            task = c.start(old, agent='omp', cwd=directory, prompt='synthetic', write=True)
            c.store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='exact-record')
            backend = CloseBackend(verified=False)
            with patch('release.NativeBackend', return_value=backend):
                held = c.release_only(current, task['id'], expected_turn=1)
                self.assertEqual(held['close_receipt']['state'], 'unknown')
                with self.assertRaises(ValueError):
                    c.start(current, cwd=directory, prompt='blocked', write=True)
                outputs = []
                for _ in range(2):
                    output = io.StringIO()
                    with patch.object(collab, 'Collaboration', return_value=c), contextlib.redirect_stdout(output):
                        self.assertEqual(collab.main(['--db', str(c.store.path), '--client', current,
                            'release_only', '--task', task['id'], '--expected-turn', '1', '--abandon']), 0)
                    outputs.append(json.loads(output.getvalue()))
                self.assertEqual(outputs[0]['close_receipt'], outputs[1]['close_receipt'])
                self.assertEqual(outputs[0]['close_receipt']['operation'], 'explicit-abandonment')
                self.assertEqual(outputs[0]['state'], 'unknown')
                self.assertEqual((backend.closes, backend.probes), (1, 1))
                c.start(current, cwd=directory, prompt='authorized new writer', write=True)


if __name__ == '__main__':
    unittest.main()
