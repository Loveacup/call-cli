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
        self.assertEqual(result['resource_recovery'], {'task_id':task['id'], 'turn':7,
            'business_state':'unknown', 'resource_state':'unknown', 'write':True, 'receipt':None})
        self.assertEqual(result['diagnostic'], {
            'agent': 'omp', 'agy_mode': None, 'native_phase': None,
            'stop_reason': None, 'exit_code': None, 'pending_question_count': 0,
            'intervention_status': 'pending', 'native_diagnostic': None,
        })
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

    def test_cross_route_diagnostic_projection_is_closed_set_and_readonly(self):
        task = self.start(write=True)
        self.store.update(
            task['id'],
            agent='agy',
            agy_mode='swarm',
            native_phase='unknown',
            stop_reason='untrusted_free_text_reason',
            exit_code=True,
            state='unknown',
            runner_pid=0,
            prompt='CANARY_PROMPT',
            settlement_note='CANARY_NOTE',
            error='CANARY_ERROR',
            questions=['1:2'],
            answers={'1:1': 'CANARY_ANS'},
            native_observation={
                'turn': 1,
                'history_degraded': True,
                'input_available': False,
                'response_pending_reason': 'board_navigation_only',
                'pending_tools': [
                    {'id': 'CANARY_TID', 'name': 'CANARY_TOOL', 'kind': 'read', 'status': 'in_progress'},
                ],
                'question_blockers': [
                    {
                        'interaction_seq': 2,
                        'reason': 'incomplete_question',
                        'available_actions': ['observe', 'dialogue', 'cancel', 'CANARY_ACTION'],
                        'blocks_completion': True,
                    },
                ],
                'history_diagnostic': {
                    'reason_code': 'dialogue_missing',
                    'call_site': 'section',
                    'interaction_seq': 2,
                    'previous_len': 62,
                    'current_len': 25,
                    'background_growth': True,
                    'extra_secret': 'CANARY_EXTRA',
                },
            },
        )
        self.store.event(task['id'], 's1', 'stage', 'CANARY_STAGE')
        self.env.update(HERMES_SESSION_ID='current', HERMES_SESSION_KEY='other-route',
                        HERMES_SESSION_PLATFORM='cli', HERMES_SESSION_CHAT_ID='', HERMES_SESSION_THREAD_ID='')
        before = self.store.path.read_bytes()
        result = self.action('recovery_status', task)
        self.assertEqual(self.store.path.read_bytes(), before)
        diag = result['diagnostic']
        self.assertEqual(diag['agent'], 'agy')
        self.assertEqual(diag['agy_mode'], 'swarm')
        self.assertEqual(diag['native_phase'], 'unknown')
        self.assertEqual(diag['stop_reason'], 'other')
        self.assertIsNone(diag['exit_code'])
        self.assertEqual(diag['pending_question_count'], 1)
        self.assertEqual(diag['intervention_status'], 'none')
        nd = diag['native_diagnostic']
        self.assertIs(nd['history_degraded'], True)
        self.assertIs(nd['input_available'], False)
        self.assertEqual(nd['response_pending_reason'], 'board_navigation_only')
        self.assertEqual(nd['pending_tool_count'], 1)
        self.assertEqual(nd['pending_tool_kinds'], ['read'])
        self.assertEqual(nd['question_blockers'], [{
            'interaction_seq': 2,
            'reason': 'incomplete_question',
            'available_actions': ['observe', 'dialogue', 'cancel'],
            'blocks_completion': True,
        }])
        self.assertEqual(nd['history_diagnostic'], {
            'reason_code': 'dialogue_missing',
            'call_site': 'section',
            'interaction_seq': 2,
            'previous_len': 62,
            'current_len': 25,
            'background_growth': True,
        })
        dumped = json.dumps(result)
        for secret in ('CANARY_', str(self.root), 'other-route', 'old', 'telegram'):
            self.assertNotIn(secret, dumped)

    def test_resource_recovery_sanitizes_malicious_nested_values_and_preserves_literal_task_id(self):
        task = self.start(write=True)
        tid = task['id']
        canary = 'CANARY_NESTED_SECRET_/private/recovery'
        self.store.update(
            tid,
            state={'nested_state': canary},
            resource_state={'nested_secret': canary},
            turn=True,
            intervention={'status': {'nested_iv': canary}},
        )
        with self.store.connect() as db:
            raw = json.loads(db.execute('SELECT data FROM tasks WHERE id=?', (tid,)).fetchone()[0])
            raw['id'] = {'nested_id': canary}
            db.execute('UPDATE tasks SET data=? WHERE id=?', (json.dumps(raw), tid))
            db.execute('CREATE TABLE IF NOT EXISTS release_fences (task TEXT PRIMARY KEY, receipt TEXT)')
            db.execute(
                'INSERT OR REPLACE INTO release_fences (task, receipt) VALUES (?, ?)',
                (tid, json.dumps({
                    'state': 'released',
                    'operation': {'nested_op': canary},
                    'business_state': canary,
                    'external_close_attempted': [canary],
                    'extra': canary,
                })),
            )
        self.env.update(HERMES_SESSION_ID='current', HERMES_SESSION_KEY='other-route')
        before = self.store.path.read_bytes()
        result = self.action('recovery_status', task)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertNotIn(canary, json.dumps(result))
        self.assertEqual(result['resource_recovery'], {
            'task_id': tid,
            'turn': None,
            'business_state': 'unknown',
            'resource_state': 'released',
            'write': True,
            'receipt': {
                'state': 'released',
                'operation': 'unknown',
                'business_state': 'unknown',
                'external_close_attempted': None,
            },
        })
        self.assertEqual(result['diagnostic']['intervention_status'], 'unknown')

        raw_id = '  raw-LITERAL_id-1  '
        row = self.store.get(tid)
        row['id'] = raw_id
        row['state'] = 'unknown'
        row['resource_state'] = 'superseded'
        row['turn'] = 3
        with self.store.connect() as db:
            db.execute('INSERT INTO tasks VALUES (?, ?, ?)', (raw_id, json.dumps(row['origin']), json.dumps(row)))
        lit = self.action('recovery_status', {'id': raw_id})
        self.assertEqual(lit['resource_recovery']['task_id'], raw_id)
        self.assertEqual(lit['resource_recovery']['resource_state'], 'superseded')
        with self.assertRaises(KeyError):
            self.action('recovery_status', {'id': raw_id.strip()})

if __name__ == '__main__': unittest.main()
