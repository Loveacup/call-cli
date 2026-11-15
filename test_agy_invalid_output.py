"""Offline regressions for the observed public AGY invalid-output sentinel."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from collab import Collaboration
from runner import run_task
from agy import outcome_note
from events import EventDecoder
import test_runner


class SingleOutcomeIntegrationTests(unittest.TestCase):
    setUp = test_runner.RunnerBehaviorTests.setUp
    tearDown = test_runner.RunnerBehaviorTests.tearDown
    run_agy = test_runner.RunnerBehaviorTests.run_agy

    def test_invalid_output_uses_existing_unknown_notification_path(self):
        rc, status, events = self.run_agy(['⚠ Model produced invalid output.'])
        self.assertEqual((rc, status['state'], status['exit_code'], status['stop_reason']),
                         (1, 'unknown', 0, 'end_turn'))
        self.assertIn('no usable final answer', status['settlement_note'])
        self.assertTrue(events[-1]['attention_required'])
        self.assertEqual(events[-1]['notification_scope'], 'originating-session')


WARNING = '⚠ Model produced invalid output.'


class InvalidOutputTests(unittest.TestCase):
    def decode(self, text):
        decoder = EventDecoder(shared_request_id='owned:1')
        # Split chunks prove this is an outcome check, not a token classifier.
        for chunk in (text[:8], text[8:]):
            decoder.feed(dict(type='callcli.shared.event', requestId='owned:1', event=dict(
                type='text_delta', stream='output', tag='agent_message_chunk', text=chunk)))
        decoder.feed(dict(type='callcli.shared.result', requestId='owned:1',
                          status='completed', stopReason='end_turn'))
        decoder.finish()
        return decoder

    def test_single_invalid_output_is_not_usable_despite_end_turn_exit_zero(self):
        decoder = self.decode(WARNING)
        self.assertFalse(decoder.failed)  # Keep protocol and business layers separate.
        self.assertEqual(decoder.stop_reason, 'end_turn')
        note = outcome_note(decoder, 0, 'completed')
        self.assertIsNotNone(note)
        self.assertIn(WARNING, note)
        self.assertIn('no usable final answer', note)
        self.assertNotIn('Tool failure', note)
        self.assertNotIn('permission decisions', note)
        self.assertIn('no automatic replay', note)

    def test_warning_quotations_and_ordinary_failure_reports_remain_answers(self):
        for text in ('The message was: '+WARNING, '> '+WARNING, '`'+WARNING+'`',
                     '```\n'+WARNING+'\n```', 'Example warning:\n'+WARNING,
                     WARNING+'\nHere is the recovered result.',
                     'The delegated worker timed out; no artifact was produced.'):
            with self.subTest(text=text):
                self.assertIsNone(outcome_note(self.decode(text), 0, 'completed'))

    def test_existing_fatal_sentinel_still_reports_unknown(self):
        from agy import AGY_FATAL_LINE
        note = outcome_note(self.decode('Progress\n'+AGY_FATAL_LINE), 0, 'completed')
        self.assertIn('fatal execution error', note)
        self.assertIn('outcome unknown', note)

    def test_whitespace_only_around_exact_warning_does_not_make_it_usable(self):
        note = outcome_note(self.decode(' \n'+WARNING+'\n\t'), 0, 'completed')
        self.assertIn('no usable final answer', note)

    def test_native_error_only_result_notifies_but_proven_unload_can_close(self):
        for model in ('ocs', 'oct'):
            for phase in ('foreground', 'replay'):
                with self.subTest(model=model, phase=phase), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    executable = root/'agy'
                    executable.write_text('#!'+sys.executable+'\n'+
                        (Path(__file__).parent/'fixtures/agy_native_agent.py').read_text().replace(
                            "availableCommands=[dict(name='swarm')]", "availableCommands=[dict(name='swarm'), dict(name='teamwork')]"))
                    executable.chmod(0o700)
                    warning = dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text=WARNING))
                    (root/'scenario.json').write_text(json.dumps(dict(
                        foreground=[warning] if phase == 'foreground' else [], replays=[[warning]])))
                    output = io.StringIO()
                    with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], AGY_NATIVE_FIXTURE=directory), \
                            patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), \
                            patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                            contextlib.redirect_stdout(output):
                        c = Collaboration(root/'tasks.db', launch=lambda task: None)
                        client = c.connect('hermes')['client_id']
                        task = c.start(client, agent='agy', model=model, cwd=directory, prompt='synthetic', write=True)
                        rc = run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)
                        status = c.status(client, task['id'])
                        self.assertEqual(status['state'], 'unknown')
                        self.assertEqual(rc, 1)
                        self.assertNotIn('native_result', status)
                        self.assertIn('no usable final answer', status['settlement_note'])
                        self.assertEqual(status['native_foreground_stop'], 'end_turn')
                        self.assertTrue(status['native_cleanup']['session_unloaded'])
                        self.assertEqual(status['native_cleanup']['outcome'], 'unknown')
                        self.assertEqual(status['native_owner_exit']['kind'], 'natural')
                        self.assertEqual(status['native_owner_exit']['returncode'], 0)
                        event = c.events(client, task['id'])[-1]
                        self.assertTrue(event['attention_required'])
                        self.assertEqual(event['notification_scope'], 'originating-session')
                        self.assertIn('Do not replay', event['next_action'])
                        notice = json.loads(output.getvalue().splitlines()[-1])
                        self.assertEqual((notice['event'], notice['task_id'], notice['turn']), ('unknown', task['id'], 1))
                        self.assertEqual(c.close(client, task['id'])['state'], 'closed')
                        self.assertEqual(c.close(client, task['id'])['state'], 'closed')
                        self.assertIn('no usable final answer', c.status(client, task['id'])['settlement_note'])
                    requests = [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
                    self.assertEqual(sum(r.get('method') == 'session/prompt' for r in requests), 1)
                    self.assertEqual(sum(r.get('method') == 'session/close' for r in requests), 1)
                    self.assertFalse(any(r.get('method') == 'session/cancel' for r in requests))
