import time
import importlib
from unittest.mock import patch
from test_swarm_thin import SwarmThinTests
from test_recovery_status import RecoveryStatusTests
from fixtures.close_backend import CloseBackend

class NativeFinding(SwarmThinTests):
    def test_historical_working_then_substantive_final(self):
        with self.workspace('working-final') as (root,c,client,task,thread):
            thread.join(2)
            self.assertEqual(c.status(client,task['id'])['state'],'completed')
            self.assertFalse(thread.is_alive())
            self.assertEqual(c.close(client,task['id'])['state'],'closed')
    def test_final_cannot_override_current_tool_or_question(self):
        for fault in ('working-final-active','working-final-question'):
            with self.subTest(fault=fault),self.workspace(fault) as (root,c,client,task,thread):
                deadline=time.monotonic()+3
                while not c.status(client,task['id']).get('native_observation') and time.monotonic()<deadline: time.sleep(.01)
                time.sleep(.2)
                current=c.status(client,task['id'])
                self.assertEqual(current['state'],'running')
                self.assertNotIn('native_cleanup',current)
                self.assertTrue(thread.is_alive())

class ReleaseFinding(RecoveryStatusTests):
    def test_completed_close_preserves_business_in_recovery_projection(self):
        task=self.start()
        self.store.update(task['id'],state='completed',runner_pid=0,acpx_record_id='fixture-record')
        backend=CloseBackend()
        with patch.object(importlib.import_module('callcli_plugin.release'),'NativeBackend',return_value=backend):
            self.action('close',task,expected_turn=1)
        projection=self.action('recovery_status',task)['resource_recovery']
        self.assertEqual(projection['resource_state'],'released')
        self.assertEqual(projection['business_state'],'completed')
        self.assertEqual(projection['receipt']['business_state'],'completed')
