"""Synthetic release recovery: probes never repeat an execution close."""
from pathlib import Path
import tempfile
import unittest
from test_release_workflow import fixture, Backend


class ReconcileTests(unittest.TestCase):
    def test_closed_backend_settles_hold_without_repeating_close_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as td:
            release, owner, backend = fixture(Path(td), Backend(fail=True))
            hold = release.release(release.prepare()['nonce'])
            calls = backend.calls
            backend.probe = lambda policy: dict(verified=True,
                binding=dict(acpxRecordId=policy['record'], acpxSessionId=policy['acp'],
                    session=policy['session'], agent=policy['agent'], cwd=policy['cwd']),
                record=dict(closed=True, lastRequestId=policy['task']+':'+str(policy['turn'])),
                status='no-session', source='shared-runtime-exact-record-probe',
                arbitraryDetachedDescendantsProven=False)
            result = release.reconcile()
            self.assertEqual((result['state'], backend.calls), ('released', calls))
            self.assertEqual(release.store.get(release.policy['task'])['state'], 'closed')
            self.assertEqual(result['attempts'][0]['reason_code'], 'close_uncertain')
            self.assertEqual(result['attempts'][-1]['operation'], 'probe')
            self.assertFalse(release.store.event(release.policy['task'], 'late', 'stage', 'no'))

    def test_wrong_record_or_open_probe_retains_hold_and_failure_history(self):
        for wrong in ('binding', 'closed', 'request', 'status'):
            with self.subTest(wrong=wrong), tempfile.TemporaryDirectory() as td:
                release, _, backend = fixture(Path(td), Backend(fail=True))
                release.release(release.prepare()['nonce'])
                def probe(policy):
                    return dict(verified=True, binding={} if wrong == 'binding' else dict(
                        acpxRecordId=policy['record'], acpxSessionId=policy['acp'],session=policy['session'],
                        agent=policy['agent'],cwd=policy['cwd']),record=dict(closed=wrong!='closed',
                        lastRequestId='wrong' if wrong=='request' else policy['task']+':'+str(policy['turn'])),
                        status='open' if wrong=='status' else 'no-session',source='shared-runtime-exact-record-probe',
                        arbitraryDetachedDescendantsProven=False)
                backend.probe = probe
                receipt = release.reconcile()
                self.assertEqual((receipt['state'], receipt['phase'], backend.calls), ('unknown','hold',1))
                self.assertEqual(len(receipt['attempts']), 2)
                with self.assertRaises(ValueError):
                    release.store.create(__import__('bridge').Origin(**release.policy['owner']),
                        'local','omp',td,'competing writer',True)


    def test_concurrent_probe_result_is_not_overwritten_by_close_return(self):
        with tempfile.TemporaryDirectory() as td:
            release, _, backend = fixture(Path(td))
            def probe(policy):
                return dict(verified=True,binding=dict(acpxRecordId=policy['record'],acpxSessionId=policy['acp'],
                    session=policy['session'],agent=policy['agent'],cwd=policy['cwd']),
                    record=dict(closed=True,lastRequestId=policy['task']+':'+str(policy['turn'])),
                    status='no-session',source='shared-runtime-exact-record-probe',arbitraryDetachedDescendantsProven=False)
            backend.probe = probe
            def close(policy):
                backend.calls += 1
                release.reconcile()
                raise TimeoutError('response lost after concurrent reconcile')
            backend.close = close
            result = release.release(release.prepare()['nonce'])
            self.assertEqual((result['state'], release.store.get(release.policy['task'])['state']), ('released','closed'))
            self.assertEqual(len(result['attempts']), 2)

    def test_probe_failure_keeps_sanitized_stage_without_exception_body(self):
        from release import BackendFailure
        with tempfile.TemporaryDirectory() as td:
            release, _, backend = fixture(Path(td), Backend(fail=True))
            release.release(release.prepare()['nonce'])
            def probe(policy): raise BackendFailure('probe','record_binding_mismatch')
            backend.probe = probe
            receipt = release.reconcile()
            self.assertEqual((receipt['state'],receipt['stage'],receipt['reason_code']),
                             ('unknown','probe','record_binding_mismatch'))
            self.assertEqual(receipt['attempts'][0]['reason_code'],'close_uncertain')

    def test_probe_does_not_overwrite_first_failure_and_keeps_latest_probe_separate(self):
        from release import BackendFailure
        with tempfile.TemporaryDirectory() as td:
            release,_,backend=fixture(Path(td),Backend(fail=True))
            release.release(release.prepare()['nonce'])
            def probe(policy): raise BackendFailure('load','backend_unavailable',dict(substep='load_runtime'))
            backend.probe=probe
            receipt=release.reconcile()
            self.assertEqual(receipt['first_failure']['reason_code'],'close_uncertain')
            self.assertEqual(receipt['latest_probe']['stage'],'load')
            self.assertEqual(receipt['latest_probe']['substep'],'load_runtime')
            self.assertEqual(backend.calls,1)

    def test_release_and_probe_consume_only_typed_safe_diagnostic_fields(self):
        from release import BackendFailure, recover_native
        import json
        payload=dict(operation='close',substep='status_after',error_name='Error',returncode=2,
            message='PRIVATE_SENTINEL',stack='PRIVATE_SENTINEL',raw_stderr='PRIVATE_SENTINEL',
            state='released',phase='verified',stderr_length='PRIVATE_SENTINEL',timeout='yes',
            record_id_match=True,closed_observed=False,name_lookup_found=False,name_scope_match='PRIVATE_SENTINEL')
        with tempfile.TemporaryDirectory() as td:
            release,owner,backend=fixture(Path(td))
            def fail(policy): raise BackendFailure('verify','backend_unavailable',payload)
            backend.close=fail; backend.probe=fail
            receipt=release.release(release.prepare()['nonce'])
            self.assertNotIn('PRIVATE_SENTINEL',json.dumps(receipt))
            receipt=release.reconcile()
            self.assertNotIn('PRIVATE_SENTINEL',json.dumps(receipt))
            self.assertNotIn('timeout',receipt['latest_probe'])
            self.assertEqual(receipt['state'],'unknown')
            self.assertIs(receipt['latest_probe']['closed_observed'],False)
            self.assertIs(receipt['latest_probe']['name_lookup_found'],False)
            self.assertNotIn('name_scope_match',receipt['latest_probe'])
        from bridge import Origin,Store
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td)/'tasks.db'); owner=Origin('fixture','owner','owner')
            task=store.create(owner,'local','omp',td,'owned',False)
            store.update(task['id'],state='completed',acpx_record_id='record')
            class Backend:
                def resolve(self,policy):return dict(acp='acp')
                close=staticmethod(fail); probe=staticmethod(fail)
            receipt=recover_native(store,owner,task['id'],1,'close',Backend())
            self.assertNotIn('PRIVATE_SENTINEL',json.dumps(receipt))
            self.assertEqual(receipt['state'],'unknown')


if __name__ == '__main__': unittest.main()
