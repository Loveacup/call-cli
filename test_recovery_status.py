"""Read-only resource projection; synthetic host/store only."""
import json
import unittest
from unittest.mock import patch
import test_native_ended_owner as fixtures

class RecoveryStatusTests(unittest.TestCase):
    setUp = fixtures.EndedOwnerTests.setUp
    start = fixtures.EndedOwnerTests.start
    action = fixtures.EndedOwnerTests.action

    def test_cross_route_snapshot_is_minimal_read_only_and_enables_exact_release(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='unknown', runner_pid=0, turn=7,
                          error='PRIVATE', answers={'q': 'PRIVATE'},
                          intervention={'status':'pending','prompt':'PRIVATE'})
        self.env.update(HERMES_SESSION_ID='current', HERMES_SESSION_KEY='other-route')
        with self.assertRaises(PermissionError): self.action('status', task)
        before = self.store.path.read_bytes()
        result = self.action('recovery_status', task)
        self.assertEqual(result, {'resource_recovery': {'task_id':task['id'], 'turn':7,
            'business_state':'unknown', 'resource_state':'unknown', 'write':True, 'receipt':None}})
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(len(self.ctx.calls), 1)
        self.assertNotIn('PRIVATE', json.dumps(result))
        release = self.action('release_only', task, expected_turn=result['resource_recovery']['turn'], abandon=True)
        self.assertEqual(release['receipt']['state'], 'released')
        readback = self.action('recovery_status', task)['resource_recovery']
        self.assertEqual(readback['resource_state'], 'released')
        self.assertEqual(readback['business_state'], 'unknown')
        self.assertEqual(set(readback['receipt']), {'state','operation','business_state','external_close_attempted'})
        with self.assertRaises(PermissionError): self.action('cancel', task)

    def test_strict_release_readback_preserves_unknown_business(self):
        import importlib
        from fixtures.close_backend import CloseBackend
        task = self.start()
        self.store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='fixture-record')
        self.env.update(HERMES_SESSION_ID='current', HERMES_SESSION_KEY='other-route')
        with patch.object(importlib.import_module('callcli_plugin.release'), 'NativeBackend', return_value=CloseBackend()):
            self.action('release_only', task, expected_turn=1)
        result = self.action('recovery_status', task)['resource_recovery']
        self.assertEqual(result['resource_state'], 'released')
        self.assertEqual(result['business_state'], 'unknown')

    def test_read_has_no_initialization_or_write_capability(self):
        import sqlite3
        missing = self.root/'missing'/'tasks.db'
        readonly = self.mod.Store(missing, action='recovery_status')
        with self.assertRaises(sqlite3.OperationalError):
            with readonly.connect(): pass
        self.assertFalse(missing.parent.exists())
        self.start()
        readonly = self.mod.Store(self.store.path, action='recovery_status')
        with readonly.connect() as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('CREATE TABLE forbidden(x)')

    def test_profile_identity_controls_and_active_snapshots(self):
        task = self.start(write=True)
        self.env.update(HERMES_SESSION_ID='current', HERMES_SESSION_KEY='other-route')
        for state in ('queued','running','cancelling','unknown'):
            self.store.update(task['id'], state=state)
            self.assertEqual(self.action('recovery_status', task)['resource_recovery']['resource_state'], state)
        for profile in ('foreign','standalone','omp-extension'):
            self.ctx.profile_name = profile
            with self.assertRaises(PermissionError): self.action('recovery_status', task)
        self.ctx.profile_name = 'default'
        for action in ('events','reply','intervene','cancel'):
            with self.assertRaises(PermissionError): self.action(action, task)
        with self.assertRaises(KeyError): self.action('recovery_status', {'id':'nonexistent'})
        for field, value in (('abandon',True),('expected_turn',1),('prompt','private'),('write',True)):
            with self.assertRaises(ValueError): self.action('recovery_status', task, **{field:value})
        self.env.pop('HERMES_SESSION_ID')
        with self.assertRaises(ValueError): self.action('recovery_status', task)

    def test_observed_turn_does_not_authorize_stale_release(self):
        task = self.start()
        self.store.update(task['id'], state='unknown', runner_pid=0)
        self.env.update(HERMES_SESSION_ID='current', HERMES_SESSION_KEY='other-route')
        observed = self.action('recovery_status', task)['resource_recovery']['turn']
        self.store.update(task['id'], turn=observed+1)
        with self.assertRaises(ValueError):
            self.action('release_only', task, expected_turn=observed, abandon=True)
        self.assertEqual(self.action('recovery_status', task)['resource_recovery']['resource_state'], 'unknown')

if __name__ == '__main__': unittest.main()
