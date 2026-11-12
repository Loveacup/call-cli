"""Controller, observed-turn and dispatch identity regressions with synthetic state."""
from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch
import test_native_ended_owner as fixtures


class ControllerTests(unittest.TestCase):
    setUp = fixtures.EndedOwnerTests.setUp
    row = fixtures.EndedOwnerTests.row
    start = fixtures.EndedOwnerTests.start
    action = fixtures.EndedOwnerTests.action
    end_owner = fixtures.EndedOwnerTests.end_owner
    def continue_task(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='completed')
        self.end_owner()
        return task, self.action('reply', task, prompt='new business',
                                 expected_turn=1, command_id='new-owner')['task']

    def test_current_controller_can_cancel_after_original_owner_reopens(self):
        task, resumed = self.continue_task()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE sessions SET ended_at=NULL,end_reason=NULL WHERE id='old'")
        self.assertEqual(self.action('status', task)['task']['controller']['session_id'], 'current')
        self.assertEqual(self.action('cancel', task)['task']['state'], 'cancelled')
        self.env['HERMES_SESSION_ID'] = 'old'
        self.assertEqual(self.action('status', task)['task']['turn'], 2)
        with self.assertRaises(PermissionError):
            self.action('intervene', task, prompt='steal back', command_id='old', expected_turn=2)

    def test_late_reply_and_retry_do_not_start_another_turn(self):
        task, resumed = self.continue_task()
        launches = len(self.ctx.calls)
        self.action('reply', task, prompt='new business', expected_turn=1, command_id='new-owner')
        self.assertEqual(len(self.ctx.calls), launches)
        self.store.update(task['id'], state='completed')
        with self.assertRaises(ValueError):
            self.action('reply', task, prompt='late intent', expected_turn=1, command_id='late')
        self.assertEqual(self.store.get(task['id'])['turn'], 2)

    def test_old_dispatch_cannot_claim_successor_or_use_missing_identity(self):
        task = self.start()
        old_command = self.ctx.called[1]['command']
        self.action('cancel', task)
        resumed = self.action('intervene', task, prompt='new work', command_id='next')['task']
        import shlex
        argv = shlex.split(old_command)
        self.assertIn('--turn', argv)
        self.assertIn('--launch-id', argv)
        self.assertIn('--epoch', argv)
        import callcli_plugin.runner as runner
        with self.assertRaises(ValueError):
            runner.run_task(str(self.store.path), task['id'], turn=1,
                launch_id=task['launch_id'], epoch=1)
        with self.assertRaises(ValueError):
            runner.run_task(str(self.store.path), task['id'])
        self.assertEqual((self.store.get(task['id'])['turn'], self.store.get(task['id'])['state']), (2, 'queued'))


    def test_native_unknown_reconcile_closes_record_layer_without_close_replay(self):
        task = self.start(write=True)
        self.store.update(task['id'],state='unknown',acpx_record_id='synthetic-record')
        self.end_owner()
        import callcli_plugin.release as release
        class Backend:
            closes = 0
            def resolve(self, policy): return {'acp':'synthetic-acp'}
            def close(self, policy): self.closes += 1; raise AssertionError('NO CLOSE')
            def probe(self, policy):
                return dict(verified=True,binding=dict(acpxRecordId=policy['record'],acpxSessionId=policy['acp'],
                    session=policy['session'],agent=policy['agent'],cwd=policy['cwd']),
                    record=dict(closed=True,lastRequestId=policy['task']+':'+str(policy['turn'])),
                    status='no-session',source='shared-runtime-exact-record-probe',arbitraryDetachedDescendantsProven=False)
        backend = Backend()
        with patch.object(release,'NativeBackend',return_value=backend):
            result = self.action('reconcile',task,expected_turn=1)
            self.assertEqual((result['receipt']['state'],backend.closes),('released',0))
            self.assertEqual(self.action('reconcile',task,expected_turn=1)['receipt'],result['receipt'])
        self.assertFalse(self.store.event(task['id'],'late','stage','ignored'))
        with self.assertRaises(ValueError): self.store.update(task['id'],state='queued')

    def test_start_and_status_recovery_card_do_not_claim_exit_hook(self):
        task = self.start()
        result = self.action('status',task)
        card = result['recovery']
        self.assertEqual((card['task_id'],card['turn'],card['control_epoch']),(task['id'],1,1))
        self.assertEqual(card['responsibility']['coverage'],'uncovered')
        self.assertIs(card['responsibility']['pre_exit_hook'],False)
        self.assertNotIn('prompt',card)

    def test_admitted_dispatch_survives_reopen_and_running_controller_can_cancel(self):
        task, resumed = self.continue_task()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE sessions SET ended_at=NULL,end_reason=NULL WHERE id='old'")
        import callcli_plugin.runner as runner
        from callcli_plugin.backends import TurnResult
        test = self
        class Backend:
            def run(self, store, current, emit):
                test.assertEqual(test.action('cancel',task)['task']['state'],'cancelling')
                test.env['HERMES_SESSION_ID'] = 'old'
                with test.assertRaises(PermissionError): test.action('cancel',task)
                test.env['HERMES_SESSION_ID'] = 'current'
                return TurnResult('cancelled',0,'cancelled')
        with patch.object(runner,'backend_for',return_value=Backend()):
            self.assertEqual(runner.run_task(self.store.path,task['id'],turn=2,
                launch_id=resumed['launch_id'],epoch=2),0)
        self.assertEqual(self.action('status',task)['task']['state'],'cancelled')
        with self.assertRaises(ValueError):
            runner.run_task(self.store.path,task['id'],turn=2,launch_id=resumed['launch_id'],epoch=2)

    def test_legacy_unclaimed_first_turn_is_supported_but_successors_require_binding(self):
        from bridge import Origin, Store
        from runner import run_task
        from backends import TurnResult
        store = Store(self.root/'legacy.db')
        owner = Origin('standalone','legacy','legacy')
        task = store.create(owner,'local','omp',str(self.root),'legacy first',False)
        class Backend:
            def run(self, store, current, emit): return TurnResult('completed',0,'end_turn')
        with patch('runner.backend_for',return_value=Backend()):
            self.assertEqual(run_task(store.path,task['id']),0)
        store.reply(task['id'],owner,'','successor')
        with self.assertRaises(ValueError): run_task(store.path,task['id'])

    def test_cli_transferred_controller_compression_remains_discoverable(self):
        self.env = {'HERMES_SESSION_ID':'old'}
        task = self.start()
        self.store.update(task['id'],state='completed')
        self.end_owner()
        self.action('reply',task,prompt='admit',expected_turn=1,command_id='admit')
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE sessions SET ended_at=2,end_reason='compression' WHERE id='current'")
            db.execute("INSERT INTO sessions VALUES ('compressed','current',NULL,NULL,NULL,'cli',NULL)")
        self.env = {'HERMES_SESSION_ID':'compressed','HERMES_SESSION_KEY':'current'}
        self.assertEqual([t['id'] for t in self.c.handle({'action':'list'})['tasks']],[task['id']])
        self.assertEqual(self.action('cancel',task)['task']['state'],'cancelled')

    def test_new_native_no_question_reply_requires_observed_intent_and_legacy_task_still_works(self):
        task = self.start()
        self.store.update(task['id'],state='completed')
        with self.assertRaises(ValueError): self.action('reply',task,prompt='unbound')
        self.store.update(task['id'],continuation_version=1)
        self.assertEqual(self.action('reply',task,prompt='legacy explicit continue')['task']['turn'],2)

    def test_former_origin_inspection_does_not_authorize_recovery(self):
        task, resumed = self.continue_task()
        self.store.update(task['id'],state='unknown',acpx_record_id='fixture-record')
        self.env['HERMES_SESSION_ID'] = 'old'
        self.assertEqual(self.action('status',task)['task']['state'],'unknown')
        with self.assertRaises(PermissionError): self.action('reconcile',task,expected_turn=2)

    def test_recovery_card_preserves_surface_without_private_db_or_command_prompt(self):
        task = self.start()
        self.store.update(task['id'], state='completed',
            commands={'prior': {'prompt': 'PRIVATE_REVISION', 'status': 'target_completed'}},
            intervention={'prompt': 'PRIVATE_REVISION', 'status': 'target_completed'})
        result = self.action('status', task)
        self.assertNotIn('db', result['recovery'])
        self.assertEqual(result['recovery']['responsibility']['host'], 'hermes-native')
        self.assertNotIn('PRIVATE_REVISION', json.dumps(result))
        self.store.update(task['id'],state='running')
        self.store.finish_turn(task['id'],1,'completed',0,'end_turn')
        event = self.action('events',task)['events'][-1]
        self.assertNotIn('db',event['recovery'])
        self.assertEqual(event['recovery']['responsibility']['host'],'hermes-native')

    def test_reconcile_healthy_task_does_not_freeze_business(self):
        task = self.start()
        import callcli_plugin.release as release
        class Backend:
            def resolve(self, policy): raise AssertionError('Healthy reconcile must not probe')
        for state in ('waiting_reply','completed','cancelled'):
            self.store.update(task['id'],state=state,acpx_record_id='synthetic-record')
            before = self.store.get(task['id'])
            with patch.object(release,'NativeBackend',return_value=Backend()):
                with self.assertRaises(ValueError): self.action('reconcile',task,expected_turn=1)
            self.assertEqual(self.store.get(task['id']),before)
        self.store.update(task['id'],state='waiting_reply')
        self.end_owner()
        with patch.object(release,'NativeBackend',return_value=Backend()):
            with self.assertRaises(ValueError): self.action('close',task,expected_turn=1)
        self.assertNotIn('release_epoch',self.store.get(task['id']))

if __name__ == '__main__':
    unittest.main()
