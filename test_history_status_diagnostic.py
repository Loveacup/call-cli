"""Malformed tool status must not mask a fail-closed history diagnosis."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agy_native import ProtocolError, additions, normalize, snapshot
from collab import Collaboration
from omp_bridge import HostBridge
from runner import run_task
import test_plugin


CANARY = 'SYNTHETIC_PRIVATE_STATUS_CANARY_73c912'
CANARY_SHA = hashlib.sha256(CANARY.encode()).hexdigest()


def text(value):
    return dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=value))


def tool(status, kind='read'):
    return dict(sessionUpdate='tool_call', toolCallId='status-tool', name='read_file',
                kind=kind, status=status, rawInput={'secret': CANARY})


class HistoryStatusDiagnosticTests(unittest.TestCase):
    def test_malformed_status_preserves_prefix_and_tool_conflict_diagnostics(self):
        values = ({}, {'secret': CANARY}, [], [CANARY], None, 42, CANARY)
        for status in values:
            cases = (
                ('prefix_new', [text(CANARY)], [tool(status)], 'prefix_changed', None, None),
                ('prefix_old', [tool(status)], [text(CANARY)], 'prefix_changed', None, None),
                ('tool_new', [tool('pending')], [tool(status, 'edit')], 'tool_changed', 'pending', None),
                ('tool_old', [tool(status)], [tool('completed', 'edit')], 'tool_changed', None, 'completed'),
            )
            for label, previous, current, reason, old_status, new_status in cases:
                with self.subTest(case=label, status=status):
                    previous = snapshot([normalize(entry) for entry in previous])
                    current = snapshot([normalize(entry) for entry in current])
                    with self.assertRaises(ProtocolError) as caught:
                        additions(previous, current, background_growth=True,
                                  call_site='section', interaction_seq=0)
                    error = caught.exception
                    self.assertEqual(str(error), 'Native '+('tool history changed' if reason == 'tool_changed'
                                                            else 'history prefix changed')+'; reconciliation required')
                    diag = error.history_diagnostic
                    self.assertEqual(diag['reason_code'], reason)
                    self.assertEqual(diag['index'], 0)
                    self.assertEqual(diag['old_status'], old_status)
                    self.assertEqual(diag['new_status'], new_status)
                    for blob in (str(error), json.dumps(diag)):
                        self.assertNotIn(CANARY, blob)
                        self.assertNotIn(CANARY_SHA, blob)

    def test_malformed_replay_status_keeps_safe_diagnosis_on_public_hosts(self):
        for status in ({'secret': CANARY}, [CANARY], None):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                executable = root/'agy'
                executable.write_text('#!'+sys.executable+'\n'+(Path(__file__).parent/'fixtures/agy_native_agent.py').read_text())
                executable.chmod(0o700)
                first = [text('CALLCLI_STAGE pending\n'), tool('pending')]
                second = [text('CALLCLI_STAGE pending\n'), tool(status, 'edit')]
                (root/'scenario.json').write_text(json.dumps(dict(foreground=[], replays=[first, second])))
                stdout = io.StringIO()
                with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'],
                                AGY_NATIVE_FIXTURE=directory), \
                        patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), \
                        patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                        contextlib.redirect_stdout(stdout):
                    c = Collaboration(root/'tasks.db', launch=lambda task: None)
                    client = c.connect('omp')['client_id']
                    task = c.start(client, agent='agy', cwd=directory, prompt='Private '+CANARY, write=True)
                    code = run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)
                state = c.status(client, task['id'])
                self.assertEqual(code, 1)
                self.assertEqual(state['state'], 'unknown')
                self.assertNotIn('native_result', state)
                self.assertNotIn('native_cleanup', state)
                self.assertFalse((root/'closed-session').exists())
                diag = state['native_observation']['history_diagnostic']
                self.assertEqual(diag['reason_code'], 'tool_changed')
                self.assertEqual(diag['call_site'], 'section')
                self.assertEqual(diag['old_status'], 'pending')
                self.assertIsNone(diag['new_status'])
                self.assertEqual(diag['turn'], 1)
                self.assertIn('Native tool history changed; reconciliation required [', state['settlement_note'])
                self.assertNotIn('backend_failed', state['settlement_note'])
                raw = c.store.get(task['id'])
                projections = (test_plugin.load_plugin().Coordinator.public(raw), HostBridge.public(raw))
                for projected in projections:
                    self.assertEqual(projected['native_observation']['history_diagnostic'], diag)
                    self.assertEqual(projected['settlement_note'], state['settlement_note'])
                for value in (state, c.events(client, task['id']), *projections):
                    blob = json.dumps(value)
                    self.assertNotIn(CANARY, blob)
                    self.assertNotIn(CANARY_SHA, blob)
                self.assertNotIn(CANARY, stdout.getvalue())
                self.assertNotIn(CANARY_SHA, stdout.getvalue())
