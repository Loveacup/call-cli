"""Slice D verification: ACPX + OAR local AGY coexistence using real @botiverse/oar modules.

Zero-model tests use:
1. Real @botiverse/oar/testing (`scriptedRuntime`) + @botiverse/oar/observe.
2. Real @botiverse/oar/runtimes/antigravity + @botiverse/oar/observe paired with
   the explicitly synthetic ACP stdio double `fixtures/oar_agy_agent.py`.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from test_dispatch import run_dispatched

REPO_ROOT = Path(__file__).resolve().parent
OAR_TEST_DIST = os.environ.get('CALL_CLI_OAR_TEST_DIST')
OAR_STAGE_DIST = Path(OAR_TEST_DIST).resolve() if OAR_TEST_DIST else REPO_ROOT / '.oar-stage' / 'oar-pkg'
FIXTURE_AGENT = REPO_ROOT / 'fixtures' / 'oar_agy_agent.py'


@contextmanager
def patched_env(**updates):
    old = {k: os.environ.get(k) for k in updates}
    try:
        for k, v in updates.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, prev in old.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


def wait_until(predicate, timeout=6.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = predicate()
        if val:
            return val
        time.sleep(interval)
    raise AssertionError('Timed out waiting for condition')


class OarCoexistTests(unittest.TestCase):
    def setUp(self):
        if self._testMethodName.startswith('test_real_oar_') and not (OAR_STAGE_DIST / 'dist' / 'observe' / 'index.js').is_file():
            if OAR_TEST_DIST:
                self.fail(f'Missing OAR test package at {OAR_STAGE_DIST}')
            self.skipTest('Optional real OAR package not staged; set CALL_CLI_OAR_TEST_DIST to run this integration test')

    def test_defaults_and_legacy_tasks_unchanged(self):
        from backends import BACKENDS, backend_for, prepare_start
        from collab import Collaboration

        self.assertEqual(tuple(BACKENDS.keys()), ('omp', 'codex', 'pi', 'agy'))
        self.assertNotIn('oar', BACKENDS)
        self.assertEqual(prepare_start('codex'), {'execution_policy': 'native-skip'})
        self.assertEqual(prepare_start('agy'), {
            'agy_approval': 'skip-permissions',
            'model': 'gemini-pro-orchestrator',
            'execution_policy': 'native-skip',
            'agy_swarm': 'native',
            'agy_mode': 'swarm',
        })
        self.assertEqual(prepare_start('agy', 'oa'), {
            'agy_approval': 'skip-permissions',
            'model': 'gemini-pro-orchestrator-argon',
            'execution_policy': 'native-skip',
            'agy_swarm': 'off',
        })
        self.assertIs(backend_for('agy'), BACKENDS['agy'])

        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root) / 'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']
            default_task = c.start(client, agent='agy', cwd=root, prompt='default unchanged')
            self.assertNotIn('backend', default_task)
            self.assertEqual(default_task['agy_swarm'], 'native')
            legacy = c.store.create(c.origin(client), 'local', 'agy', root, 'legacy', False)
            self.assertNotIn('backend', legacy)
            c.store.update(legacy['id'], state='unknown', acpx_record_id='11111111-1111-4111-8111-111111111111')
            self.assertIn('close', c.status(client, legacy['id'])['recovery']['next_action'])

    def test_oar_selection_constraints_and_cli_flag(self):
        from backends import ModelSelectionError, prepare_start
        from collab import Collaboration, main

        fields = prepare_start('agy', 'oc', 'low', backend='oar')
        self.assertEqual(fields, {
            'agy_approval': 'skip-permissions',
            'model': 'gemini-pro-orchestrator',
            'effort': 'low',
            'execution_policy': 'native-skip',
            'backend': 'oar',
            'agy_swarm': 'off',
            'acpx_record_id': None,
        })
        self.assertEqual(prepare_start('agy', 'gemini-4-carbon', agy_mode='single', backend='oar')['agy_mode'], 'single')

        for agent, kw in [
            ('codex', {'backend': 'oar'}),
            ('omp', {'backend': 'oar'}),
            ('agy', {'backend': 'unknown'}),
            ('agy', {'backend': 'oar', 'agy_swarm': 'native'}),
            ('agy', {'backend': 'oar', 'agy_mode': 'swarm'}),
            ('agy', {'backend': 'oar', 'agy_mode': 'teamwork'}),
            ('agy', {'backend': 'oar', 'agy_mode': 'dynamic'}),
            ('agy', {'backend': 'oar', 'agy_worker_model': 'gemini-4-carbon'}),
            ('agy', {'backend': 'oar', 'model': 'ocs'}),
            ('agy', {'backend': 'oar', 'model': 'oct'}),
            ('agy', {'backend': 'oar', 'agy_approval': 'approve-all'}),
            ('agy', {'backend': 'oar', 'agy_approval': 'approve-reads'}),
        ]:
            with self.subTest(agent=agent, kw=kw):
                with self.assertRaises((ValueError, ModelSelectionError)):
                    prepare_start(agent, **kw)

        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root) / 'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']
            with self.assertRaises(ValueError):
                c.start(client, agent='agy', device='remote-box', cwd='/work', prompt='nope', backend='oar')

            db_path = Path(root) / 'cli.db'
            out = io.StringIO()
            with patch('sys.stdout', out):
                rc = main(['--db', str(db_path), 'connect', '--runtime', 'omp'])
            self.assertEqual(rc, 0)
            cli_client = json.loads(out.getvalue())['client_id']

            with patch.object(Collaboration, '_launch', lambda self, task: None):
                out_oar = io.StringIO()
                with patch('sys.stdout', out_oar):
                    rc = main([
                        '--db', str(db_path), '--client', cli_client, 'start',
                        '--agent', 'agy', '--backend', 'oar', '--model', 'oc',
                        '--cwd', root, '--prompt', 'cli oar',
                    ])
                self.assertEqual(rc, 0)
                started_oar = json.loads(out_oar.getvalue())
                self.assertEqual(started_oar['backend'], 'oar')
                self.assertEqual(started_oar['agy_swarm'], 'off')

                out_default = io.StringIO()
                with patch('sys.stdout', out_default):
                    rc = main([
                        '--db', str(db_path), '--client', cli_client, 'start',
                        '--agent', 'agy', '--cwd', root, '--prompt', 'cli default',
                    ])
                self.assertEqual(rc, 0)
                started_default = json.loads(out_default.getvalue())
                self.assertNotIn('backend', started_default)
                self.assertEqual(started_default['agy_swarm'], 'native')

    def test_backend_immutability_and_no_cross_backend_control(self):
        from agy import AgyBackend
        from collab import Collaboration
        from runner import control as runner_control

        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root) / 'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']
            oar_task = c.start(client, agent='agy', cwd=root, prompt='immutable', backend='oar')
            with self.assertRaises(ValueError):
                c.store.update(oar_task['id'], backend='acpx')
            with self.assertRaises(ValueError):
                c.store.update(oar_task['id'], backend=None)

            legacy = c.start(client, agent='agy', cwd=root, prompt='legacy')
            with self.assertRaises(ValueError):
                c.store.update(legacy['id'], backend='oar')

            with self.assertRaises(ValueError):
                AgyBackend().control(oar_task, 'status')
            with self.assertRaises(ValueError):
                runner_control(oar_task, 'close')

    def test_oar_missing_dist_fails_closed_without_fallback(self):
        from acpx_backend import AcpxBackend
        from collab import Collaboration

        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root) / 'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']
            task = c.start(client, agent='agy', cwd=root, prompt='missing dist', backend='oar', write=True)

            for bad_dist in (None, str(Path(root) / 'does-not-exist')):
                with self.subTest(bad_dist=bad_dist):
                    if bad_dist is not None:
                        c.store.update(task['id'], state='queued', turn=2, launch_turn=None, launch_id=None)
                    with patched_env(CALL_CLI_OAR_DIST=bad_dist), \
                         patch.object(AcpxBackend, 'run', side_effect=AssertionError('ACPX fallback forbidden')), \
                         patch('agy_native.run_native', side_effect=AssertionError('agy_native fallback forbidden')):
                        rc = run_dispatched(c.store.path, task['id'])
                    self.assertEqual(rc, 1)
                    st = c.status(client, task['id'])
                    self.assertEqual(st['state'], 'unknown')
                    self.assertIn('CALL_CLI_OAR_DIST', st['settlement_note'])

    def test_real_oar_scripted_runtime_end_to_end(self):
        from collab import Collaboration

        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root) / 'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']
            task = c.start(client, agent='agy', cwd=root, prompt='scripted flow', backend='oar', write=True)
            tid = task['id']
            c.store.update(tid, oar_script={'mode': 'steer_wait', 'waitMs': 3000})

            with patched_env(CALL_CLI_OAR_DIST=str(OAR_STAGE_DIST), CALL_CLI_OAR_TEST_DRIVER='scripted'):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    fut = pool.submit(run_dispatched, c.store.path, tid)
                    wait_until(lambda: c.store.get(tid).get('native_input', {}).get('accepting') is True)
                    c.dialogue(client, tid, 'please steer scripted turn', expected_turn=1, command_id='steer-scripted-1')
                    rc = fut.result(timeout=10)

            self.assertEqual(rc, 0)
            st = c.status(client, tid)
            self.assertEqual(st['state'], 'completed')
            self.assertEqual(st['oar_session']['driver'], 'scripted')
            self.assertEqual(st['oar_session']['oar_version'], '0.121.0')
            self.assertTrue(st['oar_cleanup']['session_closed'])
            self.assertTrue(st['oar_cleanup']['process_exited'])
            self.assertIs(st['oar_cleanup']['arbitraryDetachedDescendantsProven'], False)
            audit = st['oar_steer_audit']['steer-scripted-1']
            self.assertTrue(audit['accepted'])
            self.assertTrue(audit['dispatched'])
            self.assertIsNone(audit['adopted'])

            events = c.events(client, tid)
            kinds = [e['kind'] for e in events]
            self.assertIn('stage', kinds)
            self.assertTrue(any('Steered output: please steer scripted turn' in e['text'] for e in events if e['kind'] == 'answer'))

            with patch('release.NativeBackend', side_effect=AssertionError('ACPX close must never run for OAR')):
                closed = c.close(client, tid)
            self.assertEqual(closed['state'], 'closed')
            self.assertEqual(closed['close_receipt']['state'], 'released')
            self.assertEqual(closed['close_receipt']['phase'], 'verified')
            self.assertIs(closed['close_receipt']['external_close_attempted'], False)
            self.assertIs(closed['close_receipt']['arbitraryDetachedDescendantsProven'], False)
            with c.store.ro_connect() as db:
                fence_receipt = json.loads(db.execute('SELECT receipt FROM release_fences WHERE task=?', (tid,)).fetchone()[0])
            self.assertEqual(fence_receipt['source'], 'oar-single-owner-cleanup')

            other = c.connect('pi')['client_id']
            next_task = c.start(other, agent='codex', cwd=root, prompt='lock released', write=True)
            self.assertEqual(next_task['state'], 'queued')

    def test_real_oar_antigravity_full_lifecycle_and_close_without_acpx(self):
        from collab import Collaboration

        with tempfile.TemporaryDirectory() as root:
            state_file = Path(root) / 'fixture-state.json'
            c = Collaboration(Path(root) / 'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']
            other = c.connect('pi')['client_id']
            task = c.start(
                client, agent='agy', cwd=root, prompt='NORMAL lifecycle test',
                model='gemini-4-carbon', effort='high', backend='oar', write=True,
            )
            tid = task['id']

            with self.assertRaises(ValueError):
                c.start(other, agent='agy', cwd=root, prompt='writer lock conflict', write=True)

            with patched_env(
                CALL_CLI_OAR_DIST=str(OAR_STAGE_DIST),
                CALL_CLI_OAR_TEST_DRIVER='antigravity',
                OAR_ANTIGRAVITY_BIN=str(FIXTURE_AGENT),
                OAR_FIXTURE_STATE_FILE=str(state_file),
            ):
                rc = run_dispatched(c.store.path, tid)

            self.assertEqual(rc, 0)
            st = c.status(client, tid)
            self.assertEqual(st['state'], 'completed')
            self.assertEqual(st['oar_session']['driver'], 'antigravity')
            self.assertEqual(st['oar_session']['oar_version'], '0.121.0')
            self.assertEqual(st['oar_session']['model'], 'gemini-4-carbon')
            self.assertEqual(st['oar_session']['effort'], 'high')
            self.assertTrue(st['oar_cleanup']['session_closed'])
            self.assertTrue(st['oar_cleanup']['process_exited'])

            events = c.events(client, tid)
            self.assertTrue(any(e['kind'] == 'tool' and 'read_file' in e['text'] for e in events))
            self.assertTrue(any(
                e['kind'] == 'answer' and 'model=gemini-4-carbon effort=high append=True open=session/new' in e['text']
                for e in events
            ))

            fixture_state = json.loads(state_file.read_text())
            self.assertIn(st['oar_session']['session_id'], fixture_state['closed'])

            with patch('release.NativeBackend', side_effect=AssertionError('ACPX close must never run for OAR')):
                closed = c.close(client, tid)
            self.assertEqual(closed['state'], 'closed')
            self.assertEqual(closed['close_receipt']['state'], 'released')
            self.assertIs(closed['close_receipt']['external_close_attempted'], False)
            self.assertIs(closed['close_receipt']['arbitraryDetachedDescendantsProven'], False)
            with c.store.ro_connect() as db:
                fence_receipt = json.loads(db.execute('SELECT receipt FROM release_fences WHERE task=?', (tid,)).fetchone()[0])
            self.assertEqual(fence_receipt['source'], 'oar-single-owner-cleanup')

    def test_real_oar_antigravity_mid_turn_steer_and_latest_outcome(self):
        from collab import Collaboration

        with tempfile.TemporaryDirectory() as root:
            state_file = Path(root) / 'fixture-state.json'
            c = Collaboration(Path(root) / 'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']
            task = c.start(client, agent='agy', cwd=root, prompt='STEER_WAIT initial', backend='oar', write=True)
            tid = task['id']

            with patched_env(
                CALL_CLI_OAR_DIST=str(OAR_STAGE_DIST),
                CALL_CLI_OAR_TEST_DRIVER='antigravity',
                OAR_ANTIGRAVITY_BIN=str(FIXTURE_AGENT),
                OAR_FIXTURE_STATE_FILE=str(state_file),
            ):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    fut = pool.submit(run_dispatched, c.store.path, tid)
                    wait_until(lambda: c.store.get(tid).get('native_input', {}).get('accepting') is True)
                    admitted = c.dialogue(client, tid, 'change course to X', expected_turn=1, command_id='steer-agy-1')
                    self.assertEqual(admitted['commands']['steer-agy-1']['status'], 'accepted')
                    rc = fut.result(timeout=10)

            self.assertEqual(rc, 0)
            st = c.status(client, tid)
            self.assertEqual(st['state'], 'completed')
            self.assertEqual(st['commands']['steer-agy-1']['status'], 'returned')
            audit = st['oar_steer_audit']['steer-agy-1']
            self.assertEqual(audit['command_id'], 'steer-agy-1')
            self.assertTrue(audit['accepted'])
            self.assertTrue(audit['dispatched'])
            self.assertTrue(audit['request_id'])
            self.assertNotEqual(audit['request_id'], audit['command_id'])
            self.assertTrue(audit['runtime_observed'])
            self.assertIsNone(audit['adopted'])

            answers = [e['text'] for e in c.events(client, tid) if e['kind'] == 'answer']
            self.assertTrue(any('Steered answer: change course to X' in a for a in answers))

    def test_real_oar_antigravity_outside_turn_activity_not_misreported_as_completion(self):
        from collab import Collaboration

        with tempfile.TemporaryDirectory() as root:
            state_file = Path(root) / 'fixture-state.json'
            c = Collaboration(Path(root) / 'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']

            # Case 1: failed tool + no in-turn answer, followed by late post-end_turn activity.
            # Outside-turn activity must stay outside_turn_activity and never make the turn completed.
            fail_task = c.start(client, agent='agy', cwd=root, prompt='OUTSIDE_TURN_FAIL probe', backend='oar')
            with patched_env(
                CALL_CLI_OAR_DIST=str(OAR_STAGE_DIST),
                CALL_CLI_OAR_TEST_DRIVER='antigravity',
                OAR_ANTIGRAVITY_BIN=str(FIXTURE_AGENT),
                OAR_FIXTURE_STATE_FILE=str(state_file),
                CALL_CLI_OAR_POST_TURN_GRACE_MS='150',
            ):
                rc1 = run_dispatched(c.store.path, fail_task['id'])

            self.assertEqual(rc1, 1)
            st1 = c.status(client, fail_task['id'])
            self.assertEqual(st1['state'], 'unknown')
            self.assertGreaterEqual(st1.get('oar_outside_turn_activity', 0), 1)
            ev1 = c.events(client, fail_task['id'])
            self.assertTrue(any(e['kind'] == 'outside_turn_activity' for e in ev1))
            self.assertFalse(any(e['kind'] == 'answer' and 'late background output' in e['text'] for e in ev1))

            # Case 2: normal in-turn answer + late post-end_turn activity.
            ok_task = c.start(client, agent='agy', cwd=root, prompt='OUTSIDE_TURN probe', backend='oar')
            with patched_env(
                CALL_CLI_OAR_DIST=str(OAR_STAGE_DIST),
                CALL_CLI_OAR_TEST_DRIVER='antigravity',
                OAR_ANTIGRAVITY_BIN=str(FIXTURE_AGENT),
                OAR_FIXTURE_STATE_FILE=str(state_file),
                CALL_CLI_OAR_POST_TURN_GRACE_MS='150',
            ):
                rc2 = run_dispatched(c.store.path, ok_task['id'])

            self.assertEqual(rc2, 0)
            st2 = c.status(client, ok_task['id'])
            self.assertEqual(st2['state'], 'completed')
            self.assertGreaterEqual(st2.get('oar_outside_turn_activity', 0), 1)
            ev2 = c.events(client, ok_task['id'])
            self.assertTrue(any(e['kind'] == 'outside_turn_activity' for e in ev2))
            self.assertFalse(any(e['kind'] == 'answer' and 'late background output' in e['text'] for e in ev2))

            # Case 3: zero post-turn grace window forces late activity to arrive during dispose;
            # bridge/Python shutdown must preserve outside_turn_activity instead of dropping it.
            dispose_fail_task = c.start(client, agent='agy', cwd=root, prompt='OUTSIDE_TURN_FAIL dispose probe', backend='oar')
            with patched_env(
                CALL_CLI_OAR_DIST=str(OAR_STAGE_DIST),
                CALL_CLI_OAR_TEST_DRIVER='antigravity',
                OAR_ANTIGRAVITY_BIN=str(FIXTURE_AGENT),
                OAR_FIXTURE_STATE_FILE=str(state_file),
                CALL_CLI_OAR_POST_TURN_GRACE_MS='0',
            ):
                rc3 = run_dispatched(c.store.path, dispose_fail_task['id'])

            self.assertEqual(rc3, 1)
            st3 = c.status(client, dispose_fail_task['id'])
            self.assertEqual(st3['state'], 'unknown')
            self.assertGreaterEqual(st3.get('oar_outside_turn_activity', 0), 1)
            ev3 = c.events(client, dispose_fail_task['id'])
            self.assertTrue(any(e['kind'] == 'outside_turn_activity' for e in ev3))
            self.assertFalse(any(e['kind'] == 'answer' and 'late background output' in e['text'] for e in ev3))

    def test_real_oar_antigravity_resume_checks_cwd_and_reapplies_config(self):
        from collab import Collaboration

        with tempfile.TemporaryDirectory() as root:
            state_file = Path(root) / 'fixture-state.json'
            c = Collaboration(Path(root) / 'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']
            task = c.start(
                client, agent='agy', cwd=root, prompt='ASK_QUESTION turn1',
                model='gemini-4-carbon', effort='high', backend='oar', write=True,
            )
            tid = task['id']
            env_vars = dict(
                CALL_CLI_OAR_DIST=str(OAR_STAGE_DIST),
                CALL_CLI_OAR_TEST_DRIVER='antigravity',
                OAR_ANTIGRAVITY_BIN=str(FIXTURE_AGENT),
                OAR_FIXTURE_STATE_FILE=str(state_file),
            )
            with patched_env(**env_vars):
                rc1 = run_dispatched(c.store.path, tid)
            self.assertEqual(rc1, 0)
            st1 = c.status(client, tid)
            self.assertEqual(st1['state'], 'waiting_reply')
            sid = st1['oar_session']['session_id']
            q = next(e for e in c.events(client, tid) if e['kind'] == 'question')

            c.reply(client, tid, q['token'], 'NORMAL turn2 answer', expected_turn=1, command_id='r2')
            with patched_env(**env_vars):
                rc2 = run_dispatched(c.store.path, tid)
            self.assertEqual(rc2, 0)
            st2 = c.status(client, tid)
            self.assertEqual(st2['state'], 'completed')
            self.assertEqual(st2['turn'], 2)
            self.assertEqual(st2['oar_session']['session_id'], sid)
            self.assertEqual(st2['oar_session']['open_method'], 'session/resume')
            self.assertEqual(st2['oar_session']['model'], 'gemini-4-carbon')
            self.assertEqual(st2['oar_session']['effort'], 'high')
            ev2 = [e['text'] for e in c.events(client, tid) if e['kind'] == 'answer' and e.get('turn') == 2]
            self.assertTrue(any('model=gemini-4-carbon effort=high append=True open=session/resume' in t for t in ev2))

            reqs = json.loads(state_file.read_text())['requests']
            methods = [r['method'] for r in reqs]
            self.assertIn('session/list', methods)
            self.assertIn('session/resume', methods)

            # CWD mismatch before resume must fail closed without replay
            c.intervene(client, tid, 'should fail cwd', command_id='iv-bad-cwd', expected_turn=2)
            c.store.update(tid, cwd=str(Path(root) / 'other-dir'))
            with patched_env(**env_vars):
                rc3 = run_dispatched(c.store.path, tid)
            self.assertEqual(rc3, 1)
            self.assertEqual(c.status(client, tid)['state'], 'unknown')
            self.assertIn('cwd mismatch', c.status(client, tid)['settlement_note'])

    def test_real_oar_antigravity_cancel_withdraws_accepted_dialogue_and_unproven_cleanup_holds(self):
        from collab import Collaboration

        with tempfile.TemporaryDirectory() as root:
            state_file = Path(root) / 'fixture-state.json'
            c = Collaboration(Path(root) / 'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']
            other = c.connect('pi')['client_id']
            env_vars = dict(
                CALL_CLI_OAR_DIST=str(OAR_STAGE_DIST),
                CALL_CLI_OAR_TEST_DRIVER='antigravity',
                OAR_ANTIGRAVITY_BIN=str(FIXTURE_AGENT),
                OAR_FIXTURE_STATE_FILE=str(state_file),
            )

            # 1. Clean cancellation withdraws un-dispatched dialogue and releases on close without ACPX
            t_cancel = c.start(client, agent='agy', cwd=root, prompt='CANCEL_WAIT abort me', backend='oar', write=True)
            tid_c = t_cancel['id']
            with patched_env(**env_vars):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    fut = pool.submit(run_dispatched, c.store.path, tid_c)
                    wait_until(lambda: c.store.get(tid_c).get('native_input', {}).get('accepting') is True)
                    c.dialogue(client, tid_c, 'queued steer', expected_turn=1, command_id='d-queued')
                    c.cancel(client, tid_c)
                    rc_c = fut.result(timeout=10)
            self.assertEqual(rc_c, 0)
            st_c = c.status(client, tid_c)
            self.assertEqual(st_c['state'], 'cancelled')
            self.assertIn(st_c['commands']['d-queued']['status'], {'not_sent', 'cancelled'})
            self.assertTrue(st_c['oar_cleanup']['session_closed'])
            with patch('release.NativeBackend', side_effect=AssertionError('ACPX close forbidden')):
                self.assertEqual(c.close(client, tid_c)['state'], 'closed')

            # Deterministic check: accepted dialogue sealed on cancel becomes not_sent and never dispatches
            t_det = c.start(client, agent='agy', cwd=root, prompt='CANCEL_WAIT det', backend='oar')
            tid_d = t_det['id']
            c.store.update(tid_d, state='running', runner_pid=os.getpid())
            c.store.native_input_open(tid_d, 1, t_det['launch_id'], 1)
            c.dialogue(client, tid_d, 'must not send', expected_turn=1, command_id='d-unclaimed')
            c.cancel(client, tid_d)
            self.assertIsNone(c.store.native_dialogue_claim(tid_d, 1, t_det['launch_id'], 1))
            c.store.native_input_end(tid_d, 1, t_det['launch_id'], 1)
            self.assertEqual(c.status(client, tid_d)['commands']['d-unclaimed']['status'], 'not_sent')

            # 2. Unproven cleanup (mid-turn crash) stays HOLD / unknown, never invokes ACPX,
            #    and requires explicit release_only(..., abandon=True)
            t_crash = c.start(client, agent='agy', cwd=root, prompt='CRASH_MID_TURN boom', backend='oar', write=True)
            tid_x = t_crash['id']
            with patched_env(**env_vars):
                rc_x = run_dispatched(c.store.path, tid_x)
            self.assertEqual(rc_x, 1)
            st_x = c.status(client, tid_x)
            self.assertEqual(st_x['state'], 'unknown')
            self.assertNotIn('oar_cleanup', st_x)
            self.assertIn('OAR', st_x['recovery']['next_action'])
            self.assertIn('never ACPX', st_x['recovery']['next_action'])
            self.assertFalse(st_x['recovery']['capabilities']['exact_record_reconcile'])

            with patch('release.NativeBackend', side_effect=AssertionError('ACPX close forbidden')):
                close_res = c.close(client, tid_x)
            self.assertEqual(close_res['state'], 'unknown')
            self.assertEqual(close_res['close_receipt']['reason_code'], 'oar_cleanup_unproven')
            with self.assertRaises(ValueError):
                c.start(other, agent='codex', cwd=root, prompt='still locked', write=True)

            with patch('release.NativeBackend', side_effect=AssertionError('ACPX release_only forbidden')):
                hold_rel = c.release_only(client, tid_x, expected_turn=1, abandon=False)
            self.assertEqual(hold_rel['close_receipt']['reason_code'], 'oar_cleanup_unproven')

            with patch('release.NativeBackend', side_effect=AssertionError('ACPX abandon forbidden')):
                abandoned = c.release_only(client, tid_x, expected_turn=1, abandon=True)
            self.assertEqual(abandoned['close_receipt']['state'], 'released')
            self.assertEqual(abandoned['close_receipt']['business_state'], 'unknown')
            self.assertEqual(c.recovery_status(client, tid_x)['resource_recovery']['resource_state'], 'released')
            self.assertEqual(c.start(other, agent='codex', cwd=root, prompt='now unlocked', write=True)['state'], 'queued')


if __name__ == '__main__':
    unittest.main()
