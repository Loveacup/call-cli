"""Isolated RED/GREEN and negative-control tests for Slice C1 (successor writer vs old task close)."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_plugin import Ctx, load_plugin
from fixtures.close_backend import CloseBackend
from test_release_workflow import Backend as WorkflowBackend, fixture as release_fixture


class _C1Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'state.db'
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript('''
                CREATE TABLE schema_version(version INTEGER NOT NULL);
                INSERT INTO schema_version VALUES (32);
                CREATE TABLE sessions(
                    id TEXT PRIMARY KEY,
                    parent_session_id TEXT,
                    end_reason TEXT,
                    ended_at REAL,
                    model_config TEXT,
                    source TEXT,
                    profile_name TEXT
                );
                INSERT INTO sessions VALUES ('owner-1', NULL, NULL, NULL, NULL, 'telegram', NULL);
            ''')
        self.mod = load_plugin()
        self.ctx = Ctx()
        self.env = {
            'HERMES_SESSION_ID': 'owner-1',
            'HERMES_SESSION_KEY': 'telegram:chat-1',
            'HERMES_SESSION_PLATFORM': 'telegram',
            'HERMES_SESSION_CHAT_ID': 'chat-1',
            'HERMES_SESSION_THREAD_ID': 'topic-1',
        }
        self.c = self.mod.Coordinator(
            self.ctx,
            self.root / 'plugin-data',
            lambda key, default='': self.env.get(key, default),
            hermes_home=self.root,
            hermes_root=self.root,
        )
        self.store = self.mod.Store(self.root / 'plugin-data/call-cli/tasks.sqlite3')
        self.owner = self.mod.capture_origin(lambda k, d='': self.env.get(k, d), 'default')

    def start_task(self, *, cwd=None, **kwargs):
        target = Path(cwd) if cwd is not None else self.root
        target.mkdir(parents=True, exist_ok=True)
        return self.c.handle(dict(
            action='start', agent='omp', cwd=str(target), prompt='synthetic', **kwargs
        ))['task']

    def act(self, action, task, **kwargs):
        return self.c.handle(dict(action=action, task_id=task['id'], **kwargs))


class C1SuccessorCloseRedTests(_C1Base):
    def test_c1_completed_and_cancelled_close_succeed_with_active_successor_writer(self):
        import callcli_plugin.release as rel

        # Sub-case A: settled completed T1 + active successor T2
        dir_a = self.root / 'case_completed'
        t1 = self.start_task(cwd=dir_a, write=True)
        self.store.update(t1['id'], state='completed', runner_pid=0, acpx_record_id='rec-t1')
        t2 = self.start_task(cwd=dir_a, write=True)
        self.assertEqual(t2['state'], 'queued')
        with patch.object(rel, 'NativeBackend', return_value=CloseBackend(verified=True)):
            res1 = self.act('close', t1, expected_turn=1)
        self.assertEqual((res1['task']['state'], res1['receipt']['state']), ('closed', 'released'))
        self.assertEqual(self.store.get(t2['id'])['state'], 'queued')

        # Sub-case B: settled cancelled T1c + active successor T2c
        dir_b = self.root / 'case_cancelled'
        t1c = self.start_task(cwd=dir_b, write=True)
        self.store.update(t1c['id'], state='cancelled', runner_pid=0, acpx_record_id='rec-t1c')
        t2c = self.start_task(cwd=dir_b, write=True)
        self.assertEqual(t2c['state'], 'queued')
        with patch.object(rel, 'NativeBackend', return_value=CloseBackend(verified=True)):
            res2 = self.act('close', t1c, expected_turn=1)
        self.assertEqual((res2['task']['state'], res2['receipt']['state']), ('closed', 'released'))
        self.assertEqual(self.store.get(t2c['id'])['state'], 'queued')

    def test_c1_failed_close_with_successor_writer_does_not_steal_lock_or_block_successor_continuation(self):
        import callcli_plugin.release as rel

        t1 = self.start_task(write=True)
        self.store.update(t1['id'], state='completed', runner_pid=0, acpx_record_id='rec-t1')
        t2 = self.start_task(write=True)
        self.assertEqual(t2['state'], 'queued')

        def fail_close(_policy):
            raise TimeoutError('synthetic close timeout')

        backend = CloseBackend(on_close=fail_close, verified=False)
        with patch.object(rel, 'NativeBackend', return_value=backend):
            hold = self.act('close', t1, expected_turn=1)
        self.assertEqual((hold['task']['state'], hold['task'].get('resource_state')), ('unknown', 'superseded'))
        self.assertEqual(self.store.get(t2['id'])['state'], 'queued')

        # Successor T2 completes Turn 1 and continues into Turn 2 without being blocked by T1 HOLD
        self.store.update(t2['id'], state='completed', runner_pid=0)
        r2 = self.act('reply', t2, prompt='turn 2', expected_turn=1, command_id='r1')['task']
        self.assertEqual((r2['turn'], r2['state']), (2, 'queued'))

        # After T2 settles, a third writer T3 can also start cleanly
        self.store.update(t2['id'], state='completed', runner_pid=0)
        t3 = self.start_task(write=True)
        self.assertEqual(t3['state'], 'queued')

        # Once backend reports no-session, reconciling T1 closes T1 cleanly and clears superseded marker
        backend.verified = True
        with patch.object(rel, 'NativeBackend', return_value=backend):
            rec = self.act('reconcile', t1, expected_turn=1)
        self.assertEqual((rec['task']['state'], rec['receipt']['state']), ('closed', 'released'))
        self.assertNotEqual(rec['task'].get('resource_state'), 'superseded')

    def test_c1_missing_record_native_unproven_begin_close_and_operator_release_with_successor_writer(self):
        # (a) close_task missing acpx_record_id with active successor
        dir_a = self.root / 'sub_missing'
        t1a = self.start_task(cwd=dir_a, write=True)
        self.store.update(t1a['id'], state='completed', runner_pid=0, acpx_record_id=None)
        t2a = self.start_task(cwd=dir_a, write=True)
        hold_a = self.act('close', t1a, expected_turn=1)
        self.assertEqual(
            (hold_a['task']['state'], hold_a['task'].get('resource_state'), hold_a['receipt']['reason_code']),
            ('unknown', 'superseded', 'exact_record_unavailable'),
        )
        self.store.update(t2a['id'], state='completed', runner_pid=0)
        self.assertEqual(
            self.act('reply', t2a, prompt='turn 2', expected_turn=1, command_id='r-a')['task']['turn'], 2
        )

        # (b) close_task native unproven cleanup with active successor
        dir_b = self.root / 'sub_native'
        t1b = self.start_task(cwd=dir_b, write=True)
        self.store.update(t1b['id'], agent='agy', agy_swarm='native', state='completed', runner_pid=0)
        t2b = self.start_task(cwd=dir_b, write=True)
        hold_b = self.act('close', t1b, expected_turn=1)
        self.assertEqual(
            (hold_b['task']['state'], hold_b['task'].get('resource_state'), hold_b['receipt']['reason_code']),
            ('unknown', 'superseded', 'native_workers_unproven'),
        )
        self.store.update(t2b['id'], state='completed', runner_pid=0)
        self.assertEqual(
            self.act('reply', t2b, prompt='turn 2', expected_turn=1, command_id='r-b')['task']['turn'], 2
        )

        # (c) Store.begin_close + finish_close(False) with active successor
        dir_c = self.root / 'sub_begin_close'
        t1c = self.start_task(cwd=dir_c, write=True)
        self.store.update(t1c['id'], state='completed', runner_pid=0)
        t2c = self.start_task(cwd=dir_c, write=True)
        closing_task = self.store.begin_close(t1c['id'], self.owner)
        self.assertEqual((closing_task['state'], closing_task.get('resource_state')), ('closing', 'superseded'))
        self.assertTrue(self.store.finish_close(t1c['id'], 1, False))
        t1c_after = self.store.get(t1c['id'])
        self.assertEqual((t1c_after['state'], t1c_after.get('resource_state')), ('unknown', 'superseded'))
        self.store.update(t2c['id'], state='completed', runner_pid=0)
        self.assertEqual(
            self.act('reply', t2c, prompt='turn 2', expected_turn=1, command_id='r-c')['task']['turn'], 2
        )

        # (d) Release.release(nonce) operator P2 workflow with active successor (both success and failed close)
        with tempfile.TemporaryDirectory() as td_ok:
            rel_ok, owner_ok, _ = release_fixture(Path(td_ok))
            t_succ = rel_ok.store.create(owner_ok, 'local', 'omp', td_ok, 'successor', True)
            nonce = rel_ok.prepare()['nonce']
            receipt = rel_ok.release(nonce)
            self.assertEqual(receipt['state'], 'released')
            self.assertEqual(rel_ok.store.get(t_succ['id'])['state'], 'queued')

        with tempfile.TemporaryDirectory() as td_fail:
            rel_fail, owner_fail, _ = release_fixture(Path(td_fail), WorkflowBackend(fail=True))
            t_succ2 = rel_fail.store.create(owner_fail, 'local', 'omp', td_fail, 'successor', True)
            nonce2 = rel_fail.prepare()['nonce']
            receipt2 = rel_fail.release(nonce2)
            self.assertEqual((receipt2['state'], receipt2['phase']), ('unknown', 'hold'))
            self.assertEqual(rel_fail.store.get(rel_fail.policy['task']).get('resource_state'), 'superseded')
            rel_fail.store.update(t_succ2['id'], state='completed', runner_pid=0)
            r_succ2 = rel_fail.store.reply(
                t_succ2['id'], owner_fail, '', 'turn 2', expected_turn=1, command_id='r-d'
            )
            self.assertEqual((r_succ2['turn'], r_succ2['state']), (2, 'queued'))


class C1WriterHoldNegativeControlsTests(_C1Base):
    def test_ctrl_solo_failed_close_retains_workspace_hold(self):
        import callcli_plugin.release as rel

        # (a) Solo backend close failure retains HOLD and blocks new writers
        dir_a = self.root / 'solo_timeout'
        t_a = self.start_task(cwd=dir_a, write=True)
        self.store.update(t_a['id'], state='completed', runner_pid=0, acpx_record_id='rec-solo')
        failing = CloseBackend(
            on_close=lambda _p: (_ for _ in ()).throw(TimeoutError('synthetic close timeout')),
            verified=False,
        )
        with patch.object(rel, 'NativeBackend', return_value=failing):
            hold_a = self.act('close', t_a, expected_turn=1)
        self.assertEqual(hold_a['task']['state'], 'unknown')
        self.assertNotEqual(hold_a['task'].get('resource_state'), 'superseded')
        with self.assertRaisesRegex(ValueError, 'Workspace is already owned by task '):
            self.start_task(cwd=dir_a, write=True)

        # (b) Solo missing acpx_record_id retains HOLD and blocks new writers
        dir_b = self.root / 'solo_missing'
        t_b = self.start_task(cwd=dir_b, write=True)
        self.store.update(t_b['id'], state='completed', runner_pid=0, acpx_record_id=None)
        hold_b = self.act('close', t_b, expected_turn=1)
        self.assertEqual(hold_b['task']['state'], 'unknown')
        self.assertNotEqual(hold_b['task'].get('resource_state'), 'superseded')
        with self.assertRaisesRegex(ValueError, 'Workspace is already owned by task '):
            self.start_task(cwd=dir_b, write=True)

        # (c) Solo native unproven cleanup retains HOLD and blocks new writers
        dir_c = self.root / 'solo_native'
        t_c = self.start_task(cwd=dir_c, write=True)
        self.store.update(t_c['id'], agent='agy', agy_swarm='native', state='completed', runner_pid=0)
        hold_c = self.act('close', t_c, expected_turn=1)
        self.assertEqual(hold_c['task']['state'], 'unknown')
        self.assertNotEqual(hold_c['task'].get('resource_state'), 'superseded')
        with self.assertRaisesRegex(ValueError, 'Workspace is already owned by task '):
            self.start_task(cwd=dir_c, write=True)

    def test_ctrl_active_and_exclusive_unknown_writers_enforce_mutex(self):
        for state in ('queued', 'running', 'waiting_reply', 'cancelling', 'unknown'):
            with self.subTest(state=state):
                sub = self.root / f'mutex_{state}'
                t_other = self.start_task(cwd=sub, write=True)
                self.store.update(t_other['id'], state='completed', runner_pid=0)
                t_active = self.start_task(cwd=sub, write=True)
                self.store.update(t_active['id'], state=state)

                with self.assertRaisesRegex(ValueError, 'Workspace is already owned by task '):
                    self.start_task(cwd=sub, write=True)
                with self.assertRaisesRegex(ValueError, 'Workspace is already owned by task '):
                    self.act('reply', t_other, prompt='blocked reply', expected_turn=1, command_id=f'r-{state}')
                with self.assertRaisesRegex(ValueError, 'Workspace is already owned by task '):
                    self.act('intervene', t_other, prompt='blocked intervene', expected_turn=1, command_id=f'i-{state}')


if __name__ == '__main__':
    unittest.main()
