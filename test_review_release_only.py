"""Independent scoped acceptance; synthetic backend and scratch SQLite only."""
import importlib
import json
import unittest
from unittest.mock import patch
import test_native_ended_owner as fixtures
from fixtures.close_backend import CloseBackend

class IndependentReleaseTests(unittest.TestCase):
    setUp = fixtures.EndedOwnerTests.setUp
    start = fixtures.EndedOwnerTests.start
    action = fixtures.EndedOwnerTests.action

    def prepared(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='unknown', runner_pid=0,
                          acpx_record_id='fixture-record', error='business not accepted')
        return task

    def test_all_admission_bindings_are_rechecked_after_resolve(self):
        release = importlib.import_module('callcli_plugin.release')
        for field, value in [('runner_pid', 12), ('device', 'remote'), ('state', 'running'),
                             ('turn', 2), ('session', 'replacement'), ('cwd', '/replacement'),
                             ('controller', {'session_id':'other'})]:
            with self.subTest(field=field):
                task = self.prepared()
                backend = CloseBackend()
                def resolve(policy):
                    self.store.update(task['id'], **{field:value})
                    return {'acp':'fixture-acp'}
                backend.resolve = resolve
                with patch.object(release, 'NativeBackend', return_value=backend):
                    with self.assertRaisesRegex(ValueError, 'snapshot changed'):
                        self.action('release_only', task, expected_turn=1)
                self.assertEqual((backend.closes, backend.probes), (0,0))
                self.store.update(task['id'], state='closed', runner_pid=0)

    def test_fence_write_failure_cannot_dispatch_close(self):
        release = importlib.import_module('callcli_plugin.release')
        task = self.prepared()
        backend = CloseBackend()
        with patch.object(release, 'NativeBackend', return_value=backend), \
             patch.object(release.Release, '_publish', side_effect=OSError('synthetic storage failure')):
            with self.assertRaises(OSError):
                self.action('release_only', task, expected_turn=1)
        self.assertEqual((backend.closes, backend.probes), (0,0))
        self.assertEqual(self.store.get(task['id'])['state'], 'unknown')
        with self.assertRaises(ValueError): self.start(write=True)

    def test_close_observes_committed_fence_and_probe_failure_holds(self):
        release = importlib.import_module('callcli_plugin.release')
        task = self.prepared()
        def on_close(policy):
            with self.store.connect() as db:
                row = db.execute('SELECT receipt FROM release_fences WHERE task=?', (task['id'],)).fetchone()
            receipt = json.loads(row[0])
            self.assertEqual(receipt['business_state'], 'unknown')
            self.assertEqual(receipt['attempts'][0]['reason_code'], 'in_progress')
            self.assertEqual(receipt['target'], policy)
        backend = CloseBackend(on_close=on_close)
        backend.probe = lambda policy: (_ for _ in ()).throw(TimeoutError('synthetic probe failure'))
        with patch.object(release, 'NativeBackend', return_value=backend):
            for _ in range(2):
                result = self.action('release_only', task, expected_turn=1)
                self.assertEqual(result['receipt']['phase'], 'hold')
                self.assertEqual(result['receipt']['business_state'], 'unknown')
                self.assertEqual(result['task']['state'], 'unknown')
                self.assertEqual(result['task']['error'], 'business not accepted')
                with self.assertRaises(ValueError): self.start(write=True)
        self.assertEqual(backend.closes, 1)
        self.assertEqual(len(self.ctx.calls), 1)

if __name__ == '__main__':
    unittest.main()
