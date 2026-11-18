"""Synthetic old-session cleanup regressions; no live backend or host state."""
from pathlib import Path
from contextlib import closing
import sqlite3
import os
import subprocess
import sys

from fixtures.close_backend import CloseBackend
from unittest.mock import patch

import test_native_ended_owner as owner_fixtures
import tempfile
import test_native_continuity as continuity_fixtures
import unittest

from test_release_workflow import fixture


class SuccessorFinishCloseTests(unittest.TestCase):
    def test_successful_finish_clears_superseded_without_touching_successor(self):
        with tempfile.TemporaryDirectory() as td:
            release, owner, _ = fixture(Path(td))
            successor = release.store.create(owner, 'local', 'omp', td, 'synthetic', True)
            tid = release.policy['task']
            release.store.begin_close(tid, owner)
            self.assertTrue(release.store.finish_close(tid, 4, True))
            task = release.store.get(tid)
            self.assertEqual(task['state'], 'closed')
            self.assertNotIn('resource_state', task)
            self.assertEqual(release.store.get(successor['id']), successor)


class EndedNativeCleanupTests(unittest.TestCase):
    setUp = owner_fixtures.EndedOwnerTests.setUp
    start = owner_fixtures.EndedOwnerTests.start
    action = owner_fixtures.EndedOwnerTests.action
    row = owner_fixtures.EndedOwnerTests.row
    end_owner = owner_fixtures.EndedOwnerTests.end_owner

    def native_task(self, kind='native-mode-settled', mode='swarm'):
        task = self.start(write=True)
        cancel = kind == 'verified-cancellation'
        self.store.update(task['id'], agent='agy', agy_swarm='native', agy_mode=mode,
            state='cancelling' if cancel else 'running', launch_id='launch',
            native_session_id='native-session', native_cancel_id='cancel', runner_pid=0)
        token = self.store.native_cleanup_owner(task['id'], 1, 'launch', 1)
        receipt = dict(kind=kind, mode=mode, outcome='completed',
            evidence=('parent-end-turn-complete-tool-history-and-unload' if mode == 'dynamic'
                      else 'public-response-and-session-unload'), session_unloaded=True,
            task=task['id'], turn=1, launch_id='launch', epoch=1, session_id='native-session')
        if cancel:
            receipt['cancel_id'] = 'cancel'
        self.store.native_cleanup_commit(task['id'], 1, 'launch', 1, token, receipt)
        self.store.update(task['id'], state='unknown' if cancel else 'completed')
        return task

    def test_ended_owner_only_settles_bound_receipt_without_controller_transfer(self):
        import callcli_plugin.release as release
        for kind, mode, action in [('native-mode-settled', 'swarm', 'close'),
                ('verified-cancellation', 'swarm', 'reconcile'),
                ('native-mode-settled', 'dynamic', 'close')]:
            with self.subTest(kind=kind, mode=mode):
                self.setUp()
                self.env['HERMES_SESSION_ID'] = 'old'
                task = self.native_task(kind, mode)
                before = self.store.get(task['id'])
                successor = self.start(write=True) if action == 'close' else None
                if successor:
                    successor = self.store.get(successor['id'])
                if not self.env.get('ended'):
                    self.end_owner()
                    self.env['ended'] = 'yes'
                self.env['HERMES_SESSION_ID'] = 'current'
                with patch.object(release, 'NativeBackend') as backend:
                    result = self.action(action, task, expected_turn=1)
                    backend.assert_not_called()
                self.assertEqual((result['task']['state'], result['receipt']['state']), ('closed', 'released'))
                after = self.store.get(task['id'])
                for field in ('origin', 'controller', 'control_epoch', 'native_cleanup'):
                    self.assertEqual(after[field], before[field])
                if successor:
                    self.assertEqual(self.store.get(successor['id']), successor)
                    self.store.update(successor['id'], state='completed')

    def test_wrong_receipt_route_profile_stale_turn_and_active_owner_remain_denied(self):
        import callcli_plugin.release as release
        task = self.native_task()
        self.end_owner()
        before = self.store.get(task['id'])
        with patch.object(release, 'NativeBackend') as backend:
            with self.assertRaises(ValueError):
                self.action('close', task, expected_turn=2)
            self.env['HERMES_SESSION_THREAD_ID'] = 'foreign'
            with self.assertRaises(PermissionError):
                self.action('close', task, expected_turn=1)
            self.env['HERMES_SESSION_THREAD_ID'] = 'topic'
            self.ctx.profile_name = 'foreign'
            with self.assertRaises(PermissionError):
                self.action('close', task, expected_turn=1)
            self.ctx.profile_name = 'default'
            backend.assert_not_called()
        self.assertEqual(self.store.get(task['id']), before)
        for field, value in [('native_session_id', 'wrong'), ('control_epoch', 2),
                ('native_cleanup', dict(before['native_cleanup'], session_id='forged')),
                ('state', 'running'), ('runner_pid', 123)]:
            with self.subTest(field=field):
                self.store.update(task['id'], **{field: value})
                snapshot = self.store.get(task['id'])
                with self.assertRaises(ValueError):
                    self.action('close', task, expected_turn=1)
                self.assertEqual(self.store.get(task['id']), snapshot)
                self.store.update(task['id'], **{field: before[field]})


class SettledUnknownRecoveryTests(unittest.TestCase):
    setUp = owner_fixtures.EndedOwnerTests.setUp
    start = owner_fixtures.EndedOwnerTests.start
    action = owner_fixtures.EndedOwnerTests.action

    def settled(self, *, legacy, pid):
        task = self.start(write=True)
        req = task['id'] + ':1'
        self.store.update(task['id'], state='unknown', runner_pid=pid,
            acpx_record_id='legacy-record', backend_request_id=req, exit_code=0,
            stop_reason='end_turn', turn_settlement=None if legacy else dict(
                turn=1, request_id=req, launch_id=task.get('launch_id'), protocol_settled=True))
        return task

    def exercise_recovery(self, task, backend):
        import callcli_plugin.release as release
        with patch.object(release, 'NativeBackend', return_value=backend):
            first = self.action('close', task, expected_turn=1)
            self.assertEqual((first['task']['state'], backend.closes, backend.probes), ('unknown', 0, 1))
            return self.action('release_only', task, expected_turn=1)

    def test_dead_pid_with_current_settlement_enters_strict_recovery(self):
        proc = subprocess.Popen([sys.executable, '-c', 'pass'])
        proc.wait()
        task = self.settled(legacy=False, pid=proc.pid)
        backend = StatefulCloseBackend()
        result = self.exercise_recovery(task, backend)
        self.assertEqual((result['task']['state'], backend.closes, backend.probes), ('closed', 1, 2))
        self.assertEqual(result['receipt']['business_state'], 'unknown')
        self.start(write=True)

    def test_legacy_settlement_enters_strict_recovery_without_synthesizing_identity(self):
        for dead in (False, True):
            with self.subTest(dead=dead):
                self.setUp()
                proc = subprocess.Popen([sys.executable, '-c', 'pass'])
                proc.wait()
                task = self.settled(legacy=True, pid=proc.pid if dead else 0)
                before = self.store.get(task['id'])
                backend = StatefulCloseBackend()
                result = self.exercise_recovery(task, backend)
                self.assertEqual((result['task']['state'], backend.closes, backend.probes), ('closed', 1, 2))
                after = self.store.get(task['id'])
                from callcli_plugin.recovery import recovery_card
                self.assertIn('single exact-record close', recovery_card(before)['next_action'])
                for field in ('origin', 'controller', 'control_epoch', 'backend_request_id', 'turn_settlement', 'runner_pid'):
                    self.assertEqual(after[field], before[field])

    def test_dead_pid_is_not_settlement_and_wrong_readback_keeps_hold(self):
        cases = [dict(exit_code=None), dict(stop_reason='error'), dict(backend_request_id='wrong'),
                 dict(turn_settlement={}), dict(turn_settlement={'protocol_settled': False})]
        for changed in cases:
            with self.subTest(changed=changed):
                proc = subprocess.Popen([sys.executable, '-c', 'pass'])
                proc.wait()
                self.setUp()
                task = self.settled(legacy=True, pid=proc.pid)
                self.store.update(task['id'], **changed)
                backend = StatefulCloseBackend()
                result = self.exercise_recovery(task, backend)
                self.assertEqual((result['task']['state'], backend.closes, backend.probes), ('unknown', 0, 2))
                with self.assertRaises(ValueError):
                    self.start(write=True)
        for wrong in ('record', 'request', 'backend'):
            with self.subTest(wrong=wrong):
                self.setUp()
                task = self.settled(legacy=True, pid=0)
                class WrongProof(StatefulCloseBackend):
                    def probe(self, policy):
                        proof = super().probe(policy)
                        if wrong == 'request':
                            proof['record']['lastRequestId'] = 'wrong'
                        else:
                            proof['binding']['acpxRecordId' if wrong == 'record' else 'acpxSessionId'] = 'wrong'
                        return proof
                backend = WrongProof()
                result = self.exercise_recovery(task, backend)
                self.assertEqual((result['task']['state'], backend.closes, backend.probes), ('unknown', 1, 2))
                import callcli_plugin.release as release
                with patch.object(release, 'NativeBackend', return_value=backend):
                    self.action('release_only', task, expected_turn=1)
                self.assertEqual(backend.closes, 1)
                with self.assertRaises(ValueError):
                    self.start(write=True)

    def test_live_invalid_and_unverifiable_pid_do_not_authorize_close(self):
        import callcli_plugin.release as release
        for pid in (os.getpid(), '123', -1, True):
            with self.subTest(pid=pid):
                self.setUp()
                task = self.settled(legacy=True, pid=pid)
                backend = StatefulCloseBackend()
                with patch.object(release, 'NativeBackend', return_value=backend):
                    self.action('close', task, expected_turn=1)
                    with self.assertRaises(ValueError):
                        self.action('release_only', task, expected_turn=1)
                self.assertEqual((backend.closes, backend.probes), (0, 1))
        self.setUp()
        task = self.settled(legacy=True, pid=98765)
        backend = StatefulCloseBackend()
        with patch.object(release, 'NativeBackend', return_value=backend), patch.object(release.os, 'kill', side_effect=PermissionError):
            self.action('close', task, expected_turn=1)
            with self.assertRaises(ValueError):
                self.action('release_only', task, expected_turn=1)
        self.assertEqual((backend.closes, backend.probes), (0, 1))


class StatefulCloseBackend(CloseBackend):
    def __init__(self):
        super().__init__(verified=False)

    def close(self, policy):
        result = super().close(policy)
        self.verified = True
        return result


class ResetCompressionTests(unittest.TestCase):
    setUp = continuity_fixtures.NativeContinuityTests.setUp
    row = continuity_fixtures.NativeContinuityTests.row
    start = continuity_fixtures.NativeContinuityTests.start
    action = continuity_fixtures.NativeContinuityTests.action

    def test_compression_of_post_reset_owner_keeps_reset_boundary(self):
        self.row('before', reason='session_reset', ended=1)
        before = self.start()
        self.row('reset-owner', parent='before', reason='compression', ended=2,
                 config='{"_reset_from":"before"}')
        self.env['HERMES_SESSION_ID'] = 'reset-owner'
        with self.assertRaises(PermissionError):
            self.action('status', before)
        after_reset = self.start()
        self.row('next', parent='reset-owner')
        self.env['HERMES_SESSION_ID'] = 'next'
        self.assertEqual(self.action('status', after_reset)['task']['id'], after_reset['id'])
        with self.assertRaises(PermissionError):
            self.action('status', before)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE sessions SET parent_session_id='reset-owner' WHERE id='before'")
        with self.assertRaises(PermissionError):
            self.action('status', after_reset)


if __name__ == '__main__':
    unittest.main()
