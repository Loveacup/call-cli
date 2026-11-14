"""Original native controller release-only: real SQLite, synthetic backend only."""
import importlib
import unittest
from unittest.mock import patch
import test_native_ended_owner as fixtures
from fixtures.close_backend import CloseBackend


class ReleaseOnlyTests(unittest.TestCase):
    setUp = fixtures.EndedOwnerTests.setUp
    start = fixtures.EndedOwnerTests.start
    action = fixtures.EndedOwnerTests.action

    def test_explicit_unknown_release_closes_once_without_business_success(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='unknown', runner_pid=0,
                          acpx_record_id='fixture-record', error='unverified result')
        class StatefulBackend(CloseBackend):
            def __init__(self):
                super().__init__(verified=False)
            def close(self, policy):
                self.assert_target(policy)
                result = super().close(policy)
                self.verified = True
                return result
            def assert_target(self, policy):
                if (policy['task'], policy['turn'], policy['record'], policy['request_id']) != (
                        task['id'], 1, 'fixture-record', task['id'] + ':1'):
                    raise AssertionError('wrong synthetic target')
        backend = StatefulBackend()
        release = importlib.import_module('callcli_plugin.release')
        with patch.object(release, 'NativeBackend', return_value=backend):
            result = self.action('release_only', task, expected_turn=1)
            repeated = self.action('release_only', task, expected_turn=1)
        self.assertEqual(result['task']['state'], 'closed')
        self.assertEqual(result['task']['error'], 'unverified result')
        self.assertEqual(result['receipt']['business_state'], 'unknown')
        self.assertEqual(result['receipt'], repeated['receipt'])
        self.assertEqual((backend.closes, backend.probes), (1, 1))
        self.assertEqual(len(self.ctx.calls), 1)  # only the synthetic original start
        self.assertFalse(self.store.finish_turn(task['id'], 1, 'completed', 0, 'end_turn'))
        self.start(write=True)  # reservation released only after exact proof

    def test_other_identity_and_ended_owner_have_no_release_authority(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='fixture-record')
        fixtures.EndedOwnerTests.row(self, 'old', 'agent_close', 1, 'subagent')
        fixtures.EndedOwnerTests.row(self, 'current')
        release = importlib.import_module('callcli_plugin.release')
        before = self.store.get(task['id'])
        with patch.object(release, 'NativeBackend') as backend:
            for key, value in [('HERMES_SESSION_ID', 'current'),
                               ('HERMES_SESSION_CHAT_ID', 'other'),
                               ('HERMES_SESSION_THREAD_ID', 'other')]:
                old = self.env[key]
                self.env[key] = value
                with self.assertRaises(PermissionError):
                    self.action('release_only', task, expected_turn=1)
                self.env[key] = old
            backend.assert_not_called()
        self.assertEqual(self.store.get(task['id']), before)

    def test_transferred_current_controller_cannot_borrow_original_authority(self):
        task = self.start()
        current = dict(task['origin'], session_id='current')
        self.store.update(task['id'], state='unknown', runner_pid=0,
                          acpx_record_id='fixture-record', controller=current, control_epoch=2)
        release = importlib.import_module('callcli_plugin.release')
        with patch.object(release, 'NativeBackend') as backend:
            for sid in ('old', 'current'):
                self.env['HERMES_SESSION_ID'] = sid
                with self.assertRaises(PermissionError):
                    self.action('release_only', task, expected_turn=1)
            backend.assert_not_called()

    def test_admission_boundaries_are_side_effect_free(self):
        task = self.start(write=True)
        base = dict(state='unknown', runner_pid=0, acpx_record_id='fixture-record',
                    device='local', agy_swarm='off')
        release = importlib.import_module('callcli_plugin.release')
        cases = [({'state': state}, 1) for state in
                 ('queued', 'running', 'cancelling', 'completed', 'cancelled', 'waiting_reply')]
        cases += [({'runner_pid': 42}, 1), ({'agy_swarm': 'native'}, 1),
                  ({'device': 'remote'}, 1), ({'acpx_record_id': ''}, 1),
                  ({}, None), ({}, 2), ({}, True)]
        for fields, turn in cases:
            with self.subTest(fields=fields, turn=turn):
                self.store.update(task['id'], **dict(base, **fields))
                before = self.store.get(task['id'])
                with patch.object(release, 'NativeBackend') as backend:
                    with self.assertRaises(ValueError):
                        self.action('release_only', task, expected_turn=turn)
                    self.assertFalse(backend.return_value.resolve.called)
                    self.assertFalse(backend.return_value.close.called)
                    self.assertFalse(backend.return_value.probe.called)
                self.assertEqual(self.store.get(task['id']), before)

    def test_failed_close_keeps_hold_and_retry_only_probes(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='fixture-record')
        release = importlib.import_module('callcli_plugin.release')
        def fail(policy):
            reserved = self.store.get(task['id'])
            self.assertEqual(reserved['state'], 'unknown')
            self.assertEqual(reserved['release_epoch'], 1)
            with self.store.connect() as db:
                import json
                receipt = json.loads(db.execute('SELECT receipt FROM release_fences WHERE task=?',
                                               (task['id'],)).fetchone()[0])
            self.assertEqual(receipt['attempts'][0]['reason_code'], 'in_progress')
            raise TimeoutError('synthetic close uncertainty')
        backend = CloseBackend(on_close=fail, verified=False)
        with patch.object(release, 'NativeBackend', return_value=backend):
            for _ in range(2):
                result = self.action('release_only', task, expected_turn=1)
                self.assertEqual(result['task']['state'], 'unknown')
                self.assertEqual(result['receipt']['business_state'], 'unknown')
                with self.assertRaises(ValueError): self.start(write=True)
            backend.verified = True
            result = self.action('release_only', task, expected_turn=1)
            repeated = self.action('release_only', task, expected_turn=1)
        self.assertEqual(result['receipt'], repeated['receipt'])
        self.assertEqual(result['task']['state'], 'closed')
        self.assertEqual(result['receipt']['first_failure']['reason_code'], 'close_uncertain')
        self.assertEqual((backend.closes, backend.probes), (1, 3))

    def test_mismatched_readback_cannot_release_writer(self):
        release = importlib.import_module('callcli_plugin.release')
        for field in ('record', 'acp', 'request', 'closed', 'status'):
            with self.subTest(field=field):
                task = self.start(write=True)
                self.store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='fixture-record')
                backend = CloseBackend()
                valid = backend.probe
                def bad(policy):
                    proof = valid(policy)
                    if field == 'record': proof['binding']['acpxRecordId'] = 'other'
                    elif field == 'acp': proof['binding']['acpxSessionId'] = 'other'
                    elif field == 'request': proof['record']['lastRequestId'] = 'other:2'
                    elif field == 'closed': proof['record']['closed'] = False
                    else: proof['status'] = 'running'
                    return proof
                backend.probe = bad
                with patch.object(release, 'NativeBackend', return_value=backend):
                    self.assertEqual(self.action('release_only', task, expected_turn=1)['task']['state'], 'unknown')
                    with self.assertRaises(ValueError): self.start(write=True)
                    backend.probe = valid
                    self.action('release_only', task, expected_turn=1)
                self.assertEqual(backend.closes, 1)

    def test_snapshot_change_during_resolve_prevents_close(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='fixture-record')
        backend = CloseBackend()
        def resolve(policy):
            self.store.update(task['id'], acpx_record_id='replacement')
            return {'acp': 'fixture-acp'}
        backend.resolve = resolve
        release = importlib.import_module('callcli_plugin.release')
        with patch.object(release, 'NativeBackend', return_value=backend):
            with self.assertRaisesRegex(ValueError, 'snapshot changed'):
                self.action('release_only', task, expected_turn=1)
        self.assertEqual((backend.closes, backend.probes), (0, 0))
        self.assertFalse(self.store.get(task['id']).get('release_epoch'))

    def test_concurrent_release_never_repeats_inflight_close(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        task = self.start(write=True)
        self.store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='fixture-record')
        entered, proceed = Event(), Event()
        backend = CloseBackend(verified=False)
        def close(policy):
            entered.set()
            if not proceed.wait(5): raise TimeoutError('test barrier')
            backend.verified = True
        backend.on_close = close
        release = importlib.import_module('callcli_plugin.release')
        with patch.object(release, 'NativeBackend', return_value=backend), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.action, 'release_only', task, expected_turn=1)
            try:
                self.assertTrue(entered.wait(3))
                result = self.action('release_only', task, expected_turn=1)
                self.assertEqual(result['task']['state'], 'unknown')
                self.assertEqual(backend.closes, 1)
            finally:
                proceed.set()
            future.result(timeout=5)
            result = self.action('release_only', task, expected_turn=1)
            self.assertEqual(result['task']['state'], 'closed')
        self.assertEqual(backend.closes, 1)

    def test_existing_probe_fence_never_becomes_close_permission(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='fixture-record')
        backend = CloseBackend(verified=False)
        release = importlib.import_module('callcli_plugin.release')
        with patch.object(release, 'NativeBackend', return_value=backend):
            self.action('close', task, expected_turn=1)
            self.action('release_only', task, expected_turn=1)
        self.assertEqual((backend.closes, backend.probes), (0, 2))
        self.assertEqual(self.store.get(task['id'])['state'], 'unknown')


if __name__ == '__main__':
    unittest.main()
