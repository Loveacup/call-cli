"""Strict release and current-host recovery; isolated SQLite and synthetic backend."""
import importlib
import unittest
from unittest.mock import patch
import test_native_ended_owner as fixtures
from fixtures.close_backend import CloseBackend


class ReleaseOnlyTests(unittest.TestCase):
    setUp = fixtures.EndedOwnerTests.setUp
    start = fixtures.EndedOwnerTests.start
    action = fixtures.EndedOwnerTests.action

    def queued_standalone(self):
        owner = self.mod.Origin('standalone', 'private-client', 'private-client', 'hermes')
        task = self.store.create(owner, 'local', 'omp', str(self.root), 'private-prompt', False,
                                 initiating_surface='standalone')
        return self.store.update(task['id'], needs_session=True, acpx_created=False,
                                 session_creation_attempted=False)

    def test_force_retires_never_launched_standalone_without_owner_or_backend(self):
        task = self.queued_standalone()
        with self.assertRaises(ValueError):
            self.action('release_only', task, expected_turn=1)
        release = importlib.import_module('callcli_plugin.release')
        with patch.object(release, 'NativeBackend') as backend:
            result = self.action('release_only', task, expected_turn=1, abandon=True)
            self.assertEqual(result, self.action('release_only', task, expected_turn=1, abandon=True))
            backend.assert_not_called()
        recovery = self.action('recovery_status', task)['resource_recovery']
        self.assertEqual(recovery['business_state'], 'cancelled')
        self.assertEqual(recovery['resource_state'], 'released')
        self.assertEqual(recovery['receipt']['operation'], 'unlaunched-queue-retirement')
        self.assertFalse(recovery['receipt']['external_close_attempted'])
        self.assertNotIn('private-client', repr(result))
        self.assertNotIn('private-prompt', repr(result))
        self.assertNotIn(str(self.root), repr(result))
        self.assertEqual(self.store.get(task['id'])['origin'], task['origin'])
        with self.assertRaises(ValueError):
            self.store.claim_launch(task['id'], 1)

    def test_force_never_retires_a_claimed_or_unproven_queue(self):
        for fields in ({'process_id': 'attempt'}, {'write': True},
                       {'session_creation_attempted': True}, {'turn': 2}, {'device': 'remote'}):
            with self.subTest(fields=fields):
                task = self.queued_standalone()
                before = self.store.update(task['id'], **fields)
                with self.assertRaises(ValueError):
                    self.action('release_only', task, expected_turn=before['turn'], abandon=True)
                self.assertEqual(self.store.get(task['id']), before)
        task = self.queued_standalone()
        self.store.event(task['id'], 'started', 'started', 'claimed externally')
        with self.assertRaises(ValueError):
            self.action('release_only', task, expected_turn=1, abandon=True)
        with self.assertRaises(ValueError):
            self.action('release_only', self.queued_standalone(), expected_turn=2, abandon=True)

    def test_standalone_exit_after_first_commit_leaves_a_retirable_task(self):
        from collab import Collaboration
        facade = Collaboration(db=self.store.path)
        client = facade.connect('hermes')['client_id']
        create = facade.store.create
        def exit_after_create(*args, **kwargs):
            create(*args, **kwargs)
            raise InterruptedError('standalone caller exited after first commit')
        with patch.object(facade.store, 'create', side_effect=exit_after_create):
            with self.assertRaises(InterruptedError):
                facade.start(client, agent='omp', cwd=str(self.root), prompt='unlaunched')
        task, = facade.list(client)
        result = self.action('release_only', task, expected_turn=1, abandon=True)
        self.assertEqual(result['resource_recovery']['business_state'], 'cancelled')
        self.assertEqual(result['resource_recovery']['resource_state'], 'released')

    def test_launch_claim_wins_before_dispatch_even_without_a_process(self):
        task = self.queued_standalone()
        claimed = self.store.claim_launch(task['id'], 1)
        with self.assertRaises(ValueError):
            self.action('release_only', task, expected_turn=1, abandon=True)
        self.assertEqual(self.store.get(task['id']), claimed)

    def test_queue_retirement_requires_trusted_host(self):
        from collab import Collaboration
        facade = Collaboration(db=self.store.path)
        client = facade.connect('hermes')['client_id']
        with patch.object(facade, '_dispatch'):
            task = facade.start(client, agent='omp', cwd=str(self.root), prompt='unlaunched')
        with self.assertRaises(ValueError):
            facade.release_only(client, task['id'], expected_turn=1, abandon=True)
        self.assertEqual(facade.status(client, task['id']), task)

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

    def test_current_host_can_strictly_release_an_ended_owner_task(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='fixture-record')
        self.env['HERMES_SESSION_ID'] = 'current'
        self.env['HERMES_SESSION_KEY'] = 'current-route'
        release = importlib.import_module('callcli_plugin.release')
        backend = CloseBackend()
        with patch.object(release, 'NativeBackend', return_value=backend):
            result = self.action('release_only', task, expected_turn=1)
        self.assertEqual(result['receipt']['actor']['session_id'], 'current')
        self.assertEqual(result['task']['origin'], task['origin'])
        self.assertEqual(result['task']['controller'], task['controller'])
        self.assertEqual(result['task']['state'], 'closed')
        self.assertEqual(backend.closes, 1)

    def test_foreign_profile_does_not_grant_release_authority(self):
        task = self.start()
        self.store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='fixture-record')
        self.ctx.profile_name = 'foreign'
        with self.assertRaises(PermissionError):
            self.action('release_only', task, expected_turn=1)

    def test_admission_boundaries_are_side_effect_free(self):
        task = self.start(write=True)
        base = dict(state='unknown', runner_pid=0, acpx_record_id='fixture-record',
                    device='local', agy_swarm='off')
        release = importlib.import_module('callcli_plugin.release')
        cases = [({'state': state}, 1) for state in
                 ('queued', 'running', 'cancelling', 'completed', 'cancelled', 'waiting_reply')]
        import os
        cases += [({'runner_pid': os.getpid()}, 1), ({}, None), ({}, 2), ({}, True)]
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


    def test_reconcile_after_abandon_returns_receipt_without_backend(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='fixture-record')
        released = self.action('release_only', task, expected_turn=1, abandon=True)
        release = importlib.import_module('callcli_plugin.release')
        with patch.object(release, 'NativeBackend') as backend:
            reconciled = self.action('reconcile', task, expected_turn=1)
        self.assertEqual(reconciled['receipt'], released['receipt'])
        backend.return_value.resolve.assert_not_called()
        backend.return_value.probe.assert_not_called()

    def test_recovery_of_existing_fence_audits_current_actor(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='fixture-record')
        release = importlib.import_module('callcli_plugin.release')
        backend = CloseBackend(verified=False)
        with patch.object(release, 'NativeBackend', return_value=backend):
            held = self.action('close', task, expected_turn=1)
            self.env['HERMES_SESSION_ID'] = 'current'
            backend.verified = True
            recovered = self.action('release_only', task, expected_turn=1)
        self.assertEqual(recovered['receipt']['attempts'][:1], held['receipt']['attempts'])
        self.assertEqual(recovered['receipt']['attempts'][-1]['actor']['session_id'], 'current')
        self.assertEqual(recovered['receipt']['business_state'], 'unknown')

    def test_settled_missing_final_allows_single_strict_close_after_probe_fence(self):
        task = self.start(write=True)
        settlement = dict(turn=1, request_id='req-1', launch_id=task.get('launch_id') or 'launch-1',
                          protocol_settled=True, reason_code='no_post_tool_final_answer')
        self.store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='fixture-record',
                          backend_request_id='req-1', launch_id=settlement['launch_id'],
                          turn_settlement=settlement)
        class StatefulBackend(CloseBackend):
            def __init__(self):
                super().__init__(verified=False)
            def close(self, policy):
                result = super().close(policy)
                self.verified = True
                return result
        backend = StatefulBackend()
        release = importlib.import_module('callcli_plugin.release')
        with patch.object(release, 'NativeBackend', return_value=backend):
            held = self.action('close', task, expected_turn=1)
            self.assertEqual(held['task']['state'], 'unknown')
            self.assertEqual((backend.closes, backend.probes), (0, 1))
            released = self.action('release_only', task, expected_turn=1)
        self.assertEqual(released['task']['state'], 'closed')
        self.assertEqual(released['receipt']['business_state'], 'unknown')
        self.assertEqual((backend.closes, backend.probes), (1, 2))
        self.start(write=True)

    def test_settled_missing_final_failed_close_never_replays_close(self):
        task = self.start(write=True)
        settlement = dict(turn=1, request_id='req-1', launch_id=task.get('launch_id') or 'launch-1',
                          protocol_settled=True, reason_code='no_post_tool_final_answer')
        self.store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='fixture-record',
                          backend_request_id='req-1', launch_id=settlement['launch_id'],
                          turn_settlement=settlement)
        def fail(policy):
            raise TimeoutError('synthetic close timeout')
        backend = CloseBackend(on_close=fail, verified=False)
        release = importlib.import_module('callcli_plugin.release')
        with patch.object(release, 'NativeBackend', return_value=backend):
            first = self.action('release_only', task, expected_turn=1)
            second = self.action('release_only', task, expected_turn=1)
        self.assertEqual(first['task']['state'], 'unknown')
        self.assertEqual(second['task']['state'], 'unknown')
        self.assertEqual((backend.closes, backend.probes), (1, 2))

    def test_stale_turn_or_wrong_request_or_launch_never_authorizes_close_after_probe(self):
        for bad_field, bad_val in (('turn', 0), ('request_id', 'wrong-req'), ('launch_id', 'wrong-launch')):
            with self.subTest(bad_field=bad_field):
                task = self.start(write=True)
                settlement = dict(turn=1, request_id='req-1', launch_id=task.get('launch_id') or 'launch-1',
                                  protocol_settled=True, reason_code='no_post_tool_final_answer')
                settlement[bad_field] = bad_val
                self.store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='fixture-record',
                                  backend_request_id='req-1', launch_id=task.get('launch_id') or 'launch-1',
                                  turn_settlement=settlement)
                backend = CloseBackend(verified=False)
                release = importlib.import_module('callcli_plugin.release')
                with patch.object(release, 'NativeBackend', return_value=backend):
                    self.action('close', task, expected_turn=1)
                    self.action('release_only', task, expected_turn=1)
                self.assertEqual((backend.closes, backend.probes), (0, 2))
                self.assertEqual(self.store.get(task['id'])['state'], 'unknown')
                self.action('release_only', task, expected_turn=1, abandon=True)

if __name__ == '__main__':
    unittest.main()
