"""Synthetic single-policy release through the durable public interface."""
import concurrent.futures
import dataclasses
import json
from pathlib import Path
import sqlite3
import tempfile
from contextlib import closing
import unittest

from bridge import Origin, Store
from release import Release


class Backend:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def close(self, policy):
        self.calls += 1
        if self.fail:
            raise TimeoutError('synthetic uncertain close')
        binding = dict(acpxRecordId=policy['record'], acpxSessionId=policy['acp'],
                       session=policy['session'], agent=policy['agent'], cwd=policy['cwd'])
        return dict(verified=True, close=dict(action='session_closed', **binding),
                    status=dict(action='status_snapshot', status='no-session', binding=binding),
                    execution=dict(scope='acpx-managed-execution',
                                   source='supported-record-close-and-session-turn-lock'))


def fixture(home, backend=None):
    home.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(home / 'state.db')) as db, db:
        db.executescript('CREATE TABLE schema_version(version INTEGER); INSERT INTO schema_version VALUES(32);'
                         'CREATE TABLE sessions(id TEXT PRIMARY KEY,end_reason TEXT,ended_at REAL,source TEXT,profile_name TEXT);')
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', ('fixture-ended-owner', 'agent_close', 1234., 'subagent', 'default'))
    store = Store(home / 'plugin-data' / 'call-cli' / 'tasks.sqlite3')
    owner = Origin('default', 'fixture-ended-owner', 'fixture-ended-owner')
    task = store.create(owner, 'local', 'omp', str(home), 'SYNTHETIC ONLY', True)
    store.update(task['id'], state='completed', turn=4, runner_pid=0, launch_id='old-launch',
                 acpx_record_id='11111111-1111-4111-8111-111111111111', exit_code=0, stop_reason='end_turn')
    policy = dict(task=task['id'], owner=dataclasses.asdict(owner), turn=4,
                  record='11111111-1111-4111-8111-111111111111', acp='fixture-acp-session',
                  cwd=str(home), agent='omp', session=task['session'], submission='fixture-submission')
    path = home / 'plugin-data' / 'call-cli' / 'release-policy.json'
    path.write_text(json.dumps(policy)); path.chmod(0o600)
    backend = backend or Backend()
    return Release(home, policy, backend), owner, backend


class WorkflowTests(unittest.TestCase):
    def test_one_shot_release_preserves_owner_history_and_rejects_every_late_writer(self):
        with tempfile.TemporaryDirectory() as td:
            release, owner, backend = fixture(Path(td))
            store = release.store
            tid = release.policy['task']
            other = store.create(owner, 'local', 'omp', td + '/other', 'OTHER FIXTURE', False)
            before = store.get(tid)
            prepared = release.prepare()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                receipts = list(pool.map(lambda _: release.release(prepared['nonce']), range(2)))
            receipt = release.receipt()
            self.assertEqual((receipt['state'], backend.calls), ('released', 1))
            after = store.get(tid, owner)
            self.assertEqual({k:v for k,v in after.items() if k not in ('state','release_epoch')},
                             {k:v for k,v in before.items() if k != 'state'})
            self.assertEqual(after['state'], 'closed')
            self.assertEqual(store.get(other['id']), other)
            self.assertFalse(store.event(tid, 'late', 'question', 'Q1 Late?', turn=4))
            self.assertFalse(store.finish_turn(tid,4,'completed',0,'end_turn'))
            store.finish_launch(tid,4,'old-launch','late-process')
            for action in (lambda: store.update(tid,state='queued'), lambda: store.claim_launch(tid,4),
                           lambda: store.reply(tid,owner,'','resume'),
                           lambda: store.intervene(tid,owner,'resume','late'),
                           lambda: store.request_cancel(tid,owner), lambda: store.begin_close(tid,owner)):
                with self.assertRaises((ValueError, sqlite3.IntegrityError)):
                    action()
            self.assertEqual(store.get(tid,owner),after)

    def test_unknown_effect_keeps_reservation_and_retry_never_recloses(self):
        with tempfile.TemporaryDirectory() as td:
            release, owner, backend = fixture(Path(td), Backend(fail=True))
            nonce = release.prepare()['nonce']
            receipt = release.release(nonce)
            self.assertEqual((receipt['state'],receipt['phase']), ('unknown','hold'))
            recovered = Release(release.home,release.policy,backend)
            self.assertEqual(recovered.release(nonce), receipt)
            self.assertEqual(backend.calls,1)
            with self.assertRaises(ValueError):
                recovered.store.create(owner,'local','omp',td,'conflicting writer',True)

    def test_expiration_snapshot_aba_target_mismatch_and_forgery_have_zero_effects(self):
        for scenario in ('expired','aba','owner','turn','record','forged'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as td:
                release, owner, backend = fixture(Path(td))
                release.clock = lambda: 1000
                nonce = release.prepare()['nonce']
                before = release.store.get(release.policy['task'])
                if scenario == 'expired':release.clock = lambda:1120
                elif scenario == 'aba':
                    release.store.update(before['id'],state='running')
                    release.store.update(before['id'],state='completed')
                elif scenario == 'owner':release.policy = dict(release.policy,owner=dict(release.policy['owner'],session_id='other'))
                elif scenario == 'turn':release.policy = dict(release.policy,turn=5)
                elif scenario == 'record':release.policy = dict(release.policy,record='22222222-2222-4222-8222-222222222222')
                else:nonce='model-approved'
                with self.assertRaises(ValueError):release.release(nonce)
                self.assertEqual((backend.calls,release.receipt()),(0,None))
                self.assertEqual(release.store.get(before['id']),before)

    def test_crash_after_dispatch_preserves_unknown_and_never_replays(self):
        with tempfile.TemporaryDirectory() as td:
            release,owner,backend=fixture(Path(td))
            store=release.store;tid=release.policy['task']
            store.update(tid,commands={'done':{'status':'target_completed'},
                                      'pending':{'status':'pending'},'queued':{'status':'queued'}},
                         intervention={'status':'pending','command_id':'pending'})
            nonce=release.prepare()['nonce']
            def crash(policy):
                backend.calls+=1
                raise SystemExit('synthetic process crash after possible effect')
            backend.close=crash
            with self.assertRaises(SystemExit):release.release(nonce)
            recovered=Release(release.home,release.policy,backend)
            self.assertEqual(recovered.release(nonce)['state'],'unknown')
            self.assertEqual(backend.calls,1)
            task=store.get(tid,owner)
            self.assertEqual(task['commands']['done'],{'status':'target_completed'})
            self.assertEqual(task['commands']['pending']['status'],'release-revoked')
            self.assertEqual(task['commands']['queued']['status'],'release-revoked')
            self.assertEqual(task['intervention']['status'],'release-revoked')

    def test_malformed_close_or_unlinked_no_session_keeps_hold(self):
        for failure in ('wrong-record','substring','unlinked','missing-stop'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as td:
                release, owner, backend = fixture(Path(td))
                valid = backend.close
                def broken(policy):
                    proof = valid(policy)
                    if failure == 'wrong-record':proof['close']['acpxRecordId']='other'
                    elif failure == 'substring':proof['status']['status']='error mentions no-session'
                    elif failure == 'unlinked':proof['status']['binding']={}
                    else:proof['execution']={}
                    return proof
                backend.close = broken
                receipt=release.release(release.prepare()['nonce'])
                self.assertEqual((receipt['state'],receipt['phase']),('unknown','hold'))
                with self.assertRaises(ValueError):
                    release.store.create(owner,'local','omp',td,'conflict',True)

    def test_wrong_native_store_is_not_created_and_private_policy_is_required(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)
            with self.assertRaises(ValueError):Release(home,{},Backend())
            self.assertFalse((home/'plugin-data').exists())
            release,_,_=fixture(home)
            policy=home/'plugin-data/call-cli/release-policy.json'
            policy.chmod(0o644)
            with self.assertRaises(PermissionError):Release.load_policy(home)


if __name__ == '__main__':unittest.main()
