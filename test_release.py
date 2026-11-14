"""Release safety through durable Store and authenticated operator seams."""
import tempfile
import unittest
from pathlib import Path

from bridge import Origin, Store


class ClosedFenceTests(unittest.TestCase):
    def test_closed_task_rejects_late_events_launch_and_reopening(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / 'tasks.sqlite3')
            owner = Origin('default', 'ended-owner', 'ended-owner')
            task = store.create(owner, 'local', 'omp', td, 'fixture', True)
            tid = task['id']
            store.update(tid, state='completed', launch_id='old-launch')
            store.begin_close(tid, owner)
            store.finish_close(tid, 1, True)
            before = store.get(tid, owner)
            self.assertFalse(store.event(tid, 'late', 'question', 'Q1 Reopen?', turn=1))
            store.finish_launch(tid, 1, 'old-launch', 'late-process')
            self.assertEqual(store.get(tid, owner), before)
            with self.assertRaises(ValueError):
                store.update(tid, state='running')
            self.assertEqual(store.get(tid, owner), before)

    def test_native_backend_transport_diagnostics_keep_operation_stage_without_private_text(self):
        from release import NativeBackend, BackendFailure
        from unittest.mock import patch
        from types import SimpleNamespace
        from contextlib import nullcontext
        import subprocess
        cases=[(OSError('PRIVATE'), 'spawn'), (subprocess.TimeoutExpired('PRIVATE',45), 'timeout'),
            (SimpleNamespace(returncode=2,stdout=b'bad',stderr=b'PRIVATE'), 'parse'),
            (SimpleNamespace(returncode=1,stdout=b'{"stage":"load","substep":"load_runtime","reason_code":"backend_unavailable","error_name":"Error"}',stderr=b'PRIVATE'), 'load')]
        for outcome, stage in cases:
            kwargs={'side_effect':outcome} if isinstance(outcome,Exception) else {'return_value':outcome}
            with self.subTest(stage=stage), patch('subprocess.run',**kwargs), \
                 patch('shutil.which', side_effect=lambda name, **kw: '/fixture/bin/' + name), \
                 patch('updates.device_lock', return_value=nullcontext()):
                try: NativeBackend().resolve({})
                except BackendFailure as exc:
                    self.assertEqual(exc.stage,stage)
                    self.assertEqual(exc.diagnostic['operation'],'resolve')
                    self.assertNotIn('PRIVATE',str(exc.diagnostic))
                else:self.fail('Backend failure was not classified')


    def test_durable_release_fence_blocks_preloaded_sql_writer(self):
        import json
        import sqlite3
        from release import Release
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / 'tasks.sqlite3')
            owner = Origin('default', 'ended-owner', 'ended-owner')
            task = store.create(owner, 'local', 'omp', td, 'fixture', True)
            old = sqlite3.connect(store.path)
            try:
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    Release.install(db)
                    db.execute('INSERT INTO release_fences(task, epoch, data) VALUES (?,1,?)',
                               (task['id'], json.dumps(task)))
                changed = dict(task, state='running')
                with self.assertRaises(sqlite3.IntegrityError):
                    old.execute('UPDATE tasks SET data=? WHERE id=?', (json.dumps(changed), task['id']))
                old.rollback()
                with self.assertRaises(sqlite3.IntegrityError):
                    old.execute('INSERT INTO events VALUES (?,?,?)', (task['id'], 'late', '{}'))
                old.rollback()
                # Exact SQL used by old Store._save and runner's direct claim.
                for sql,args in (
                    ('UPDATE tasks SET data=? WHERE id=?', (json.dumps(dict(task,state='running',runner_pid=123)),task['id'])),
                    ('UPDATE tasks SET owner=? WHERE id=?', ('forged',task['id'])),
                    ('DELETE FROM tasks WHERE id=?', (task['id'],)),
                    ('INSERT OR REPLACE INTO tasks VALUES(?,?,?)', (task['id'],owner.key(),json.dumps(changed)))):
                    with self.assertRaises(sqlite3.IntegrityError):
                        old.execute(sql,args)
                    old.rollback()
                unaffected = store.create(owner,'local','omp',td+'/other','fixture',False)
                store.update(unaffected['id'],state='running')
                self.assertEqual(store.get(unaffected['id'])['state'],'running')
                self.assertEqual(store.get(task['id'], owner), task)
            finally:
                old.close()


if __name__ == '__main__':
    unittest.main()
