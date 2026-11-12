"""Opt-in live AGY probes using existing login and isolated temporary workspaces."""
from contextlib import contextmanager
import shutil
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import tempfile
import unittest
from collab import Collaboration
from runner import control, run_task


@contextmanager
def probe_workspace():
    root = tempfile.mkdtemp(prefix='callcli-agy-probe-')
    try:
        yield root
    except BaseException:
        print('AGY probe evidence preserved at '+root, flush=True)
        raise
    else:
        shutil.rmtree(root)


@unittest.skipUnless(os.environ.get('RUN_NATIVE_AGY_TESTS') == '1', 'opt-in live model')
class NativeAgyTests(unittest.TestCase):
    def test_explicit_model_effort_and_same_session_continuation(self):
        model = os.environ['CALLCLI_AGY_MODEL']  # caller selects; never a fallback
        effort = os.environ.get('CALLCLI_AGY_EFFORT')
        with probe_workspace() as root:
            discovered = json.loads(subprocess.check_output(['node', str(Path(__file__).with_name('models.cjs')), root], timeout=45))
            self.assertIn(model, discovered['availableModelIds'])
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            client = c.connect('codex')['client_id']  # declared, not native Codex provenance
            task = c.start(client, agent='agy', cwd=root, model=model, effort=effort,
                           prompt='Do not use tools. Reply exactly CALLCLI_AGY_READY.', timeout=90)
            try:
                self.assertEqual(__import__('test_dispatch').run_dispatched(c.store.path, task['id']), 0)
                first = c.status(client, task['id'])
                first_answers = '\n'.join(e['text'] for e in c.events(client, task['id']) if e['kind']=='answer')
                self.assertIn('CALLCLI_AGY_READY', first_answers)
                c.intervene(client, task['id'], 'Do not use tools. Reply exactly CALLCLI_AGY_CONTINUED.')
                self.assertEqual(__import__('test_dispatch').run_dispatched(c.store.path, task['id']), 0)
                last = c.status(client, task['id'])
                self.assertEqual(first['acpx_record_id'], last['acpx_record_id'])
                events = c.events(client, task['id'])
                self.assertEqual(sum(e['kind']=='version' for e in events), 1)
                selected = [json.loads(e['text']) for e in events if e['kind']=='model']
                self.assertEqual(len(selected), 2)
                self.assertTrue(all(s['model']==model for s in selected))
                answers = '\n'.join(e['text'] for e in events if e['kind']=='answer')
                self.assertIn('CALLCLI_AGY_READY', answers)
                self.assertIn('CALLCLI_AGY_CONTINUED', answers)
                result = control(last, 'show')
                self.assertEqual(result.returncode, 0)
                state = json.loads(result.stdout)['acpx']
                self.assertEqual(state['current_model_id'], model)
                if effort:
                    self.assertEqual(next(o['currentValue'] for o in state['config_options'] if o['id']=='thought_level'), effort)
            finally:
                self.assertEqual(c.close(client, task['id'])['state'], 'closed')

    def test_running_intervention_cancels_before_same_session_revision(self):
        model = os.environ['CALLCLI_AGY_MODEL']
        effort = os.environ.get('CALLCLI_AGY_EFFORT')
        with probe_workspace() as root:
            work = Path(root)
            nonce = uuid.uuid4().hex
            (work/'input.txt').write_text(nonce)
            workers = []
            def launch(task):
                workers.append(subprocess.Popen([
                    __import__('sys').executable, '-B', str(Path(__file__).with_name('runner.py')),
                    '--db', str(work/'tasks.db'), '--task', task['id']],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True))
            c = Collaboration(work/'tasks.db', launch=launch)
            client = c.connect('codex')['client_id']  # coordinator handle, not native host provenance
            task = c.start(client, agent='agy', model=model, effort=effort, cwd=root,
                           write=True, timeout=180, prompt=(
                'Controlled runtime-intervention probe. Only this temporary cwd is writable. '
                'Emit CALLCLI_STAGE AGY_ACTIVE_GATE. Immediately run this foreground terminal command: '
                'python3 -c "from pathlib import Path; import time; '
                "Path('old-started.txt').write_text('OLD_RUNNING'); time.sleep(5)\". "
                'Then repeatedly run foreground 5-second sleeps until release.txt exists. '
                'Do not end the turn, background commands, or create release.txt. '
                'Only after release.txt exists write old-result.txt containing OLD_FINISHED and end. '
                'The coordinator will cancel this running turn.'))
            tid = task['id']
            def await_state(predicate, seconds=90):
                deadline = time.monotonic()+seconds
                while time.monotonic() < deadline:
                    current = c.status(client, tid)
                    if predicate(current):
                        return current
                    if current['state'] in {'unknown', 'completed', 'cancelled', 'waiting_reply'}:
                        self.fail('Unexpected settlement: '+repr(current['state']))
                    time.sleep(.1)
                self.fail('Timed out waiting for controlled AGY probe')
            try:
                def ready(current):
                    events = c.events(client, tid)
                    return (current['state']=='running' and (work/'old-started.txt').exists()
                            and any(e['kind']=='stage' and e['text']=='AGY_ACTIVE_GATE' for e in events)
                            and any(e['kind']=='tool' and 'in_progress' in e['text'] for e in events))
                before = await_state(ready)
                self.assertFalse((work/'old-result.txt').exists())
                pending = c.intervene(client, tid, (
                    'Runtime intervention replaces old instructions. Stop polling; never create old-result.txt. '
                    'Preserve old-started.txt. Read input.txt and write revised-result.txt containing exactly '
                    'REVISED: followed by its full content, no extra text. Read revised-result.txt back with '
                    'a tool and report its exact content and CALLCLI_STAGE AGY_REVISION_VERIFIED. '
                    'Only this temporary cwd is writable.'), command_id='runtime-revision')
                self.assertEqual((pending['state'], pending['turn']), ('cancelling', 1))
                settled = await_state(lambda t: t['state']=='cancelled')
                self.assertEqual((settled['exit_code'], settled['stop_reason']), (0, 'cancelled'))
                deadline = time.monotonic()+5
                while True:
                    advanced = c.wait(client, tid, timeout=0)
                    resumed = advanced['task']
                    if not advanced['busy'] and resumed and resumed['turn']==2:
                        break
                    self.assertLess(time.monotonic(), deadline, 'Intervention advancement stayed busy')
                    time.sleep(.05)
                self.assertEqual(resumed['turn'], 2)
                final = await_state(lambda t: t['state']=='completed')
                self.assertEqual(final['acpx_record_id'], before['acpx_record_id'])
                self.assertEqual(final['session'], before['session'])
                self.assertEqual((work/'revised-result.txt').read_text(), 'REVISED:'+nonce)
                self.assertEqual((work/'old-started.txt').read_text(), 'OLD_RUNNING')
                self.assertFalse((work/'old-result.txt').exists())
                answers = '\n'.join(e['text'] for e in c.events(client, tid) if e['kind']=='answer' and e['turn']==2)
                self.assertIn('REVISED:'+nonce, answers)
            finally:
                current = c.status(client, tid)
                if current['state'] in {'queued', 'running', 'cancelling'}:
                    c.cancel(client, tid)
                    deadline = time.monotonic()+35
                    while time.monotonic()<deadline and c.status(client, tid)['state'] in {'queued','running','cancelling'}:
                        time.sleep(.1)
                self.assertEqual(c.close(client, tid)['state'], 'closed')
                for worker in workers:
                    worker.wait(timeout=15)
