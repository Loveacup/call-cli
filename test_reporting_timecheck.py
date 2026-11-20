"""Offline tests for Slice A-v2 event noise reduction, 600s ReviewTracker, and terminal delivery invariants."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agy_dynamic import run_dynamic
from bridge import Origin, Store
import events
from events import (
    QUIET_REVIEW_SECONDS,
    ReviewTracker,
    classify_event_delivery,
)
from runner import run_task


def _write_fake_acpx_harness(root: Path):
    """Create a tiny zero-model fake acpx/node/npm executable set in root."""
    fake = root / 'acpx'
    fake.write_text('#!' + sys.executable + '\n' + r'''
import json, os, pathlib, sys, time
shared = pathlib.Path(sys.argv[0]).name == 'node'
payload = json.loads(sys.stdin.read()) if shared else None
args = ['prompt', '-s', payload['options']['sessionName']] if shared else sys.argv[1:]
root = pathlib.Path(os.environ['A2_FAKE_ROOT'])
if '--version' in args:
    print('0.22.0')
    raise SystemExit(0)
if 'new' in args:
    print(json.dumps({'acpxRecordId': '11111111-1111-4111-8111-111111111111'}))
    raise SystemExit(0)
if 'close' in args:
    raise SystemExit(0)
if 'cancel' in args:
    sess = args[args.index('-s') + 1]
    (root / ('cancel-' + sess)).touch()
    raise SystemExit(0)
if shared:
    req = payload['options']['requestId']
    sess = payload['options']['sessionName']
    with (root / ('started-' + sess)).open('w') as f:
        f.write(req)
    print(json.dumps({'type': 'callcli.shared.event', 'requestId': req, 'event': {'type': 'prompt_started'}}), flush=True)
    prompt_text = payload['prompt']
    if 'CHURN_BLOCK' in prompt_text:
        while not (root / ('cancel-' + sess)).exists():
            act = root / ('activity-' + sess)
            if act.exists():
                val = act.read_text()
                act.unlink()
                print(json.dumps({'type': 'callcli.shared.event', 'requestId': req,
                                  'event': {'type': 'text_delta', 'stream': 'output', 'tag': 'agent_message_chunk', 'text': val}}), flush=True)
            tool_f = root / ('tool-' + sess)
            if tool_f.exists():
                tdata = json.loads(tool_f.read_text())
                tool_f.unlink()
                print(json.dumps({'type': 'callcli.shared.event', 'requestId': req,
                                  'event': dict(tdata, type='tool_call')}), flush=True)
            time.sleep(.02)
        print(json.dumps({'type': 'callcli.shared.result', 'requestId': req, 'status': 'cancelled', 'stopReason': 'cancelled'}), flush=True)
        raise SystemExit(0)
    if 'NOISE_TEST' in prompt_text:
        chunks = [
            'CALLCLI_STAGE step1\n',
            'CALLCLI_STAGE step1\n',
            'CALLCLI_SKILL skill-1 adopted verified locally\n',
            'Intermediate answer line\n',
            'CALLCLI_STAGE step2\n',
            'Final answer line\n',
        ]
        for ch in chunks:
            print(json.dumps({'type': 'callcli.shared.event', 'requestId': req,
                              'event': {'type': 'text_delta', 'stream': 'output', 'tag': 'agent_message_chunk', 'text': ch}}), flush=True)
        print(json.dumps({'type': 'callcli.shared.result', 'requestId': req, 'status': 'completed', 'stopReason': 'end_turn'}), flush=True)
        raise SystemExit(0)
    if 'ASK_TEST' in prompt_text:
        print(json.dumps({'type': 'callcli.shared.event', 'requestId': req,
                          'event': {'type': 'text_delta', 'stream': 'output', 'tag': 'agent_message_chunk', 'text': 'CALLCLI_QUESTION Q1 Proceed?\n'}}), flush=True)
        print(json.dumps({'type': 'callcli.shared.result', 'requestId': req, 'status': 'completed', 'stopReason': 'end_turn'}), flush=True)
        raise SystemExit(0)
    if 'UNKNOWN_TEST' in prompt_text:
        print(json.dumps({'type': 'callcli.shared.event', 'requestId': req,
                          'event': {'type': 'text_delta', 'stream': 'output', 'tag': 'agent_message_chunk', 'text': 'CALLCLI_STAGE only stage\n'}}), flush=True)
        print(json.dumps({'type': 'callcli.shared.result', 'requestId': req, 'status': 'completed', 'stopReason': 'end_turn'}), flush=True)
        raise SystemExit(0)
    print(json.dumps({'type': 'callcli.shared.event', 'requestId': req,
                      'event': {'type': 'text_delta', 'stream': 'output', 'tag': 'agent_message_chunk', 'text': 'DONE_OK\n'}}), flush=True)
    print(json.dumps({'type': 'callcli.shared.result', 'requestId': req, 'status': 'completed', 'stopReason': 'end_turn'}), flush=True)
''')
    fake.chmod(0o700)
    node = root / 'node'
    node.write_bytes(fake.read_bytes())
    node.chmod(0o700)
    npm = root / 'npm'
    npm.write_text('#!' + sys.executable + '\nimport pathlib, sys\nprint(pathlib.Path(__file__).parent / "node_modules")\n')
    npm.chmod(0o700)


class EventNoiseReductionTests(unittest.TestCase):
    def test_classify_event_delivery_noise_and_substantive_boundaries(self):
        for kind in ('started', 'version', 'model', 'permissions', 'skill_staged', 'skill_cleanup'):
            d = classify_event_delivery(kind, 'info')
            self.assertFalse(d['deliver'], kind)
            self.assertEqual(d['tier'], 'store_only')
        for reason in ('record_observed', 'submission_attempted', 'acceptance_observed', 'result_observed'):
            d = classify_event_delivery('diagnostic', 'routine', extra={'reason_code': reason})
            self.assertFalse(d['deliver'], reason)
            self.assertTrue(classify_event_delivery('diagnostic', 'urgent', extra={'reason_code': reason, 'attention_required': True})['deliver'])
        self.assertTrue(classify_event_delivery('diagnostic', 'error', extra={'reason_code': 'turn_failed'})['deliver'])
        self.assertTrue(classify_event_delivery('diagnostic', 'unclassified error')['deliver'])
        self.assertFalse(classify_event_delivery('skill', 'skill-1 read')['deliver'])
        self.assertTrue(classify_event_delivery('skill', 'skill-1 unavailable', extra={'attention_required': True})['deliver'])
        self.assertFalse(classify_event_delivery('answer', 'interim line')['deliver'])
        self.assertTrue(classify_event_delivery('question', 'Q1 proceed?')['deliver'])
        self.assertTrue(classify_event_delivery('permission', 'approve script')['deliver'])

        s1 = classify_event_delivery('stage', 'phase1', last_stage=None)
        self.assertTrue(s1['deliver'])
        self.assertEqual(s1['next_last_stage'], 'phase1')
        s2 = classify_event_delivery('stage', 'phase1', last_stage=s1['next_last_stage'])
        self.assertFalse(s2['deliver'])
        self.assertEqual(s2['reason'], 'repeated_stage')
        s_notice = classify_event_delivery('stage', 'phase1', extra={'quiet_notice': True}, last_stage='phase1')
        self.assertTrue(s_notice['deliver'])
        self.assertEqual(s_notice['next_last_stage'], 'phase1')

        n = 0
        for _ in range(12):
            td = classify_event_delivery('tool', 't read ok', tool_notices=n)
            self.assertTrue(td['deliver'])
            n = td['next_tool_notices']
        td13 = classify_event_delivery('tool', 't13 read ok', tool_notices=n)
        self.assertFalse(td13['deliver'])
        self.assertEqual(td13['reason'], 'tool_cap')
        t_fail = classify_event_delivery('tool', 't14 execute failed', tool_notices=td13['next_tool_notices'])
        self.assertTrue(t_fail['deliver'])
        self.assertEqual(t_fail['reason'], 'tool_failed')

    def test_runner_retains_all_events_in_sqlite_while_suppressing_stdout_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_acpx_harness(root)
            with patch.dict(os.environ, {'PATH': str(root) + os.pathsep + os.environ['PATH'], 'A2_FAKE_ROOT': str(root)}), \
                    patch('updates.ensure_current', return_value={'decision': 'current'}), \
                    patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'):
                store = Store(root / 'tasks.db')
                origin = Origin('test', 's1', 'k1')
                task = store.create(origin, 'local', 'omp', str(root), 'NOISE_TEST', False)
                task = store.update(task['id'], shell='posix')
                task = store.claim_launch(task['id'], 1, 1)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = run_task(store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)
                self.assertEqual(rc, 0)
                persisted = store.events(task['id'], origin)
                persisted_kinds = [e['kind'] for e in persisted]
                self.assertIn('started', persisted_kinds)
                self.assertIn('diagnostic', persisted_kinds)
                self.assertIn('skill', persisted_kinds)
                self.assertIn('answer', persisted_kinds)
                self.assertEqual([e['text'] for e in persisted if e['kind'] == 'stage'], ['step1', 'step1', 'step2'])

                frames = [json.loads(line) for line in out.getvalue().splitlines()]
                self.assertEqual(
                    [(f['event'], f['text']) for f in frames],
                    [('stage', 'step1'), ('stage', 'step2'), ('completed', 'Prompt exited 0')],
                )
                self.assertNotIn('work_stop', frames[-1])
                self.assertNotIn('工作已停，未完成', out.getvalue())
                self.assertEqual(frames[-1]['turn'], 1)
                self.assertEqual(frames[-1]['seq'], persisted[-1]['seq'])
                self.assertEqual(frames[-1]['event_id'], '1:settled')
                self.assertIn('Final answer line', frames[-1]['answer_tail'])
                self.assertEqual(frames[-1]['recovery']['reservation']['state'], 'completed')


class ReviewTrackerTests(unittest.TestCase):
    def test_review_tracker_distinguishes_quiet_tool_churn_only_and_substantive_progress(self):
        tracker = ReviewTracker(0.0, interval=QUIET_REVIEW_SECONDS)
        # Routine noise never advances visible or substantive timestamps.
        tracker.record_event('started', 'local/omp', 50.0)
        tracker.record_event('diagnostic', 'record observed', 60.0, extra={'reason_code': 'record_observed'})
        self.assertFalse(tracker.is_due(599.0))
        self.assertTrue(tracker.is_due(600.0))
        q = tracker.build_notice(600.0)
        self.assertEqual(q['metadata']['notice_kind'], 'quiet')
        self.assertFalse(q['metadata']['tool_churn_only'])
        self.assertEqual(q['metadata']['quiet_seconds'], 600)

        # Tool churn alone keeps last_visible fresh without moving last_substantive_at.
        tracker2 = ReviewTracker(0.0, interval=QUIET_REVIEW_SECONDS)
        tracker2.record_event('stage', 'analyzing', 10.0)
        tracker2.record_event('stage', 'analyzing', 200.0)  # repeated identical stage is NOT substantive
        for t in (100.0, 300.0, 595.0, 610.0):
            tracker2.record_event('tool', f'read{int(t)} read completed', t)
        self.assertTrue(tracker2.is_due(611.0))
        churn = tracker2.build_notice(611.0)
        self.assertEqual(churn['metadata']['notice_kind'], 'tool_churn_only')
        self.assertTrue(churn['metadata']['tool_churn_only'])
        self.assertFalse(churn['metadata']['substantive_progress'])
        self.assertEqual(churn['metadata']['quiet_seconds'], 1)
        self.assertEqual(churn['metadata']['since_substantive_seconds'], 601)
        self.assertIn('tool_churn_only', churn['text'])

        # Substantive stage/answer/question clears tool_churn_only for the next window.
        tracker2.record_event('stage', 'editing', 700.0)
        chk = tracker2.build_notice(1211.0)
        self.assertEqual(chk['metadata']['notice_kind'], 'checkpoint')
        self.assertFalse(chk['metadata']['tool_churn_only'])
        self.assertTrue(chk['metadata']['substantive_progress'])
        self.assertEqual(chk['metadata']['last_stage'], 'editing')

    def test_acpx_backend_tool_churn_only_review_at_600s_then_substantive_checkpoint(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_acpx_harness(root)
            now = [0.0]
            with patch.dict(os.environ, {'PATH': str(root) + os.pathsep + os.environ['PATH'], 'A2_FAKE_ROOT': str(root)}), \
                    patch('updates.ensure_current', return_value={'decision': 'current'}), \
                    patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                    patch('acpx_backend.time', SimpleNamespace(monotonic=lambda: now[0])):
                store = Store(root / 'tasks.db')
                origin = Origin('test', 's1', 'k1')
                task = store.create(origin, 'local', 'omp', str(root), 'CHURN_BLOCK', False)
                task = store.update(task['id'], shell='posix')
                task = store.claim_launch(task['id'], 1, 1)

                def wait_for(pred):
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if pred():
                            return
                        time.sleep(.02)
                    self.fail('condition timed out')

                def notices():
                    return [e for e in store.events(task['id'], origin) if e.get('quiet_notice')]

                out = io.StringIO()
                with contextlib.redirect_stdout(out), ThreadPoolExecutor(max_workers=1) as pool:
                    fut = pool.submit(run_task, store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)
                    try:
                        wait_for(lambda: (root / ('started-' + task['session'])).exists())
                        time.sleep(.2)
                        for idx, ts in enumerate((100.0, 350.0, 595.0), 1):
                            now[0] = ts
                            (root / ('tool-' + task['session'])).write_text(json.dumps({
                                'toolCallId': f't{idx}', 'title': 'read_file', 'kind': 'read', 'status': 'completed',
                            }))
                            wait_for(lambda n=idx: sum(e['kind'] == 'tool' for e in store.events(task['id'], origin)) >= n)
                        now[0] = 601.0
                        wait_for(lambda: len(notices()) == 1)
                        first = notices()[0]
                        self.assertEqual(first['notice_kind'], 'tool_churn_only')
                        self.assertTrue(first['tool_churn_only'])
                        self.assertFalse(first['substantive_progress'])
                        self.assertEqual(first['since_substantive_seconds'], 601)
                        self.assertLess(first['quiet_seconds'], 600)
                        self.assertFalse((root / ('cancel-' + task['session'])).exists())

                        # Next window: substantive stage arrives at t=1150 -> notice_kind='checkpoint'
                        now[0] = 1150.0
                        (root / ('activity-' + task['session'])).write_text('CALLCLI_STAGE substantive phase\n')
                        wait_for(lambda: any(e['kind'] == 'stage' and e['text'] == 'substantive phase' for e in store.events(task['id'], origin)))
                        now[0] = 1202.0
                        wait_for(lambda: len(notices()) == 2)
                        second = notices()[1]
                        self.assertEqual(second['notice_kind'], 'checkpoint')
                        self.assertFalse(second['tool_churn_only'])
                        self.assertTrue(second['substantive_progress'])

                        store.request_cancel(task['id'], origin)
                        self.assertEqual(fut.result(timeout=5), 0)
                    finally:
                        if not fut.done():
                            store.request_cancel(task['id'], origin)
                            fut.result(timeout=5)

    def test_agy_dynamic_review_tracker_handles_quiet_tool_churn_only_and_checkpoint(self):
        class FakeOwner:
            def __init__(self, replay_steps, on_load):
                self.session_id = 'dyn-sess-1'
                self.review = None
                self.review_blocked = False
                self.permission_handler = None
                self.workflow_identity = {}
                self.pending_drained = []
                self.pending_retry_state = None
                self.drained_public_text = ''
                self.drained_line_pending = ''
                self.fatal_observed = False
                self.history_degraded = False
                self.last_history_diagnostic = None
                self._replay_steps = list(replay_steps)
                self._on_load = on_load

            def request(self, method, params, timeout=None, check_cancel=None, on_update=None):
                if on_update:
                    on_update({'sessionUpdate': 'tool_call', 'tool_event': 'tool_call',
                               'toolCallId': 'wf1', 'title': 'run_workflow', 'status': 'in_progress'})
                return {'stopReason': 'end_turn'}, None

            def load(self, reconcile_stream=True, check_cancel=None):
                self._on_load()
                if self.review:
                    self.review()
                step = self._replay_steps.pop(0) if len(self._replay_steps) > 1 else self._replay_steps[0]
                return None, step

            def drain_pending(self, check_cancel=None):
                return

        wf_sha = 'sha-wf1'
        base_wf = {'sessionUpdate': 'tool_call', 'tool_event': 'tool_call', 'toolCallId': 'wf1',
                   'title': 'run_workflow', 'status': 'in_progress', 'workflow_script_sha256': wf_sha}
        wf_done = dict(base_wf, status='completed')

        scenarios = [
            ('quiet', [
                ([base_wf], 0.0),
                ([base_wf], 605.0),
                ([wf_done, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'DONE\n'}}], 610.0),
            ], 'quiet', False),
            ('tool_churn_only', [
                ([base_wf, {'sessionUpdate': 'tool_call', 'tool_event': 'tool_call', 'toolCallId': 't1', 'title': 'read_file', 'status': 'in_progress'}], 100.0),
                ([base_wf, {'sessionUpdate': 'tool_call', 'tool_event': 'tool_call', 'toolCallId': 't1', 'title': 'read_file', 'status': 'completed'},
                  {'sessionUpdate': 'tool_call', 'tool_event': 'tool_call', 'toolCallId': 't2', 'title': 'grep', 'status': 'in_progress'}], 590.0),
                ([base_wf, {'sessionUpdate': 'tool_call', 'tool_event': 'tool_call', 'toolCallId': 't1', 'title': 'read_file', 'status': 'completed'},
                  {'sessionUpdate': 'tool_call', 'tool_event': 'tool_call', 'toolCallId': 't2', 'title': 'grep', 'status': 'completed'}], 605.0),
                ([wf_done, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'DONE\n'}}], 610.0),
            ], 'tool_churn_only', True),
            ('checkpoint', [
                ([base_wf, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'CALLCLI_STAGE dynamic phase\n'}}], 550.0),
                ([base_wf, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'CALLCLI_STAGE dynamic phase\n'}}], 605.0),
                ([wf_done, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'DONE\n'}}], 610.0),
            ], 'checkpoint', False),
        ]

        for name, steps, expected_kind, expected_churn in scenarios:
            with self.subTest(scenario=name), tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp) / 'tasks.db')
                origin = Origin('test', 's1', 'k1')
                task = store.create(origin, 'local', 'agy', tmp, 'workflow', False)
                task = store.update(
                    task['id'], state='running', launch_turn=1, launch_id='l1',
                    runner_pid=os.getpid(), native_session_id='dyn-sess-1',
                    agy_mode='dynamic', agy_approval='skip-permissions',
                )
                store.native_input_open(task['id'], 1, 'l1', 1)
                cleanup_token = store.native_cleanup_owner(task['id'], 1, 'l1', 1)
                clock = [0.0]
                times = iter([s[1] for s in steps])
                replays = [s[0] for s in steps]
                emitted = []

                def emit(kind, text, **extra):
                    emitted.append(dict(kind=kind, text=text, **extra))

                def advance():
                    clock[0] = next(times, clock[0] + 10.0)

                owner = FakeOwner(replays, advance)
                owner.workflow_identity['wf1'] = wf_sha
                timer = SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000.0 + clock[0], sleep=lambda s: None)
                with patch('agy_dynamic.time', timer):
                    res = run_dynamic(
                        store, store.get(task['id']), owner, emit,
                        policy='', binding=(task['id'], 1, 'l1', 1), token=cleanup_token,
                        baseline=[], unload=lambda reason: True, check_cancel=lambda: False,
                    )
                self.assertEqual(res.state, 'completed')
                notices = [e for e in emitted if e.get('quiet_notice')]
                self.assertEqual(len(notices), 1, notices)
                self.assertEqual(notices[0]['notice_kind'], expected_kind)
                self.assertIs(notices[0]['tool_churn_only'], expected_churn)


class TerminalDeliveryInvariantTests(unittest.TestCase):
    def test_normal_completion_waiting_reply_and_pending_intervention_cancel_never_false_alarm_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_acpx_harness(root)
            with patch.dict(os.environ, {'PATH': str(root) + os.pathsep + os.environ['PATH'], 'A2_FAKE_ROOT': str(root)}), \
                    patch('updates.ensure_current', return_value={'decision': 'current'}), \
                    patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'):
                store = Store(root / 'tasks.db')
                origin = Origin('test', 's1', 'k1')

                # 1. Single normal completed task -> verbatim terminal delivered, no false global stop.
                t_done = store.create(origin, 'local', 'omp', str(root), 'DONE_OK', False)
                t_done = store.update(t_done['id'], shell='posix')
                t_done = store.claim_launch(t_done['id'], 1, 1)
                out_done = io.StringIO()
                with contextlib.redirect_stdout(out_done):
                    rc_done = run_task(store.path, t_done['id'], turn=1, launch_id=t_done['launch_id'], epoch=1)
                self.assertEqual(rc_done, 0)
                persisted_done = store.events(t_done['id'], origin)
                frames_done = [json.loads(line) for line in out_done.getvalue().splitlines()]
                self.assertEqual(len(frames_done), 1)
                self.assertEqual(frames_done[0]['event'], 'completed')
                self.assertEqual(frames_done[0]['turn'], 1)
                self.assertEqual(frames_done[0]['seq'], persisted_done[-1]['seq'])
                self.assertIn('DONE_OK', frames_done[0]['answer_tail'])
                self.assertNotIn('work_stop', frames_done[0])
                self.assertNotIn('工作已停，未完成', out_done.getvalue())
                self.assertNotIn('global_stopped', out_done.getvalue())

                # 2. Single waiting_reply task -> question & terminal delivered verbatim, no false global stop.
                t_wait = store.create(origin, 'local', 'omp', str(root), 'ASK_TEST', False)
                t_wait = store.update(t_wait['id'], shell='posix')
                t_wait = store.claim_launch(t_wait['id'], 1, 1)
                out_wait = io.StringIO()
                with contextlib.redirect_stdout(out_wait):
                    rc_wait = run_task(store.path, t_wait['id'], turn=1, launch_id=t_wait['launch_id'], epoch=1)
                self.assertEqual(rc_wait, 0)
                persisted_wait = store.events(t_wait['id'], origin)
                frames_wait = [json.loads(line) for line in out_wait.getvalue().splitlines()]
                self.assertEqual([f['event'] for f in frames_wait], ['question', 'waiting_reply'])
                self.assertEqual(frames_wait[-1]['seq'], persisted_wait[-1]['seq'])
                self.assertEqual(frames_wait[-1]['question_ids'], [frames_wait[0]['event_id']])
                self.assertNotIn('work_stop', frames_wait[-1])
                self.assertNotIn('工作已停，未完成', out_wait.getvalue())
                self.assertNotIn('global_stopped', out_wait.getvalue())

                # 3. Cancelled with pending intervention -> verbatim terminal with next_action, no work_stop.
                t_int = store.create(origin, 'local', 'omp', str(root), 'CHURN_BLOCK', False)
                t_int = store.claim_launch(t_int['id'], 1, 1)
                store.update(t_int['id'], state='running')
                store.intervene(t_int['id'], origin, 'next phase', 'cmd-next')
                store.update(t_int['id'], state='queued')
                t_int_live = store.get(t_int['id'])
                out_int = io.StringIO()
                with contextlib.redirect_stdout(out_int), patch('runner.backend_for', return_value=SimpleNamespace(
                    run=lambda s, tk, em: SimpleNamespace(state='cancelled', exit_code=0, stop_reason='cancelled', note=None)
                )):
                    rc_int = run_task(store.path, t_int['id'], turn=1, launch_id=t_int_live['launch_id'], epoch=1)
                self.assertEqual(rc_int, 0)
                persisted_int = store.events(t_int['id'], origin)
                frames_int = [json.loads(line) for line in out_int.getvalue().splitlines()]
                self.assertEqual([f['event'] for f in frames_int], ['cancelled'])
                self.assertEqual(frames_int[0]['turn'], 1)
                self.assertEqual(frames_int[0]['seq'], persisted_int[-1]['seq'])
                self.assertIn('intervene', frames_int[0]['next_action'])
                self.assertIn('resume', frames_int[0]['next_action'])
                self.assertNotIn('work_stop', frames_int[0])
                self.assertNotIn('工作已停，未完成', out_int.getvalue())

    def test_real_unknown_durable_terminal_flows_back_despite_unrelated_active_or_queued_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_acpx_harness(root)
            with patch.dict(os.environ, {'PATH': str(root) + os.pathsep + os.environ['PATH'], 'A2_FAKE_ROOT': str(root)}), \
                    patch('updates.ensure_current', return_value={'decision': 'current'}), \
                    patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'):
                store = Store(root / 'tasks.db')
                origin = Origin('test', 's1', 'k1')

                # Same-owner unrelated active, queued, and historical unknown tasks.
                t_run_none = store.create(origin, 'local', 'omp', str(root), 'UNRELATED_RUN', False)
                store.update(t_run_none['id'], state='running')
                t_run_g1 = store.create(origin, 'local', 'omp', str(root), 'UNRELATED_GROUP_RUN', False, group='g1')
                store.update(t_run_g1['id'], state='running')
                t_queued = store.create(origin, 'local', 'omp', str(root), 'UNRELATED_QUEUED', False)
                self.assertEqual(store.get(t_queued['id'])['state'], 'queued')
                t_hist = store.create(origin, 'local', 'omp', str(root), 'CANARY_HISTORICAL', False)
                store.update(t_hist['id'], state='unknown')

                # 1. Real unknown turn in same group as active sibling still delivers durable unknown verbatim.
                t_unk = store.create(origin, 'local', 'omp', str(root), 'UNKNOWN_TEST CANARY_SECRET_PROMPT', False, group='g1')
                t_unk = store.update(t_unk['id'], shell='posix')
                t_unk = store.claim_launch(t_unk['id'], 1, 1)
                out_unk = io.StringIO()
                with contextlib.redirect_stdout(out_unk):
                    rc_unk = run_task(store.path, t_unk['id'], turn=1, launch_id=t_unk['launch_id'], epoch=1)
                self.assertEqual(rc_unk, 1)
                persisted_unk = store.events(t_unk['id'], origin)
                self.assertEqual(persisted_unk[-1]['kind'], 'unknown')
                frames_unk = [json.loads(line) for line in out_unk.getvalue().splitlines()]
                self.assertEqual(frames_unk[-1]['event'], 'unknown')
                self.assertEqual(frames_unk[-1]['turn'], 1)
                self.assertEqual(frames_unk[-1]['seq'], persisted_unk[-1]['seq'])
                self.assertIs(frames_unk[-1]['attention_required'], True)
                self.assertEqual(frames_unk[-1]['notification_scope'], 'originating-session')
                self.assertIn('Do not replay', frames_unk[-1]['next_action'])
                self.assertEqual(frames_unk[-1]['recovery']['reservation']['state'], 'unknown')
                self.assertNotIn('work_stop', frames_unk[-1])
                for forbidden in ('branch_stopped', 'global_stopped', '工作已停，未完成', 'CANARY_SECRET_PROMPT'):
                    self.assertNotIn(forbidden, out_unk.getvalue())

                # 2. Runner backend exception path also delivers durable unknown verbatim without leaking exception text.
                t_exc = store.create(origin, 'local', 'omp', str(root), 'EXC_TEST', False)
                t_exc = store.claim_launch(t_exc['id'], 1, 1)
                out_exc = io.StringIO()
                with contextlib.redirect_stdout(out_exc), patch('runner.backend_for', side_effect=RuntimeError('CANARY_ERR')):
                    rc_exc = run_task(store.path, t_exc['id'], turn=1, launch_id=t_exc['launch_id'], epoch=1)
                self.assertEqual(rc_exc, 1)
                persisted_exc = store.events(t_exc['id'], origin)
                self.assertEqual(persisted_exc[-1]['kind'], 'unknown')
                frames_exc = [json.loads(line) for line in out_exc.getvalue().splitlines()]
                self.assertEqual(frames_exc[-1]['event'], 'unknown')
                self.assertEqual(frames_exc[-1]['seq'], persisted_exc[-1]['seq'])
                self.assertIn('stage=runner reason=backend_failed', frames_exc[-1]['text'])
                self.assertNotIn('work_stop', frames_exc[-1])
                self.assertNotIn('CANARY_ERR', out_exc.getvalue())

    def test_shared_events_module_has_no_project_stop_classifier_or_state_machine(self):
        for removed in (
            'classify_work_stop',
            'StopNoticeTracker',
            'build_stopped_incomplete_summary',
            'is_confirmed_active_task',
            'is_confirmed_active_wakeup',
            'WORK_STOP_STATUSES',
            'ACTIVE_TASK_STATES',
        ):
            self.assertFalse(hasattr(events, removed), removed)

    def test_candidate_skill_docs_encode_coordinator_owned_stop_guidance(self):
        base = Path(__file__).parent / 'skill'
        core = (base / 'references' / 'core-requirements.md').read_text()
        skill_md = (base / 'SKILL.md').read_text()
        coord = (base / 'references' / 'coordination.md').read_text()
        for doc in (core, skill_md, coord):
            for phrase in (
                '工作已停，未完成',
                '已验',
                '未完',
                '停因',
                '谁/什么条件能恢复',
                '进程退出',
                '不建常驻监工',
                'queued',
                'unknown',
                '接手续作证据',
                '终态原样交回',
            ):
                self.assertIn(phrase, doc)
            self.assertNotIn('含 `queued`', doc)
            self.assertNotIn('branch_stopped', doc)
            self.assertNotIn('global_stopped', doc)
            self.assertNotIn('waiting_active_execution', doc)
            self.assertIn('共享 runner 与 OMP 适配', doc)
            self.assertIn('协调者', doc)
        self.assertIn('tool_churn_only', core)
        self.assertIn('ReviewTracker', core)
        self.assertIn('tool_churn_only', skill_md)


if __name__ == '__main__':
    unittest.main()
