"""Independent real-runner fixture checks; no provider or production DB."""
import os
import time
import unittest
from unittest.mock import patch
import test_swarm_thin

NAV = 'Run /swarm board (default shortcut alt+s) to open the live board.'

class NavigationProtocolTests(unittest.TestCase):
    workspace = test_swarm_thin.SwarmThinTests.workspace

    def test_actual_navigation_retains_owner_without_idle_roster(self):
        with patch.dict(os.environ, CALLCLI_TEST_BOARD_TEXT=NAV):
            with self.workspace('board-working') as (root,c,client,task,thread):
                deadline=time.monotonic()+4
                while not any(e['kind']=='answer' for e in c.events(client,task['id'])):
                    if time.monotonic()>deadline: self.fail('navigation not published')
                    time.sleep(.01)
                time.sleep(.3)
                status=c.status(client,task['id'])
                self.assertEqual(status['state'],'running')
                self.assertTrue(thread.is_alive())
                self.assertTrue(status['native_input']['accepting'])
                self.assertNotIn('native_cleanup',status)
                c.dialogue(client,task['id'],'return actual result',expected_turn=1,command_id='nav-final')
                thread.join(5)
                self.assertEqual(c.status(client,task['id'])['state'],'completed')
                self.assertEqual(c.close(client,task['id'])['state'],'closed')

    def test_mixed_navigation_and_substantive_answer_is_not_swallowed(self):
        for text in (NAV+'\nRequested analysis: all checks passed.',
                     'Requested analysis: all checks passed.\n'+NAV,
                     NAV+' Requested analysis: all checks passed.'):
            with self.subTest(text=text),patch.dict(os.environ,CALLCLI_TEST_BOARD_TEXT=text):
                with self.workspace('board-working') as (root,c,client,task,thread):
                    thread.join(4)
                    self.assertFalse(thread.is_alive())
                    status=c.status(client,task['id'])
                    self.assertEqual(status['state'],'completed')
                    self.assertIn('all checks passed',status['native_result']['result'])
                    self.assertEqual(c.close(client,task['id'])['state'],'closed')
        with patch.dict(os.environ,CALLCLI_TEST_BOARD_TEXT=NAV+'\nWorkers still running on board.',
                        CALLCLI_TEST_DIALOGUE_TEXT=NAV+'\nRequested analysis: all checks passed.'):
            with self.workspace('board-working') as (root,c,client,task,thread):
                deadline=time.monotonic()+4
                while not any(e['kind']=='answer' for e in c.events(client,task['id'])):
                    if time.monotonic()>deadline: self.fail('navigation progress not published')
                    time.sleep(.01)
                time.sleep(.2)
                status=c.status(client,task['id'])
                self.assertEqual(status['state'],'running')
                self.assertTrue(thread.is_alive())
                self.assertNotIn('native_cleanup',status)
                c.dialogue(client,task['id'],'deliver verified result',expected_turn=1,command_id='nav-mixed-final')
                thread.join(5)
                self.assertFalse(thread.is_alive())
                final=c.status(client,task['id'])
                self.assertEqual(final['state'],'completed')
                self.assertIn('all checks passed',final['native_result']['result'])
                self.assertEqual(c.close(client,task['id'])['state'],'closed')
