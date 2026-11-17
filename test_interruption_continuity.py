"""F1-F4 interruption classification, immediate diagnostic attention, no-force stop, I/O boundary, and same-session continuation."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agy import AGY_INTERRUPTED_LINE, is_standalone_incomplete_diagnostic
from agy_native import NativeOwner
from collab import Collaboration
from runner import run_task

API_EOF = 'API error attempt 1: unexpected EOF'


def _text(value):
    return dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=value))


class InterruptionContinuityTests(unittest.TestCase):
    @contextlib.contextmanager
    def workspace(self, scenario, **extra_env):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'agy'
            executable.write_text('#!' + sys.executable + '\n' + (Path(__file__).parent / 'fixtures/agy_native_agent.py').read_text())
            executable.chmod(0o700)
            (root / 'scenario.json').write_text(json.dumps(scenario))
            env = dict(PATH=str(root) + os.pathsep + os.environ['PATH'], AGY_NATIVE_FIXTURE=str(root), **extra_env)
            output = io.StringIO()
            with patch.dict(os.environ, env), patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                    patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                    contextlib.redirect_stdout(output):
                c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                client = c.connect('omp')['client_id']
                task = c.start(client, agent='agy', cwd=directory, prompt='owned continuity audit', write=True)
                yield root, c, client, task, output

    def execute(self, c, task):
        return run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)

    def requests(self, root):
        return [json.loads(line) for line in (root / 'requests.jsonl').read_text().splitlines()]

    def test_f1_standalone_interruption_and_provider_error_settle_unknown_with_negatives_preserved(self):
        self.assertEqual(is_standalone_incomplete_diagnostic(AGY_INTERRUPTED_LINE), 'interrupted')
        self.assertEqual(is_standalone_incomplete_diagnostic(API_EOF + '\n' + AGY_INTERRUPTED_LINE), 'interrupted')
        self.assertEqual(is_standalone_incomplete_diagnostic(API_EOF), 'provider_error')
        self.assertEqual(is_standalone_incomplete_diagnostic('⚠ API error: upstream 503'), 'provider_error')
        self.assertIsNone(is_standalone_incomplete_diagnostic(API_EOF + '\nVerified result: 42'))
        self.assertIsNone(is_standalone_incomplete_diagnostic('Previously saw "' + AGY_INTERRUPTED_LINE + '" before completion.'))

        # Whole and split interruption settle unknown without native_result
        for fg in ([_text(AGY_INTERRUPTED_LINE + '\n')], [_text(AGY_INTERRUPTED_LINE[:24]), _text(AGY_INTERRUPTED_LINE[24:] + '\n')]):
            with self.subTest(fg_len=len(fg)), self.workspace(dict(foreground=fg, replays=[[_text(AGY_INTERRUPTED_LINE + '\n')]])) as (_, c, client, task, _):
                rc = self.execute(c, task)
                st = c.status(client, task['id'])
                self.assertEqual((rc, st['state']), (1, 'unknown'))
                self.assertNotIn('native_result', st)
                self.assertIn('stream interruption', st['settlement_note'])

        # Provider error alone settles unknown
        with self.workspace(dict(foreground=[_text(API_EOF + '\n')], replays=[[_text(API_EOF + '\n')]])) as (_, c, client, task, _):
            rc = self.execute(c, task)
            st = c.status(client, task['id'])
            self.assertEqual((rc, st['state']), (1, 'unknown'))
            self.assertNotIn('native_result', st)
            self.assertIn('provider API error diagnostic', st['settlement_note'])

        # Negative 1: provider retry followed by real answer completes with zero attention_required
        retry_ok = [_text(API_EOF + '\nVerified actual result: 2 + 3 = 5.\n')]
        with self.workspace(dict(foreground=retry_ok, replays=[retry_ok])) as (root, c, client, task, _):
            self.assertEqual(self.execute(c, task), 0)
            st = c.status(client, task['id'])
            self.assertEqual(st['state'], 'completed')
            self.assertIn('Verified actual result', st['native_result']['result'])
            self.assertFalse(any(e.get('attention_required') for e in c.events(client, task['id'])))
            self.assertEqual(sum(r.get('method') == 'session/prompt' for r in self.requests(root)), 1)

        # Negative 2: prose quoting interruption completes normally with zero attention_required
        quoted = [_text('Diagnosis: upstream previously said "' + AGY_INTERRUPTED_LINE + '"; actual verification passes.\n')]
        with self.workspace(dict(foreground=quoted, replays=[quoted])) as (_, c, client, task, _):
            self.assertEqual(self.execute(c, task), 0)
            st = c.status(client, task['id'])
            self.assertEqual(st['state'], 'completed')
            self.assertFalse(any(e.get('attention_required') for e in c.events(client, task['id'])))

    def test_f3_running_interruption_emits_immediate_deduped_diagnostic_attention(self):
        updates = [
            dict(sessionUpdate='tool_call', toolCallId='work', name='execute', kind='execute', status='in_progress'),
            _text(AGY_INTERRUPTED_LINE + '\n'),
        ]
        done_updates = [
            dict(sessionUpdate='tool_call', toolCallId='work', name='execute', kind='execute', status='completed'),
            _text(AGY_INTERRUPTED_LINE + '\nFinal recovered output.\n'),
        ]
        scenario = dict(foreground=updates, replays=[updates, done_updates], review_gates=['before-load0'])
        with self.workspace(scenario) as (root, c, client, task, _):
            thread = threading.Thread(target=lambda: self.execute(c, task))
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not (root / 'waiting-before-load0').exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue((root / 'waiting-before-load0').exists())
                st = c.status(client, task['id'])
                self.assertEqual(st['state'], 'running')
                diags = [e for e in c.events(client, task['id']) if e['kind'] == 'diagnostic']
                self.assertEqual(len(diags), 1)
                self.assertTrue(diags[0]['attention_required'])
                self.assertEqual(diags[0]['diagnostic_code'], 'agy_stream_interrupted')
            finally:
                (root / 'release-before-load0').touch()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(c.status(client, task['id'])['state'], 'completed')
            diags_after = [e for e in c.events(client, task['id']) if e['kind'] == 'diagnostic']
            self.assertEqual(len(diags_after), 1, 'Replay must not duplicate interruption diagnostic for same interaction')

    def test_same_owner_continuation_adopts_dialogue_preserves_artifacts_without_replay_and_closes(self):
        endpoint = '''import json, os, select, sys, time
from pathlib import Path
root = Path(os.environ['AGY_NATIVE_FIXTURE'])
sid = 'owned-continuity-session'
model, effort = 'gemini-4-carbon', 'medium'
history = []
closed = False
def send(value):
    print(json.dumps(dict(jsonrpc='2.0', **value)), flush=True)
def update(value):
    send(dict(method='session/update', params=dict(sessionId=sid, update=value)))
def metadata():
    return dict(sessionId=sid, models=dict(currentModelId=model+'-'+effort,
        availableModels=[dict(modelId='gemini-4-carbon-medium')]), configOptions=[
        dict(id='model', currentValue=model, options=[dict(value=model)]),
        dict(id='thought_level', currentValue=effort, options=[dict(value=effort)])])
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get('method')
    with (root/'requests.jsonl').open('a') as f:
        f.write(json.dumps(msg)+'\\n')
    if method == 'initialize':
        result = dict(agentCapabilities=dict(loadSession=True, sessionCapabilities=dict(close={}, resume={})))
    elif method == 'session/new':
        update(dict(sessionUpdate='available_commands_update', availableCommands=[dict(name='swarm')]))
        result = metadata()
    elif method == 'session/set_config_option':
        if closed:
            send(dict(id=msg['id'], error=dict(code=-32602, message='Session not found')))
            continue
        result = metadata()
    elif method == 'session/load':
        for item in history: update(item)
        result = metadata()
    elif method == 'session/prompt':
        body = msg['params']['prompt'][0]['text']
        first = not history
        history.append(dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)))
        if first:
            (root/'step1-artifact.txt').write_text('step1-once')
            count = int((root/'step1-runs').read_text()) + 1 if (root/'step1-runs').exists() else 1
            (root/'step1-runs').write_text(str(count))
            item = dict(sessionUpdate='agent_message_chunk', content=dict(
                type='text', text='The stream was interrupted. Please continue the task you were working on.\\n'))
            history.append(item)
            update(item)
            (root/'foreground-interrupted').touch()
            while not (root/'release-foreground').exists():
                if select.select([sys.stdin], [], [], .01)[0]:
                    (root/'concurrent-request').touch()
                time.sleep(.01)
        else:
            existing = (root/'step1-artifact.txt').read_text()
            (root/'step2-artifact.txt').write_text(existing + '+step2-continued')
            item = dict(sessionUpdate='agent_message_chunk', content=dict(
                type='text', text='Continued from ' + existing + ' and completed remaining step2.\\n'))
            history.append(item)
            update(item)
        result = dict(stopReason='end_turn')
    elif method == 'session/close':
        closed = True
        result = {}
    else:
        raise RuntimeError(method)
    send(dict(id=msg['id'], result=result))
'''
        with self.workspace(dict(foreground=[], replays=[])) as (root, c, client, task, _):
            (root / 'agy').write_text('#!' + sys.executable + '\n' + endpoint)
            task = c.store.update(task['id'], model='gemini-4-carbon', effort='medium')
            thread = threading.Thread(target=lambda: self.execute(c, task))
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not (root / 'foreground-interrupted').exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue((root / 'foreground-interrupted').exists())
                # Diagnostic already emitted during interrupted foreground
                self.assertTrue(any(e.get('diagnostic_code') == 'agy_stream_interrupted' for e in c.events(client, task['id'])))
                queued = c.dialogue(
                    client, task['id'],
                    'Continue from step1-artifact.txt without re-running step1.',
                    expected_turn=1, command_id='continue-1',
                )
                self.assertEqual(queued['commands']['continue-1']['status'], 'accepted')
                (root / 'release-foreground').touch()
                thread.join(6)
                self.assertFalse(thread.is_alive())
                st = c.status(client, task['id'])
                self.assertEqual((st['state'], st['turn'], st['commands']['continue-1']['status']), ('completed', 1, 'returned'))
                self.assertEqual((root / 'step1-runs').read_text(), '1')
                self.assertEqual((root / 'step2-artifact.txt').read_text(), 'step1-once+step2-continued')
                self.assertIn('step1-once', st['native_result']['result'])
                prompts = [r for r in self.requests(root) if r.get('method') == 'session/prompt']
                self.assertEqual(len(prompts), 2)
                self.assertEqual(len({r['params']['sessionId'] for r in prompts}), 1)
                self.assertEqual(c.close(client, task['id'])['state'], 'closed')
            finally:
                (root / 'release-foreground').touch()
                thread.join(5)

    def test_f2_f4_abnormal_no_force_and_broken_pipe_boundary_diagnostics(self):
        stops = []
        orig_stop = NativeOwner.stop

        def record_stop(owner, **kwargs):
            stops.append(kwargs.get('force', True))
            return orig_stop(owner, **kwargs)

        with self.workspace(dict(foreground=[], replays=[]), AGY_NATIVE_EXIT='foreground') as (_, c, client, task, _), \
                patch.object(NativeOwner, 'stop', record_stop):
            self.assertEqual(self.execute(c, task), 1)
            self.assertEqual(c.status(client, task['id'])['state'], 'unknown')
            self.assertEqual(stops, [False])

        # BrokenPipeError on initial session/prompt (even with secret string in exception)
        CANARY = 'SECRET_PIPE_DETAIL_9911'
        orig_send = NativeOwner.send

        def fail_prompt_send(owner, value):
            if value.get('method') == 'session/prompt':
                raise BrokenPipeError(CANARY)
            return orig_send(owner, value)

        with self.workspace(dict(foreground=[], replays=[])) as (_, c, client, task, out), \
                patch.object(NativeOwner, 'send', fail_prompt_send):
            self.assertEqual(self.execute(c, task), 1)
            st = c.status(client, task['id'])
            self.assertEqual(st['state'], 'unknown')
            self.assertIn('reason=acp_transport_io_error', st['settlement_note'])
            self.assertIn('phase=session/prompt', st['settlement_note'])
            self.assertNotIn('reason=backend_failed', st['settlement_note'])
            self.assertNotIn(CANARY, json.dumps(st) + out.getvalue())
            with self.assertRaises(ValueError):
                c.intervene(client, task['id'], 'no broad unknown intervene', expected_turn=1, command_id='iv-no')

        # BrokenPipeError during dialogue send marks dialogue command status 'unknown'
        prompt_calls = [0]

        def fail_dialogue_send(owner, value):
            if value.get('method') == 'session/prompt':
                prompt_calls[0] += 1
                if prompt_calls[0] == 2:
                    raise BrokenPipeError(CANARY)
            return orig_send(owner, value)

        q_updates = [_text('CALLCLI_QUESTION Q1 Proceed?\n')]
        with self.workspace(dict(foreground=q_updates, replays=[q_updates] * 6)) as (_, c, client, task, out), \
                patch.object(NativeOwner, 'send', fail_dialogue_send):
            thread = threading.Thread(target=lambda: self.execute(c, task))
            thread.start()
            deadline = time.monotonic() + 5
            while (not c.status(client, task['id']).get('native_observation', {}).get('question_ids')
                   and time.monotonic() < deadline):
                time.sleep(0.01)
            q_id = c.status(client, task['id'])['questions'][0]
            c.dialogue(client, task['id'], 'yes', expected_turn=1, command_id='dlg-pipe', question_id=q_id)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            st = c.status(client, task['id'])
            self.assertEqual(st['state'], 'unknown')
            self.assertEqual(st['commands']['dlg-pipe']['status'], 'unknown')
            self.assertIn('reason=acp_transport_io_error', st['settlement_note'])
            self.assertNotIn(CANARY, json.dumps(st) + out.getvalue())

    def test_b4_b6_diagnostic_plus_navigation_and_lingering_process_no_force(self):
        import subprocess

        # B4: Diagnostic + board navigation settles unknown; retry + navigation + real answer completes
        for diag_only in (
            'API error (attempt 1): 401 UNAUTHENTICATED\nRun /swarm board\n',
            'Run /swarm board (default shortcut ctrl+b) to open the live board.\n' + AGY_INTERRUPTED_LINE + '\n',
        ):
            updates = [_text(diag_only)]
            with self.subTest(diag_only=diag_only), self.workspace(dict(foreground=updates, replays=[updates])) as (_, c, client, task, _):
                self.assertEqual(self.execute(c, task), 1)
                st = c.status(client, task['id'])
                self.assertEqual(st['state'], 'unknown')
                self.assertNotIn('native_result', st)

        recovered_updates = [_text(API_EOF + '\nRun /swarm board\nVerified actual result: 5.\n')]
        with self.workspace(dict(foreground=recovered_updates, replays=[recovered_updates])) as (_, c, client, task, _):
            self.assertEqual(self.execute(c, task), 0)
            st = c.status(client, task['id'])
            self.assertEqual(st['state'], 'completed')
            self.assertIn('Verified actual result: 5.', st['native_result']['result'])

        # B6: Verified unload on diagnostic unknown + lingering local process never invokes terminate/kill
        linger_script = '''import json, os, sys, time
sid = 'linger-diag-sid'
model, effort = 'gemini-4-carbon', 'medium'
history = []
closed = False
def send(v):
    print(json.dumps(dict(jsonrpc='2.0', **v)), flush=True)
def update(u):
    send(dict(method='session/update', params=dict(sessionId=sid, update=u)))
def meta():
    return dict(sessionId=sid, models=dict(currentModelId=model+'-'+effort,
        availableModels=[dict(modelId='gemini-4-carbon-medium')]), configOptions=[
        dict(id='model', currentValue=model, options=[dict(value=model)]),
        dict(id='thought_level', currentValue=effort, options=[dict(value=effort)])])
for line in sys.stdin:
    msg = json.loads(line)
    m = msg.get('method')
    if m == 'initialize':
        send(dict(id=msg['id'], result=dict(agentCapabilities=dict(loadSession=True, sessionCapabilities=dict(close={})))))
    elif m == 'session/new':
        send(dict(id=msg['id'], result=meta()))
        update(dict(sessionUpdate='available_commands_update', availableCommands=[dict(name='swarm')]))
    elif m == 'session/set_config_option':
        if closed:
            send(dict(id=msg['id'], error=dict(code=-32602, message='Session not found')))
        else:
            send(dict(id=msg['id'], result=meta()))
    elif m == 'session/load':
        for it in history:
            update(it)
        send(dict(id=msg['id'], result=meta()))
    elif m == 'session/prompt':
        body = msg['params']['prompt'][0]['text']
        item = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='The stream was interrupted. Please continue the task you were working on.\\n'))
        history.extend([dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)), item])
        update(item)
        send(dict(id=msg['id'], result=dict(stopReason='end_turn')))
    elif m == 'session/close':
        closed = True
        send(dict(id=msg['id'], result={}))
time.sleep(10)
'''
        orig_stop = NativeOwner.stop
        orig_term = subprocess.Popen.terminate
        orig_kill = subprocess.Popen.kill
        stop_forces = []
        term_calls = []
        kill_calls = []
        spawned_procs = []

        def spy_stop(owner, **kwargs):
            stop_forces.append(kwargs.get('force', True))
            spawned_procs.append(owner.proc)
            kwargs['wait_timeout'] = 0.15
            return orig_stop(owner, **kwargs)

        def spy_term(proc_self):
            term_calls.append(proc_self.pid)
            return orig_term(proc_self)

        def spy_kill(proc_self):
            kill_calls.append(proc_self.pid)
            return orig_kill(proc_self)

        try:
            with self.workspace(dict(foreground=[], replays=[])) as (root, c, client, task, _), \
                    patch.object(NativeOwner, 'stop', spy_stop), \
                    patch.object(subprocess.Popen, 'terminate', spy_term), \
                    patch.object(subprocess.Popen, 'kill', spy_kill):
                (root / 'agy').write_text('#!' + sys.executable + '\n' + linger_script)
                task = c.store.update(task['id'], model='gemini-4-carbon', effort='medium')
                rc = self.execute(c, task)
                self.assertEqual(rc, 1)
                st = c.status(client, task['id'])
                self.assertEqual(st['state'], 'unknown')
                self.assertIs(st['native_cleanup']['session_unloaded'], True)
                self.assertEqual(stop_forces, [False])
                self.assertEqual(term_calls, [])
                self.assertEqual(kill_calls, [])
                self.assertEqual(st['native_owner_exit']['kind'], 'stdio-closed-exit-unverified')
                self.assertIs(st['native_owner_exit']['forced'], False)
                self.assertNotIn('native_resume_session_id', st)
                with self.assertRaises(ValueError):
                    c.intervene(client, task['id'], 'no resume after unverified exit', expected_turn=1, command_id='iv-no-resume')
        finally:
            for proc in spawned_procs:
                if proc.poll() is None:
                    orig_kill(proc)
                    proc.wait(timeout=2)


if __name__ == '__main__':
    unittest.main()
