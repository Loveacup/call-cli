"""Offline diagnostics through the public collaboration API and real runner."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from collab import Collaboration
from runner import run_task


CANARY = 'SYNTHETIC_PRIVATE_PROMPT_PATH_CREDENTIAL_STACK'


class SafeDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.home = self.root / 'home'
        self.home.mkdir()
        package = self.root / 'node_modules' / 'acpx'
        package.mkdir(parents=True)
        (package / 'package.json').write_text(json.dumps(dict(name='acpx', version='0.22.0',
            exports={'./package.json':'./package.json', './runtime':'./runtime.cjs'})))
        shutil.copyfile(Path(__file__).with_name('fixtures') / 'diagnostic_runtime.cjs', package / 'runtime.cjs')
        script = '#!' + sys.executable + '\n' + r'''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['CALLCLI_DIAGNOSTIC_ROOT'])
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
with (root / 'calls.jsonl').open('a') as out:
    out.write(json.dumps([name, *args]) + '\n')
if name == 'npm':
    print(json.dumps('0.22.0') if args[0] == 'view' else root / 'node_modules')
elif args == ['--version']:
    print('0.22.0')
elif 'new' in args:
    print(os.environ['CALLCLI_DIAGNOSTIC_CANARY'], file=sys.stderr)
    if os.environ['CALLCLI_DIAGNOSTIC_MODE'] == 'create':
        raise SystemExit(1)
    print(json.dumps({'acpxRecordId':'11111111-1111-4111-8111-111111111111'}))
else:
    raise SystemExit('Unexpected synthetic control operation')
'''
        for name in ('acpx', 'npm'):
            file = self.bin / name
            file.write_text(script)
            file.chmod(0o700)
        os.symlink(shutil.which('node'), self.bin / 'node')
        self.output = io.StringIO()
        self.exits = []
        def launch(task):
            with contextlib.redirect_stdout(self.output):
                self.exits.append(run_task(self.root / 'tasks.sqlite3', task['id'],
                    turn=task['turn'], launch_id=task['launch_id'], epoch=task['control_epoch']))
        self.c = Collaboration(self.root / 'tasks.sqlite3', launch=launch)
        self.client = self.c.connect('pi')['client_id']
        environment = dict(PATH=str(self.bin) + os.pathsep + os.environ['PATH'], HOME=str(self.home),
            CALLCLI_DIAGNOSTIC_ROOT=str(self.root), CALLCLI_DIAGNOSTIC_CANARY=CANARY,
            CALLCLI_DIAGNOSTIC_MODE='create')
        self.env = patch.dict(os.environ, environment)
        self.env.start()
        self.addCleanup(self.env.stop)
        locks = patch('updates.DEFAULT_LOCK_ROOT', self.root / 'locks')
        locks.start()
        self.addCleanup(locks.stop)

    def start(self, mode, agent='omp'):
        os.environ['CALLCLI_DIAGNOSTIC_MODE'] = mode
        return self.c.start(self.client, agent=agent, prompt=CANARY, cwd=str(self.root), write=True)

    def assert_unknown(self, task, stage, reason, prompts, creations=1):
        status = self.c.status(self.client, task['id'])
        events = self.c.events(self.client, task['id'])
        self.assertEqual((status['state'], self.exits[-1], events[-1]['kind']), ('unknown', 1, 'unknown'))
        diagnostics = [e for e in events if e['kind'] == 'diagnostic']
        if stage == 'runner':
            self.assertIn('stage=runner reason=backend_failed', events[-1]['text'])
        else:
            self.assertIn((stage, reason), [(e.get('stage'), e.get('reason_code')) for e in diagnostics])
        self.assertIn(reason, events[-1]['text'])
        self.assertNotIn(CANARY, json.dumps(status) + json.dumps(events) + self.output.getvalue())
        with self.assertRaises(ValueError):
            self.c.start(self.client, prompt='conflicting writer', cwd=str(self.root), write=True)
        trace = self.root / 'runtime.jsonl'
        observed = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        self.assertEqual(observed.count('prompt'), prompts)
        calls_file = self.root / 'calls.jsonl'
        calls = [json.loads(line) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
        self.assertEqual(sum('new' in call for call in calls), creations)
        self.assertFalse(any('cancel' in call or 'close' in call for call in calls))
        return diagnostics

    def test_creation_failure_is_distinct_and_keeps_uncertain_session_reserved(self):
        task = self.start('create')
        self.assert_unknown(task, 'session_create', 'creation_failed', 0)
        self.assertTrue(task['session_creation_attempted'])
        self.assertFalse(task['acpx_created'])
        self.assertNotIn('acpx_record_id', task)


    def test_runtime_load_failure_is_safe_and_not_a_creation_failure(self):
        task = self.start('load')
        self.assert_unknown(task, 'runtime_load', 'runtime_load_failed', 0)

    def test_auth_configuration_failure_does_not_claim_provider_auth_rejection(self):
        (self.home / '.acpx').mkdir()
        (self.home / '.acpx' / 'config.json').write_text(CANARY)
        task = self.start('auth')
        self.assert_unknown(task, 'auth_load', 'auth_load_failed', 0)

    def test_runtime_creation_failure_is_distinct_from_session_creation(self):
        self.assert_unknown(self.start('runtime'), 'runtime_create', 'runtime_create_failed', 0)

    def test_start_failure_reports_submission_uncertainty_without_replay(self):
        self.assert_unknown(self.start('start'), 'prompt_submit', 'prompt_submit_failed', 1)

    def test_event_observer_failure_keeps_observed_acceptance_separate_from_success(self):
        diagnostics = self.assert_unknown(self.start('events'), 'prompt_events', 'prompt_events_failed', 1)
        self.assertIn(('prompt_execution', 'acceptance_observed'),
                      [(e.get('stage'), e.get('reason_code')) for e in diagnostics])

    def test_result_rejection_is_a_response_failure_not_an_execution_verdict(self):
        self.assert_unknown(self.start('result'), 'prompt_result', 'prompt_result_failed', 1)

    def test_failed_result_without_acceptance_reports_only_observed_evidence(self):
        diagnostics = self.assert_unknown(self.start('failed-unaccepted'), 'prompt_result', 'turn_failed', 1)
        failure = diagnostics[-1]
        self.assertIs(failure.get('acceptance_observed'), False)
        self.assertNotIn('acceptance_observed', [e['reason_code'] for e in diagnostics])

    def test_accepted_failed_result_on_codex_preserves_safe_evidence_and_hold(self):
        diagnostics = self.assert_unknown(self.start('failed', agent='codex'), 'prompt_result', 'turn_failed', 1)
        self.assertIs(diagnostics[-1].get('acceptance_observed'), True)
        self.assertEqual(sum(e['reason_code'] == 'acceptance_observed' for e in diagnostics), 1)

    def test_shutdown_failure_does_not_erase_answer_or_claim_success(self):
        task = self.start('shutdown')
        self.assert_unknown(task, 'prompt_result', 'post_result_exit_nonzero', 1)
        self.assertEqual([e['text'] for e in self.c.events(self.client, task['id']) if e['kind'] == 'answer'],
                         ['SYNTHETIC_FINAL_ANSWER'])
        self.assertEqual((task['exit_code'], task['stop_reason']), (1, 'end_turn'))

    def test_nonresponsive_shutdown_has_bounded_unknown_settlement(self):
        import time
        started = time.monotonic()
        task = self.start('shutdown-pending')
        self.assertLess(time.monotonic() - started, 8)
        self.assert_unknown(task, 'prompt_result', 'post_result_exit_nonzero', 1)
        self.assertEqual(task['stop_reason'], 'end_turn')
        self.assertIn('Local transport cleanup did not settle', task['settlement_note'])

    def test_normal_path_exposes_distinct_creation_submission_acceptance_and_response(self):
        task = self.start('normal')
        events = self.c.events(self.client, task['id'])
        self.assertEqual((task['state'], self.exits[-1]), ('completed', 0))
        self.assertEqual([e['text'] for e in events if e['kind'] == 'answer'], ['SYNTHETIC_FINAL_ANSWER'])
        self.assertEqual([(e.get('stage'), e.get('reason_code')) for e in events if e['kind'] == 'diagnostic'],
                         [('session_create', 'record_observed'), ('prompt_submit', 'submission_attempted'),
                          ('prompt_execution', 'acceptance_observed'), ('prompt_result', 'result_observed')])
        self.assertNotIn(CANARY, json.dumps(task) + json.dumps(events) + self.output.getvalue())
        self.assertEqual([json.loads(line) for line in (self.root / 'runtime.jsonl').read_text().splitlines()],
                         ['prompt', 'shutdown'])

    def test_abrupt_transport_exit_does_not_invent_a_runtime_cause(self):
        self.assert_unknown(self.start('abrupt'), 'prompt_result', 'result_not_observed', 0)

    def test_unclassified_backend_exception_never_exports_exception_text_or_type(self):
        from backends import BACKENDS
        class SyntheticFailure:
            def prepare_start(self, model, effort, approval):
                return {}

            def run(self, store, task, emit):
                raise type(CANARY, (Exception,), {})(CANARY)

        with patch.dict(BACKENDS, {'fixture': SyntheticFailure()}):
            task = self.start('unused', agent='fixture')
            self.assert_unknown(task, 'runner', 'backend_failed', 0, creations=0)

    def test_observer_exception_after_acceptance_is_not_reported_as_submission_failure(self):
        self.assert_unknown(self.start('invalid-frame'), 'prompt_events', 'transport_observation_failed', 1)

    def test_slow_but_settled_detach_preserves_success(self):
        task = self.start('shutdown-slow')
        self.assertEqual((task['state'], self.exits[-1]), ('completed', 0))
        self.assertEqual([e['text'] for e in self.c.events(self.client, task['id']) if e['kind'] == 'answer'],
                         ['SYNTHETIC_FINAL_ANSWER'])

    def test_cancel_during_successful_detach_never_sends_backend_cancel(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.start, 'shutdown-race')
            deadline = time.monotonic() + 5
            trace = self.root / 'runtime.jsonl'
            while not trace.exists() or '"shutdown"' not in trace.read_text():
                if time.monotonic() > deadline:
                    self.fail('Synthetic runtime never entered detach')
                time.sleep(.01)
            time.sleep(.5)
            task = self.c.list(self.client)[0]
            self.c.cancel(self.client, task['id'])
            result = running.result(timeout=5)
        self.assertEqual((result['state'], result['exit_code'], result['stop_reason']), ('completed', 0, 'end_turn'))
        calls = [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()]
        self.assertFalse(any('cancel' in call for call in calls))
        events = self.c.events(self.client, task['id'])
        self.assertEqual([e['text'] for e in events if e['kind'] == 'answer'], ['SYNTHETIC_FINAL_ANSWER'])
        self.assertNotIn(CANARY, json.dumps(result) + json.dumps(events) + self.output.getvalue())

    def test_malformed_optional_diagnostics_preserve_structured_auth_error(self):
        cases = [([], {}), ({}, 'turn_failed'), ('prompt_result', []),
                 ('prompt_result', {}), (None, None), (CANARY, CANARY)]
        for index, (stage, reason) in enumerate(cases, 1):
            with self.subTest(stage=type(stage).__name__, reason=type(reason).__name__):
                work = self.root / ('malformed-'+str(index))
                work.mkdir()
                os.environ['CALLCLI_DIAGNOSTIC_MODE'] = 'malformed-diagnostic'
                os.environ['CALLCLI_DIAGNOSTIC_FIELDS'] = json.dumps(dict(stage=stage, reasonCode=reason))
                task = self.c.start(self.client, agent='omp', prompt=CANARY, cwd=str(work), write=True)
                events = self.c.events(self.client, task['id'])
                self.assertEqual((task['state'], self.exits[-1]), ('unknown', 1))
                self.assertIn('runtime-reported authentication required', task.get('settlement_note', ''))
                self.assertIn('turn.result.error.detailCode=AUTH_REQUIRED', events[-1]['text'])
                self.assertNotIn(CANARY, json.dumps(task) + json.dumps(events) + self.output.getvalue())
                self.assertFalse(any(e.get('reason_code') == 'transport_observation_failed' for e in events))
                with self.assertRaises(ValueError):
                    self.c.start(self.client, prompt='conflict', cwd=str(work), write=True)

if __name__ == '__main__':
    unittest.main()
