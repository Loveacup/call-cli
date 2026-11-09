"""Skill transfer behavior at public task, transport and receipt boundaries."""
import json
import tempfile
import unittest
from pathlib import Path
from collab import Collaboration


class SkillTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.skill = self.root / 'source'
        (self.skill / 'references').mkdir(parents=True)
        (self.skill / 'SKILL.md').write_text('---\nname: sentinel\ndescription: Read the reference\n---\nRead references/value.txt.\n')
        (self.skill / 'references/value.txt').write_text('ORIGINAL_SENTINEL')
        self.c = Collaboration(self.root / 'tasks.sqlite3', launch=lambda task: None)
        self.client = self.c.connect('hermes')['client_id']

    def start(self, **kw):
        return self.c.start(self.client, prompt='Read the skill', cwd=str(self.root),
                            skills=[dict(path=str(self.skill), mode='required')], **kw)

    def test_source_edits_do_not_change_continuation_or_other_session(self):
        task = self.start()
        bundle = task['skills'][0]
        self.assertEqual((Path(bundle['snapshot']) / 'references/value.txt').read_text(), 'ORIGINAL_SENTINEL')
        (self.skill / 'references/value.txt').write_text('NEW_SENTINEL')
        self.c.store.update(task['id'], state='completed')
        resumed = self.c.intervene(self.client, task['id'], 'Continue', command_id='next')
        self.assertEqual(resumed['skills'], task['skills'])
        other = self.start()
        self.assertNotEqual(other['skills'][0]['snapshot'], bundle['snapshot'])
        self.assertNotEqual(other['skills'][0]['sha256'], bundle['sha256'])
        self.assertEqual((Path(bundle['snapshot']) / 'references/value.txt').read_text(), 'ORIGINAL_SENTINEL')
        stranger = self.c.connect('omp')['client_id']
        with self.assertRaises(PermissionError):
            self.c.status(stranger, task['id'])


    def test_target_bundle_contains_resources_and_repairs_modified_copy(self):
        from skills import stage, guidance
        task = self.start()
        task = stage(self.c.store, task, lambda *a, **kw: None)
        bundle = task['skills'][0]
        target = Path(bundle['target_path'])
        self.addCleanup(__import__('shutil').rmtree, target.parents[1], True)
        self.assertEqual((target / 'references/value.txt').read_text(), 'ORIGINAL_SENTINEL')
        (target / 'references/value.txt').write_text('TAMPERED')
        (self.skill / 'references/value.txt').write_text('LATER_SOURCE')
        task = stage(self.c.store, task, lambda *a, **kw: None)
        self.assertEqual((target / 'references/value.txt').read_text(), 'ORIGINAL_SENTINEL')
        self.assertIn(str(target / 'SKILL.md'), guidance(task))
        self.assertEqual(task['skills'][0]['sha256'], bundle['sha256'])

    def test_required_missing_report_does_not_downgrade_business_result(self):
        from events import EventDecoder
        task = self.start()
        self.c.store.update(task['id'], state='running')
        decoder = EventDecoder()
        for kind, text in decoder.line('CALLCLI_SKILL skill-1 adopted Read reference ORIGINAL_SENTINEL'):
            self.c.store.event(task['id'], 'report', kind, text, turn=1)
        self.c.store.finish_turn(task['id'], 1, 'completed', 0, 'end_turn')
        result = self.c.status(self.client, task['id'])
        self.assertEqual(result['skill_reports']['1']['skill-1']['status'], 'adopted')
        self.assertEqual(result['skill_report_missing'], [])
        self.assertEqual(result['state'], 'completed')
        self.c.intervene(self.client, task['id'], 'Continue', command_id='next')
        self.c.store.update(task['id'], state='running')
        self.c.store.finish_turn(task['id'], 2, 'completed', 0, 'end_turn')
        result = self.c.status(self.client, task['id'])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['skill_report_missing'], ['skill-1'])
        self.assertEqual(result['skill_verification'], 'unverified')

    def test_target_existing_skill_is_pinned_even_when_source_changes(self):
        from skills import stage, cleanup
        task = self.c.start(self.client, prompt='Read', cwd=str(self.root), skills=[
            dict(path=str(self.skill), mode='required', location='target')])
        self.addCleanup(lambda: cleanup(self.c.store, self.c.store.get(task['id'])))
        first = stage(self.c.store, task, lambda *a, **kw: None)
        (self.skill / 'references/value.txt').write_text('CHANGED_SOURCE')
        second = stage(self.c.store, first, lambda *a, **kw: None)
        self.assertEqual((Path(second['skills'][0]['target_path']) / 'references/value.txt').read_text(), 'ORIGINAL_SENTINEL')
        self.assertEqual(first['skills'][0]['sha256'], second['skills'][0]['sha256'])
        (Path(second['skills'][0]['target_path']) / 'references/value.txt').write_text('CORRUPT')
        with self.assertRaisesRegex(RuntimeError, 'no prompt submitted'):
            stage(self.c.store, second, lambda *a, **kw: None)

    def test_private_files_and_symlinks_are_not_transferred(self):
        (self.skill / '.env').write_text('PRIVATE_SYNTHETIC')
        (self.skill / 'secret.pem').write_text('PRIVATE_SYNTHETIC')
        (self.skill / 'linked.txt').symlink_to(self.root / 'outside')
        task = self.start()
        entries = {f['path'] for f in task['skills'][0]['files']}
        self.assertEqual(entries, {'SKILL.md', 'references/value.txt'})
        self.assertEqual(task['skills'][0]['skipped'], ['.env', 'linked.txt', 'secret.pem'])

    def test_invalid_selections_leave_no_task_or_worker(self):
        for spec in [dict(path=str(self.skill), mode='mandatory'), dict(path='relative', mode='required'),
                     dict(path=str(self.skill), mode='required', unexpected=True)]:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.c.start(self.client, prompt='Read', cwd=str(self.root), skills=[spec])
        self.assertEqual(self.c.list(self.client), [])

    def test_optional_failure_preserves_usable_skills_and_business_prompt(self):
        from skills import stage
        task = self.c.start(self.client, prompt='Business result', cwd=str(self.root), skills=[
            dict(path=str(self.root / 'absent'), mode='optional', location='target'),
            dict(path=str(self.skill), mode='reference')])
        staged = stage(self.c.store, task, lambda *a, **kw: None)
        self.addCleanup(__import__('skills').cleanup, self.c.store, staged)
        self.assertFalse(staged['skills'][0]['available'])
        self.assertTrue(staged['skills'][1]['available'])
        self.assertEqual(staged['prompt'], 'Business result')

    def test_hermes_and_json_cli_accept_selected_skills(self):
        from test_plugin import load_plugin, Ctx
        from unittest.mock import patch
        from contextlib import redirect_stdout
        from io import StringIO
        import collab
        plugin = load_plugin()
        coordinator = plugin.Coordinator(Ctx(), self.root / 'hermes', lambda k, default='':
            {'HERMES_SESSION_ID': 'test-host', 'HERMES_SESSION_KEY': 'test-key'}.get(k, default))
        result = coordinator.handle(dict(action='start', agent='omp', cwd=str(self.root), prompt='Read',
            skills=[dict(path=str(self.skill), mode='optional', source={'revision': 'fixed-fixture'})]))
        self.assertEqual(result['task']['skills'][0]['source']['revision'], 'fixed-fixture')
        out = StringIO()
        with patch.object(collab, 'Collaboration', return_value=self.c), redirect_stdout(out):
            rc = collab.main(['--client', self.client, 'start', '--cwd', str(self.root), '--prompt', 'Read',
                              '--skill', 'reference='+str(self.skill)])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())['skills'][0]['mode'], 'reference')

    def test_optional_transport_timeout_does_not_block_business_result(self):
        from skills import stage
        from unittest.mock import patch
        import subprocess
        task = self.c.start(self.client, prompt='Keep business work', cwd=str(self.root), skills=[
            dict(path=str(self.skill), mode='optional')])
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('ssh', 60)):
            result = stage(self.c.store, task, lambda *a, **kw: None)
        self.assertFalse(result['skills'][0]['available'])
        self.assertEqual(result['prompt'], 'Keep business work')

    def test_concurrent_tasks_do_not_share_writable_skill_copies(self):
        from concurrent.futures import ThreadPoolExecutor
        from skills import stage, cleanup
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = list(pool.map(lambda _: self.start(), range(2)))
            staged = list(pool.map(lambda task: stage(self.c.store, task, lambda *a, **kw: None), tasks))
        for task in staged:
            self.addCleanup(cleanup, self.c.store, task)
        left, right = [Path(task['skills'][0]['target_path']) for task in staged]
        (left / 'references/value.txt').write_text('LEFT_ONLY')
        self.assertEqual((right / 'references/value.txt').read_text(), 'ORIGINAL_SENTINEL')
        self.assertNotEqual(staged[0]['session'], staged[1]['session'])

if __name__ == '__main__':
    unittest.main()
