"""Public close must never use text receipts or replay unknown execution."""
import importlib
import unittest
from unittest.mock import patch
import test_native_ended_owner as fixtures


class CloseSafetyTests(unittest.TestCase):
    setUp = fixtures.EndedOwnerTests.setUp
    start = fixtures.EndedOwnerTests.start
    action = fixtures.EndedOwnerTests.action

    def test_unknown_same_controller_close_never_calls_close_again(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='unknown', acpx_record_id='exact-record')
        calls = []
        class Backend:
            def resolve(self, policy): return {'acp':'fixture-acp'}
            def probe(self, policy): return {'verified':False}
            def close(self, policy): calls.append('exact-close')
        release = importlib.import_module('callcli_plugin.release')
        with patch.object(importlib.import_module('callcli_plugin.runner'), 'control', side_effect=lambda *a: calls.append('legacy-close')), patch.object(release, 'NativeBackend', return_value=Backend()):
            for _ in range(2):
                try: self.action('close', task, expected_turn=1)
                except RuntimeError: pass
        self.assertEqual(calls, [])
        self.assertEqual(self.store.get(task['id'])['state'], 'unknown')

    def test_malformed_no_session_text_cannot_release_writer(self):
        task = self.start(write=True)
        self.store.update(task['id'], state='unknown', acpx_record_id='exact-record')
        class Backend:
            def resolve(self, policy): return {'acp':'fixture-acp'}
            def probe(self, policy): return 'not JSON no-session'
            def close(self, policy): raise AssertionError('unknown must not close')
        release = importlib.import_module('callcli_plugin.release')
        with patch.object(release, 'NativeBackend', return_value=Backend()):
            self.action('close', task, expected_turn=1)
        self.assertEqual(self.store.get(task['id'])['state'], 'unknown')
        with self.assertRaises(ValueError): self.start(write=True)


    def test_all_public_surfaces_unknown_close_probe_only_and_exact_proof(self):
        from collab import Collaboration
        from omp_bridge import HostBridge
        from pathlib import Path
        from fixtures.close_backend import CloseBackend
        for surface in ('hermes','cli','omp'):
            with self.subTest(surface=surface):
                if surface == 'hermes':
                    task = self.start(write=True)
                    store = self.store
                    close = lambda: self.action('close',task,expected_turn=1)
                    module = importlib.import_module('callcli_plugin.release')
                else:
                    bridge = (HostBridge if surface=='omp' else Collaboration)(Path(self.tmp.name)/(surface+'.db'),launch=lambda t:None)
                    if surface=='omp':
                        host = dict(session_id='host',agent_id='agent')
                        client = bridge.bind(host)
                        task = bridge.handle(host,client,dict(action='start',cwd='/fixture',prompt='x',write=True))['task']
                        close = lambda: bridge.handle(host,client,dict(action='close',task_id=task['id']))
                    else:
                        client = bridge.connect('pi')['client_id']
                        task = bridge.start(client,cwd='/fixture',prompt='x',write=True)
                        close = lambda: bridge.close(client,task['id'])
                    store = bridge.store
                    module = importlib.import_module('release')
                store.update(task['id'],state='unknown',acpx_record_id='fixture-record')
                backend = CloseBackend(verified=False)
                with patch.object(module,'NativeBackend',return_value=backend):
                    close(); close()
                    self.assertEqual((backend.closes,backend.probes),(0,2))
                    self.assertEqual(store.get(task['id'])['state'],'unknown')
                    backend.verified=True
                    close(); close()
                    self.assertEqual((backend.closes,backend.probes),(0,3))
                    self.assertEqual(store.get(task['id'])['state'],'closed')

    def test_same_controller_concurrent_close_and_reconcile_preserve_single_close(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from fixtures.close_backend import CloseBackend
        task = self.start(write=True)
        self.store.update(task['id'],state='completed',acpx_record_id='fixture-record')
        entered, proceed = Event(), Event()
        def delayed(policy):
            entered.set()
            if not proceed.wait(5): raise TimeoutError('test close barrier')
            raise TimeoutError('lost receipt')
        backend = CloseBackend(on_close=delayed)
        release = importlib.import_module('callcli_plugin.release')
        with patch.object(release,'NativeBackend',return_value=backend), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.action,'close',task,expected_turn=1)
            try:
                self.assertTrue(entered.wait(3))
                self.assertFalse(self.store.finish_turn(task['id'],1,'completed',0,'end_turn'))
                settled = self.action('reconcile',task,expected_turn=1)
                self.assertEqual(settled['task']['state'],'closed')
            finally: proceed.set()
            self.assertEqual(future.result(timeout=5)['task']['state'],'closed')
        self.assertEqual(backend.closes,1)
        self.assertFalse(self.store.event(task['id'],'late','stage','ignored'))

    def test_mismatched_proof_never_releases_same_controller_writer(self):
        from fixtures.close_backend import CloseBackend
        for field in ('binding','session','request','status','closed'):
            with self.subTest(field=field):
                task = self.start(write=True)
                self.store.update(task['id'],state='unknown',acpx_record_id='fixture-record')
                backend = CloseBackend()
                valid = backend.probe
                def bad(policy):
                    proof = valid(policy)
                    if field=='binding': proof['binding']['acpxRecordId']='other'
                    elif field=='session': proof['binding']['acpxSessionId']='other'
                    elif field=='request': proof['record']['lastRequestId']='other'
                    elif field=='status': proof['status']='not JSON no-session'
                    else: proof['record']['closed']=False
                    return proof
                backend.probe=bad
                release = importlib.import_module('callcli_plugin.release')
                with patch.object(release,'NativeBackend',return_value=backend):
                    self.assertEqual(self.action('close',task,expected_turn=1)['task']['state'],'unknown')
                with self.assertRaises(ValueError): self.start(write=True)
                # Close only by valid read-only proof so the next fixture can reserve.
                backend.probe=valid
                with patch.object(release,'NativeBackend',return_value=backend):
                    self.action('close',task,expected_turn=1)

    def test_transferred_controller_and_ended_controller_use_same_close_fence(self):
        from fixtures.close_backend import CloseBackend
        self.row = fixtures.EndedOwnerTests.row.__get__(self)
        self.end_owner = fixtures.EndedOwnerTests.end_owner.__get__(self)
        for transfer in (False,True):
            self.env['HERMES_SESSION_ID']='old'
            task=self.start()
            self.store.update(task['id'],state='completed',acpx_record_id='fixture-record')
            if not transfer:
                self.end_owner()
            else:
                self.env['HERMES_SESSION_ID']='current'
                self.action('reply',task,prompt='explicit',expected_turn=1,command_id='takeover')
                self.store.update(task['id'],state='completed')
            backend=CloseBackend()
            release=importlib.import_module('callcli_plugin.release')
            with patch.object(release,'NativeBackend',return_value=backend):
                result=self.action('close',task,expected_turn=2 if transfer else 1)
            self.assertEqual(result['task']['state'],'closed')
            self.assertEqual(backend.closes,1)
            self.assertTrue(result['task']['release_epoch'])

    def test_missing_record_and_remote_close_hold_without_external_control(self):
        from collab import Collaboration
        from pathlib import Path
        for remote in (False,True):
            bridge=Collaboration(Path(self.tmp.name)/('remote.db' if remote else 'missing.db'),launch=lambda t:None)
            client=bridge.connect('pi')['client_id']
            task=bridge.start(client,cwd='/fixture',prompt='x',write=True)
            fields=dict(state='unknown')
            if remote: fields.update(device='remote',acpx_record_id='fixture-record')
            bridge.store.update(task['id'],**fields)
            with patch('release.NativeBackend') as backend:
                for _ in range(2):
                    result=bridge.close(client,task['id'])
                    self.assertEqual(result['state'],'unknown')
                    self.assertEqual(result['close_receipt']['reason_code'],'exact_record_unavailable')
                backend.assert_not_called()
            with self.assertRaises(ValueError): bridge.start(client,cwd='/fixture',prompt='conflict',write=True)

    def test_never_created_close_fences_late_dispatch_and_sql_reopen(self):
        import sqlite3,json
        task=self.start(write=True)
        self.action('cancel',task)
        result=self.action('close',task)
        self.assertEqual(result['task']['state'],'closed')
        self.assertTrue(result['task']['release_epoch'])
        with self.store.connect() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('UPDATE tasks SET data=? WHERE id=?',
                           (json.dumps(dict(self.store.get(task['id']),state='queued')),task['id']))
        self.assertFalse(self.store.event(task['id'],'late','stage','no'))

if __name__ == '__main__': unittest.main()
