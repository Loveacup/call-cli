import json
import sqlite3
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from omp_bridge import HostBridge


class HostBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.launches = []
        self.bridge = HostBridge(Path(self.tmp.name) / 'tasks.db', launch=lambda t: self.launches.append(t))
        self.a = dict(session_id='native-a', agent_id='main-a')
        self.b = dict(session_id='native-b', agent_id='main-b')
        self.key = self.bridge.bind(self.a)

    def call(self, action, **args):
        return self.bridge.handle(self.a, self.key, dict(action=action, **args))

    def test_native_binding_rejects_other_session_and_model_origin(self):
        task = self.call('start', prompt='inspect', cwd='/project', agent='omp')['task']
        self.assertEqual(task['host_binding'], 'omp-extension')
        self.assertNotIn('origin', task)
        self.assertNotIn(self.key, str(task))
        with self.assertRaises(PermissionError):
            self.bridge.handle(self.b, self.key, dict(action='status', task_id=task['id']))
        with self.assertRaises(ValueError):
            self.call('start', prompt='inspect', cwd='/project', origin=self.a)

    def test_progress_question_cancel_resume_keep_exact_task_and_cursor(self):
        task = self.call('start', prompt='inspect', cwd='/project', agent='omp')['task']
        tid = task['id']
        self.bridge.store.update(tid, state='running')
        self.bridge.store.event(tid, '1:2', 'stage', 'real progress', turn=1)
        self.bridge.store.event(tid, '1:3', 'question', 'Q1 format?', turn=1)
        packet = self.call('wait', task_id=tid, after=0, timeout=0)
        self.assertEqual([e['kind'] for e in packet['events']], ['stage', 'question'])
        self.assertEqual(self.call('wait', task_id=tid, after=packet['after'], timeout=0)['events'], [])
        self.call('intervene', task_id=tid, prompt='revised', command_id='stable')
        self.assertEqual(self.call('status', task_id=tid)['task']['state'], 'cancelling')
        self.bridge.store.finish_turn(tid, 1, 'cancelled', 0, 'cancelled')
        resumed = self.call('wait', task_id=tid, after=packet['after'], timeout=0)['task']
        self.assertEqual((resumed['turn'], resumed['session']), (2, task['session']))
        self.assertEqual(len(self.launches), 2)
        self.call('intervene', task_id=tid, prompt='revised', command_id='stable')
        self.assertEqual(len(self.launches), 2)

    def test_distinct_valid_host_binding_cannot_control_other_task_or_bypass_writer(self):
        task = self.call('start', prompt='write', cwd='/project', agent='omp', write=True)['task']
        other = self.bridge.bind(self.b)
        with self.assertRaises(PermissionError):
            self.bridge.handle(self.b, other, dict(action='cancel', task_id=task['id']))
        with self.assertRaises(ValueError):
            self.bridge.handle(self.b, other, dict(action='start', prompt='write', cwd='/project/sub', agent='omp', write=True))
        self.assertEqual(self.call('status', task_id=task['id'])['task']['state'], 'queued')

    def test_one_shot_wait_locked_initialization_is_busy_and_preserves_task(self):
        task = self.call('start', prompt='inspect', cwd='/project', agent='omp')['task']
        request = dict(db=str(self.bridge.store.path), host=self.a, capability=self.key,
                       args=dict(action='wait', task_id=task['id'], after=17, timeout=0))
        with closing(sqlite3.connect(self.bridge.store.path)) as lock:
            lock.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            result = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('omp_bridge.py'))],
                                    input=json.dumps(request), text=True, capture_output=True, timeout=2)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), dict(task=None, events=[], after=17, busy=True))
        self.assertEqual(self.call('status', task_id=task['id'])['task'], task)
        packet = self.call('wait', task_id=task['id'], after=17, timeout=0)
        self.assertFalse(packet['busy'])
        self.assertEqual(packet['task']['state'], 'queued')


if __name__ == '__main__':
    unittest.main()
