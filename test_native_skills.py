"""Opt-in real ACPX transport with agent-native skill reads, not a real model."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from collab import Collaboration
from runner import run_task


@unittest.skipUnless(os.environ.get('RUN_NATIVE_ACPX_TESTS') == '1', 'opt-in real ACPX skill reads')
class NativeSkillTests(unittest.TestCase):
    def test_three_modes_resources_and_pinned_continuation(self):
        with tempfile.TemporaryDirectory(dir=Path.home()/'.hermes/cache/scratch') as tmp:
            root = Path(tmp)
            skill = root/'source'; (skill/'references').mkdir(parents=True)
            evidence_root = Path.home()/'.hermes/cache/scratch/skill-transfer-native-failure'
            (skill/'SKILL.md').write_text('---\nname: native-sentinel\ndescription: Fixture\n---\nRead references/value.txt\n')
            (skill/'references/value.txt').write_text('FIXED_NATIVE_RESOURCE')
            home = root/'home'; home.mkdir()
            fixture = Path(__file__).with_name('fixtures')/'native_shared_agent.py'
            trace = root/'wire.jsonl'
            agent = shlex.join([sys.executable, '-B', str(fixture), str(trace), 'skills'])
            def control(task, action, timeout=20):
                args = ['acpx', '--agent', agent, '--cwd', task['cwd'], '--format', 'json']
                if action == 'new': args += ['sessions', 'new', '--name', task['session']]
                elif action in {'close', 'show'}: args += ['sessions', action, task['session']]
                else: args += [action, '-s', task['session']]
                return subprocess.run(args, capture_output=True, timeout=timeout)
            c = Collaboration(root/'tasks.sqlite3', launch=lambda task: None, control=control)
            client = c.connect('hermes')['client_id']
            task = c.start(client, prompt='Read provided resources', agent='omp', cwd=str(root), timeout=45,
                skills=[dict(path=str(skill), mode=mode) for mode in ['required', 'optional', 'reference']])
            with patch.dict(os.environ, {'HOME': str(home)}), patch('acpx_backend.AcpxBackend.control', side_effect=control), patch('updates.ensure_current', return_value={'decision': 'existing-integration-install'}):
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc = __import__('test_dispatch').run_dispatched(c.store.path, task['id'])
                    if rc:
                        evidence = evidence_root
                        evidence.mkdir(exist_ok=True)
                        __import__('shutil').copytree(root, evidence/task['id'])
                        print('NATIVE_SKILL_FAILURE '+str(evidence/task['id']))
                    self.assertEqual(rc, 0, c.events(client, task['id']))
                    first = c.status(client, task['id'])
                    self.assertEqual(first['state'], 'completed')
                    (skill/'references/value.txt').write_text('MUST_NOT_REACH_CONTINUATION')
                    c.intervene(client, task['id'], 'Read the same resources again', command_id='continuation')
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(__import__('test_dispatch').run_dispatched(c.store.path, task['id']), 0)
                    final = c.status(client, task['id'])
                    self.assertEqual(final['acpx_record_id'], first['acpx_record_id'])
                    for turn in ['1', '2']:
                        self.assertEqual({r['status'] for r in final['skill_reports'][turn].values()}, {'adopted'})
                    values = [e['text'] for e in c.events(client, task['id']) if e['kind'] == 'answer']
                    self.assertEqual(values, ['RESOURCE_VALUES ["FIXED_NATIVE_RESOURCE", "FIXED_NATIVE_RESOURCE", "FIXED_NATIVE_RESOURCE"]'] * 2)
                    self.assertEqual(final['skill_report_missing'], [])
                    print('NATIVE_SKILL_EVIDENCE '+json.dumps(dict(task=task['id'], turns=2, modes=3, agent_native_file_reads=12, fixed=True)))
                finally:
                    self.assertEqual(c.close(client, task['id'])['state'], 'closed')
                    self.assertTrue(all(not Path(s['snapshot']).exists() for s in task['skills']))


if __name__ == '__main__':
    unittest.main()
