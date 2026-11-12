"""Prepared-target revision tracking and genuinely read-only receipt access."""
from contextlib import closing
import concurrent.futures
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from release import Release
from test_release_workflow import fixture


def projection(path):
    with closing(sqlite3.connect(path)) as db:
        return list(db.iterdump())


class InitializationTests(unittest.TestCase):
    def test_constructor_and_unprepared_receipt_are_read_only_under_reserved_writer(self):
        with tempfile.TemporaryDirectory() as td:
            release, _, backend = fixture(Path(td))
            before = projection(release.store.path)
            original = sqlite3.connect
            opened = []
            def readonly(database, *args, **kwargs):
                opened.append(str(database))
                self.assertIn('mode=ro', str(database))
                db = original(database, *args, **kwargs)
                db.execute('PRAGMA query_only=ON')
                return db
            with closing(original(release.store.path)) as writer:
                writer.execute('BEGIN IMMEDIATE')
                with patch('release.sqlite3.connect', side_effect=readonly):
                    recovered = Release(release.home, release.policy, backend)
                    self.assertEqual(recovered.receipt(), {'state':'not_prepared'})
                writer.rollback()
            self.assertTrue(opened)
            self.assertEqual(projection(release.store.path), before)
            with closing(original(release.store.path)) as db:
                self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'release_%' OR name='task_revisions'").fetchall(), [])

    def test_only_prepared_target_counts_revisions_and_unrelated_lifecycle_keeps_auxiliary_rows(self):
        with tempfile.TemporaryDirectory() as td:
            release, owner, _ = fixture(Path(td))
            store = release.store
            other = store.create(owner, 'local', 'omp', td+'/other', 'OTHER', False)
            before_other = store.get(other['id'])
            release.prepare()
            with store.connect() as db:
                before_aux = {name: db.execute('SELECT * FROM '+name).fetchall() for name in
                              ('task_revisions','release_confirmations','release_fences')}
                self.assertEqual([row[0] for row in before_aux['task_revisions']], [release.policy['task']])
            made = store.create(owner, 'local', 'omp', td+'/temporary', 'TEMP', False)
            store.update(other['id'],state='running')
            store.update(other['id'],state=before_other['state'])
            with store.connect() as db:
                db.execute('DELETE FROM tasks WHERE id=?',(made['id'],))
                after_aux = {name: db.execute('SELECT * FROM '+name).fetchall() for name in before_aux}
            self.assertEqual(after_aux,before_aux)
            self.assertEqual(store.get(other['id']),before_other)
            before = projection(store.path)
            with patch.object(Release,'install',side_effect=AssertionError('receipt attempted DDL')):
                self.assertIsNone(Release(release.home,release.policy,release.backend).receipt())
            self.assertEqual(projection(store.path),before)

    def test_concurrent_prepares_keep_revision_and_reject_update_and_delete_recreate_aba(self):
        for aba in ('update','recreate'):
            with self.subTest(aba=aba), tempfile.TemporaryDirectory() as td:
                release,owner,backend=fixture(Path(td))
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    prepared=list(pool.map(lambda _:Release(release.home,release.policy,backend).prepare(),range(2)))
                task=release.store.get(release.policy['task'])
                with release.store.connect() as db:
                    if aba=='update':
                        db.execute('UPDATE tasks SET data=? WHERE id=?',(json.dumps(dict(task,state='running')),task['id']))
                        db.execute('UPDATE tasks SET data=? WHERE id=?',(json.dumps(task),task['id']))
                    else:
                        db.execute('DELETE FROM tasks WHERE id=?',(task['id'],))
                        db.execute('INSERT INTO tasks VALUES(?,?,?)',(task['id'],owner.key(),json.dumps(task)))
                # A subsequent prepare may authorize the new snapshot, but cannot
                # reset enrollment and accidentally revive either old nonce.
                release.prepare()
                for confirmation in prepared:
                    with self.assertRaises(ValueError):release.release(confirmation['nonce'])
                self.assertEqual(backend.calls,0)

    def test_failed_prepare_rolls_back_schema_and_target_enrollment(self):
        with tempfile.TemporaryDirectory() as td:
            release, _, backend=fixture(Path(td))
            before=projection(release.store.path)
            release.policy=dict(release.policy,turn=5)
            with self.assertRaises(ValueError):release.prepare()
            self.assertEqual(projection(release.store.path),before)
            self.assertEqual(release.receipt(),{'state':'not_prepared'})
            self.assertEqual(backend.calls,0)

    def test_prepare_enrollment_and_snapshot_share_the_writer_transaction(self):
        with tempfile.TemporaryDirectory() as td:
            release, _, backend=fixture(Path(td))
            captured=threading.Event(); attempted=threading.Event(); finished=threading.Event()
            snapshot=release._snapshot
            def locked_snapshot(db):
                self.assertTrue(db.in_transaction)
                row=db.execute('SELECT revision FROM task_revisions WHERE task=?',(release.policy['task'],)).fetchone()
                self.assertIsNotNone(row)
                captured.set()
                self.assertTrue(attempted.wait(2))
                self.assertFalse(finished.wait(.05))
                return snapshot(db)
            def writer():
                self.assertTrue(captured.wait(2));attempted.set()
                release.store.update(release.policy['task'],state='running')
                release.store.update(release.policy['task'],state='completed')
                finished.set()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(writer)
                with patch.object(release,'_snapshot',side_effect=locked_snapshot):
                    prepared=release.prepare()
                future.result(timeout=5)
            with self.assertRaises(ValueError):release.release(prepared['nonce'])
            self.assertEqual(backend.calls,0)


if __name__=='__main__':unittest.main()
