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

from collab import Collaboration
from runner import run_task


class DynamicTests(unittest.TestCase):
    def test_explicit_mode_persists_before_launch_and_conflicts_never_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            launches = []
            c = Collaboration(Path(directory)/'tasks.db', launch=launches.append)
            client = c.connect('omp')['client_id']
            for fields in (dict(agy_mode='dynamic', model='4as'),
                           dict(agy_mode='dynamic', agy_swarm='off'),
                           dict(agy_mode='dynamic', agy_worker_model='gemini-4-hydrogen'),
                           dict(agy_mode='teamwork')):
                with self.subTest(fields=fields), self.assertRaises(ValueError):
                    c.start(client, agent='agy', cwd=directory, prompt='never launch', **fields)
            self.assertEqual(launches, [])
            task = c.start(client, agent='agy', cwd=directory, prompt='workflow',
                           model='gemini-4-argon', agy_mode='dynamic')
            self.assertEqual((task['agy_mode'], task['agy_swarm'], task['model']),
                             ('dynamic', 'native', 'gemini-4-argon'))
            self.assertEqual(launches[0]['agy_mode'], 'dynamic')


class DynamicLifecycleTests(unittest.TestCase):
    @contextlib.contextmanager
    def workspace(self, fault='normal', surface='generic', transform=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'agy'
            source = (Path(__file__).parent/'fixtures/agy_native_agent.py').read_text()
            executable.write_text('#!'+sys.executable+'\n'+(transform(source) if transform else source))
            executable.chmod(0o700)
            with patch.dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'],
                    AGY_NATIVE_FIXTURE=directory, AGY_DYNAMIC_FIXTURE=fault), \
                    patch('updates.DEFAULT_LOCK_ROOT', root/'locks'), \
                    patch('updates.ensure_current', return_value={'decision':'fixture'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                if surface=='hermes':
                    from test_plugin import load_plugin, Ctx
                    mod = load_plugin()
                    env = {'HERMES_SESSION_ID':'owned', 'HERMES_SESSION_KEY':'owned-key'}
                    host = mod.Coordinator(Ctx(), root, lambda k,d='':env.get(k,d))
                    handle = host.handle
                    db = root/'call-cli/tasks.sqlite3'
                elif surface=='omp':
                    from omp_bridge import HostBridge
                    host = HostBridge(root/'tasks.db', launch=lambda task: None)
                    identity = dict(session_id='owned', agent_id='main')
                    key = host.bind(identity)
                    handle = lambda args: host.handle(identity, key, args)
                    db = host.store.path
                else:
                    host = Collaboration(root/'tasks.db', launch=lambda task: None)
                    key = host.connect('omp')['client_id']
                    def handle(args):
                        args = dict(args)
                        action = args.pop('action')
                        if action=='events': return dict(events=host.events(key, **args))
                        return dict(task=getattr(host, action)(key, **args))
                    db = host.store.path
                task = handle(dict(action='start', agent='agy', cwd=directory, prompt='synthetic workflow',
                    agy_mode='dynamic', model='gemini-4-argon', effort='low', write=True))['task']
                thread = threading.Thread(target=lambda: run_task(db, task['id'], turn=task['turn'],
                    launch_id=task['launch_id'], epoch=task['control_epoch']))
                thread.start()
                def status(): return handle(dict(action='status', task_id=task['id']))['task']
                def events(): return handle(dict(action='events', task_id=task['id']))['events']
                def await_condition(predicate):
                    deadline = time.monotonic()+6
                    while time.monotonic()<deadline:
                        if predicate(): return
                        time.sleep(.01)
                    self.fail('Dynamic condition missing: '+repr(status()))
                try:
                    yield root, handle, task, thread, status, events, await_condition
                finally:
                    if thread.is_alive():
                        handle(dict(action='cancel', task_id=task['id']))
                        (root/'workflow-release').touch()
                        thread.join(6)
                    self.assertFalse(thread.is_alive(), 'Synthetic workflow owner still live')
    def test_exact_script_permission_returns_to_origin_on_every_host(self):
        for surface in ('generic', 'hermes', 'omp'):
            with self.subTest(surface=surface), self.workspace(surface=surface) as (root, handle, task, thread, status, events, await_condition):
                await_condition(lambda: status().get('native_permission', {}).get('status')=='pending')
                permission = status()['native_permission']
                self.assertEqual(permission['script'], 'print("synthetic")')
                self.assertTrue(any(e['kind']=='permission' for e in events()))
                args = dict(action='permission', task_id=task['id'], expected_turn=1, command_id='allow-script',
                    permission_id=permission['id'], script_sha256=permission['script_sha256'], decision='allow_once')
                with self.assertRaises((ValueError, PermissionError)):
                    handle(dict(args, script_sha256='wrong'))
                handle(args)
                handle(args)
                thread.join(6)
                self.assertFalse(thread.is_alive())
                self.assertEqual(status()['state'], 'completed', status().get('settlement_note'))
                self.assertEqual(status()['native_phase'], 'settled')
                self.assertEqual(status()['model'], 'gemini-4-argon-low')
                observation = status()['native_observation']
                self.assertEqual(observation['output_source'], 'parent-public-answer')
                self.assertEqual(observation['child_events'], 'unavailable')
                self.assertFalse(observation['all_descendants_proven_stopped'])
                answers = '\n'.join(e['text'] for e in events() if e['kind']=='answer')
                self.assertEqual(json.loads(answers)['A']['sum'], 5)
                self.assertTrue((root/'prompt').read_text().startswith('/run-workflow\n'))
                self.assertEqual(handle(dict(action='close', task_id=task['id']))['task']['state'], 'closed')


    def approve(self, handle, task, status, await_condition, decision='allow_once', command='approve'):
        await_condition(lambda: status().get('native_permission', {}).get('status')=='pending')
        entry = status()['native_permission']
        handle(dict(action='permission', task_id=task['id'], expected_turn=1, command_id=command,
            permission_id=entry['id'], script_sha256=entry['script_sha256'], decision=decision))

    def test_question_and_running_revision_are_serial_not_hot_steer(self):
        with self.workspace('question', surface='hermes') as (root, handle, task, thread, status, events, await_condition):
            await_condition(lambda: bool(status().get('questions')))
            question = status()['questions'][0]
            args = dict(action='dialogue', task_id=task['id'], prompt='use revised label',
                expected_turn=1, command_id='answer', question_id=question)
            handle(args)
            handle(args)
            self.approve(handle, task, status, await_condition)
            thread.join(6)
            self.assertEqual(status()['state'], 'completed', status().get('settlement_note'))
            self.assertEqual(status()['commands']['answer']['status'], 'returned')
            self.assertEqual(status()['questions'], [])
            self.assertIn('revised', '\n'.join(e['text'] for e in events() if e['kind']=='answer'))
            self.assertEqual(len([r for r in self.requests(root) if r.get('method')=='session/prompt']), 2)

    def requests(self, root):
        return [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]

    def test_cancel_during_active_workflow_retains_hold_and_never_borrows_swarm_cleanup(self):
        with self.workspace('block') as (root, handle, task, thread, status, events, await_condition):
            self.approve(handle, task, status, await_condition)
            await_condition(lambda: (root/'workflow-active').exists())
            handle(dict(action='dialogue', task_id=task['id'], prompt='queued only', expected_turn=1, command_id='queued'))
            self.assertEqual(status()['commands']['queued']['status'], 'accepted')
            handle(dict(action='cancel', task_id=task['id']))
            thread.join(6)
            self.assertEqual(status()['state'], 'unknown')
            self.assertNotIn('native_cleanup', status())
            self.assertEqual(handle(dict(action='close', task_id=task['id']))['task']['state'], 'unknown')
            requests = self.requests(root)
            self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests), 1)
            self.assertEqual(sum(r.get('method')=='session/close' for r in requests), 0)
            self.assertEqual(sum(r.get('method')=='session/cancel' for r in requests), 1)

    def test_failed_workflow_closes_exact_session_but_never_claims_business_success(self):
        with self.workspace('failed') as (root, handle, task, thread, status, events, await_condition):
            self.approve(handle, task, status, await_condition)
            thread.join(6)
            self.assertEqual(status()['state'], 'unknown')
            self.assertEqual(status()['native_phase'], 'unknown')
            self.assertEqual(status()['native_cleanup']['outcome'], 'unknown')
            self.assertEqual(handle(dict(action='close', task_id=task['id']))['task']['state'], 'closed')

    def test_cancel_pending_approval_and_foreign_or_malformed_permission_cannot_release(self):
        for fault in ('normal', 'foreign', 'bad-script'):
            with self.subTest(fault=fault), self.workspace(fault) as (root, handle, task, thread, status, events, await_condition):
                if fault=='normal':
                    await_condition(lambda: status().get('native_permission', {}).get('status')=='pending')
                    handle(dict(action='cancel', task_id=task['id']))
                thread.join(6)
                self.assertEqual(status()['state'], 'unknown')
                self.assertNotIn('native_cleanup', status())
                self.assertFalse(any(r.get('method')=='session/close' for r in self.requests(root)))

    def test_disconnect_or_unload_failure_never_creates_trusted_release(self):
        for fault in ('disconnect', 'close-fail', 'unapproved'):
            with self.subTest(fault=fault), self.workspace(fault) as (root, handle, task, thread, status, events, await_condition):
                if fault!='unapproved': self.approve(handle, task, status, await_condition)
                thread.join(6)
                self.assertEqual(status()['state'], 'unknown')
                self.assertNotIn('native_cleanup', status())
                self.assertEqual(handle(dict(action='close', task_id=task['id']))['task']['state'], 'unknown')

    def test_permission_reject_and_cli_exact_decision_do_not_implicitly_approve(self):
        import collab
        with self.workspace() as (root, handle, task, thread, status, events, await_condition):
            await_condition(lambda: status().get('native_permission', {}).get('status')=='pending')
            entry=status()['native_permission']
            c=Collaboration(root/'tasks.db', launch=lambda task: None)
            foreign=c.connect('omp')['client_id']
            with self.assertRaises(PermissionError):
                c.permission(foreign, task['id'], expected_turn=1, command_id='foreign', permission_id=entry['id'],
                    script_sha256=entry['script_sha256'], decision='allow_once')
            owner=c.store.get(task['id'])['origin']['session_id']
            args=['--db',str(root/'tasks.db'),'--client',owner,'permission','--task',task['id'],
                '--expected-turn','1','--command-id','deny','--permission-id',entry['id'],
                '--script-sha256',entry['script_sha256'],'--decision','reject_once']
            self.assertEqual(collab.main(args),0)
            thread.join(6)
            self.assertEqual(status()['state'],'unknown')
            self.assertEqual(status()['native_permission']['decision'],'reject_once')
            self.assertNotIn('native_cleanup',status())

    def test_async_parent_end_turn_keeps_owner_until_complete_replay(self):
        with self.workspace('async') as (root, handle, task, thread, status, events, await_condition):
            self.approve(handle, task, status, await_condition)
            await_condition(lambda: status().get('native_observation', {}).get('workflow_tools')==[{'id':'workflow','status':'in_progress'}])
            self.assertEqual(status()['state'],'running')
            self.assertTrue(thread.is_alive())
            (root/'workflow-release').touch()
            thread.join(6)
            self.assertEqual(status()['state'],'completed',status().get('settlement_note'))
            self.assertEqual(json.loads('\n'.join(e['text'] for e in events() if e['kind']=='answer'))['A']['sum'],5)
            self.assertEqual(sum(r.get('method')=='session/prompt' for r in self.requests(root)),1)

    def test_changed_permission_script_or_workflow_identity_stays_hold(self):
        for fault in ('mismatched-script','renamed-workflow','duplicate-option','changed-executed-script'):
            with self.subTest(fault=fault), self.workspace(fault) as (root, handle, task, thread, status, events, await_condition):
                if fault in {'renamed-workflow','changed-executed-script'}: self.approve(handle,task,status,await_condition)
                thread.join(6)
                self.assertEqual(status()['state'],'unknown')
                self.assertNotIn('native_cleanup',status())

    def test_dynamic_requires_advertised_slash_and_never_falls_back(self):
        with self.workspace('no-command') as (root, handle, task, thread, status, events, await_condition):
            thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(status()['state'], 'unknown')
            requests = [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
            self.assertFalse(any(r.get('method')=='session/prompt' for r in requests))

    def test_raw_script_conflict_sparse_rewrite_and_duplicate_creation_hold(self):
        changes = {
            'conflict': lambda s: s.replace("rawInput=dict(Script='changed' if fault=='changed-executed-script' else script),",
                "rawInput=dict(Script='UNAPPROVED SCRIPT'), args=dict(Script=script),"),
            'sparse': lambda s: s.replace("            if fault!='async':\n",
                "            update(dict(sessionUpdate='tool_call_update', toolCallId='workflow', rawInput=dict(Script='UNAPPROVED SCRIPT'), status='completed'))\n            if fault!='async':\n",1),
            'duplicate': lambda s: s.replace("            if fault!='async':\n",
                "            update(dict(sessionUpdate='tool_call', toolCallId='workflow', name='run_workflow', kind='other', rawInput=dict(Script=script),status='completed'))\n            if fault!='async':\n",1),
            'identity': lambda s: s.replace("            if fault!='async':\n",
                "            update(dict(sessionUpdate='tool_call_update', toolCallId='workflow', name='read_board', status='completed'))\n            if fault!='async':\n",1),
        }
        for case, transform in changes.items():
            with self.subTest(case=case), self.workspace(transform=transform) as (root, handle, task, thread, status, events, wait):
                self.approve(handle,task,status,wait)
                thread.join(6)
                self.assertEqual(status()['state'],'unknown')
                self.assertNotIn('native_cleanup',status())
                self.assertEqual(handle(dict(action='close',task_id=task['id']))['task']['state'],'unknown')

    def test_legal_sparse_workflow_update_preserves_approved_identity(self):
        def transform(s):
            return s.replace("            if fault!='async':\n",
                "            update(dict(sessionUpdate='tool_call_update', toolCallId='workflow', rawInput=dict(Script=script), status='completed'))\n            if fault!='async':\n",1)
        with self.workspace(transform=transform) as (root, handle, task, thread, status, events, wait):
            self.approve(handle,task,status,wait)
            thread.join(6)
            self.assertEqual(status()['state'],'completed',status().get('settlement_note'))

    def test_sparse_workflow_argument_fields_distinguish_omission_from_invalid_types(self):
        cases = [('omitted', 'status=\'completed\'', 'completed'),
                 ('title-only', 'title=\'run_workflow\'', 'completed')]
        for field in ('args','rawInput'):
            for label, value, expected in (
                    ('empty-object', '{}', 'completed'),
                    ('same-script', 'dict(Script=script)', 'completed'),
                    ('null', 'None', 'unknown'),
                    ('string', "'different synthetic script'", 'unknown'),
                    ('array', '[]', 'unknown'),
                    ('number', '123', 'unknown'),
                    ('boolean', 'False', 'unknown'),
                    ('null-script', 'dict(Script=None)', 'unknown'),
                    ('changed-script', "dict(Script='different synthetic script')", 'unknown')):
                cases.append((field+'-'+label, field+'='+value, expected))
        for label, payload, expected in cases:
            def transform(source):
                needle="            if fault!='async':\n"
                return source.replace(needle,
                    "            update(dict(sessionUpdate='tool_call_update',toolCallId='workflow',"+payload+"))\n"+needle,1)
            with self.subTest(case=label), self.workspace(transform=transform) as (root,handle,task,thread,status,events,wait):
                self.approve(handle,task,status,wait)
                thread.join(6)
                self.assertEqual(status()['state'],expected,status().get('settlement_note'))
                if expected=='unknown':
                    self.assertNotIn('native_cleanup',status())
                    self.assertFalse(any(r.get('method')=='session/close' for r in self.requests(root)))

    def test_pending_permission_blocks_seal_and_sealed_owner_refuses_admission(self):
        from bridge import Store
        with self.workspace() as (root, handle, task, thread, status, events, wait):
            wait(lambda: status().get('native_permission',{}).get('status')=='pending')
            store=Store(root/'tasks.db')
            binding=(task['id'],1,task['launch_id'],1)
            self.assertFalse(store.native_input_seal(*binding,0))
            self.approve(handle,task,status,wait)
            thread.join(6)
            self.assertEqual(status()['state'],'completed')
        from bridge import Origin
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'tasks.db')
            task=store.create(Origin('fixture','owned','owned'),'local','agy',directory,'synthetic',False)
            store.update(task['id'],state='running',launch_id='exact-launch',native_session_id='owned-session')
            binding=(task['id'],1,'exact-launch',1)
            store.native_input_open(*binding)
            self.assertTrue(store.native_input_seal(*binding,0))
            with self.assertRaisesRegex(ValueError,'admission unavailable'):
                store.native_permission_open(*binding,dict(id='sealed-request',script='print(9)',
                    script_sha256='unapproved',tool='late-workflow',session_id='owned-session'))
            self.assertNotIn('native_permission',store.get(task['id']))

    def test_close_phase_new_permission_or_tool_never_uses_stale_completion(self):
        permit = """        if os.environ.get('AGY_DYNAMIC_FIXTURE'):
            send(dict(id='late-permit', method='session/request_permission',params=dict(sessionId=session,toolCall=dict(toolCallId='late-workflow',title='run_workflow',kind='other',rawInput=dict(Script='LATE SCRIPT')),options=[dict(optionId='allow',kind='allow_once'),dict(optionId='deny',kind='reject_once')])))
            late=json.loads(sys.stdin.readline())
            (root/'late-permission-response.json').write_text(json.dumps(late))
"""
        activity = "        update(dict(sessionUpdate='tool_call',toolCallId='late-workflow',name='run_workflow',kind='other',rawInput=dict(Script='LATE SCRIPT'),status='in_progress'))\n"
        for case, extra in (('permission',permit),('execution',activity)):
            def transform(s):
                return s.replace("    elif method == 'session/close':\n", "    elif method == 'session/close':\n"+extra,1)
            with self.subTest(case=case), self.workspace(transform=transform) as (root, handle, task, thread, status, events, wait):
                self.approve(handle,task,status,wait)
                thread.join(1)
                late=status().get('native_permission',{})
                if late.get('status')=='pending':
                    handle(dict(action='permission',task_id=task['id'],expected_turn=1,command_id='late',
                        permission_id=late['id'],script_sha256=late['script_sha256'],decision='allow_once'))
                thread.join(6)
                self.assertEqual(status()['state'],'unknown')
                self.assertNotIn('native_cleanup',status())
                self.assertEqual(handle(dict(action='close',task_id=task['id']))['task']['state'],'unknown')
                if (root/'late-permission-response.json').exists():
                    response=json.loads((root/'late-permission-response.json').read_text())
                    self.assertNotEqual(response.get('result',{}).get('outcome',{}).get('optionId'),'allow')

    def test_load_human_wait_excludes_only_approval_time_from_rpc_budget(self):
        from agy_native import NativeOwner
        original=NativeOwner.request
        def request(owner,method,params,**kw):
            if method=='session/load': kw['timeout']=.2
            return original(owner,method,params,**kw)
        permit="""            if saved and not (root/'late-permission-response.json').exists():
                send(dict(id='late-permit',method='session/request_permission',params=dict(sessionId=session,toolCall=dict(toolCallId='late-workflow',title='run_workflow',kind='other',rawInput=dict(Script='LATE SCRIPT')),options=[dict(optionId='allow',kind='allow_once'),dict(optionId='deny',kind='reject_once')])))
                late=json.loads(sys.stdin.readline())
                (root/'late-permission-response.json').write_text(json.dumps(late))
"""
        def transform(s):
            return s.replace("        if os.environ.get('AGY_DYNAMIC_FIXTURE'):\n            if os.environ",
                "        if os.environ.get('AGY_DYNAMIC_FIXTURE'):\n"+permit+"            if os.environ",1)
        with patch.object(NativeOwner,'request',request), self.workspace(transform=transform) as (root,handle,task,thread,status,events,wait):
            self.approve(handle,task,status,wait)
            wait(lambda: status().get('native_permission',{}).get('status')=='pending')
            time.sleep(.4)
            self.approve(handle,task,status,wait,command='second')
            thread.join(6)
            self.assertEqual(status()['state'],'completed',status().get('settlement_note'))

    def test_command_advertisement_after_load_response_is_consumed_before_prompt(self):
        def transform(s):
            s=s.replace("if os.environ.get('AGY_DYNAMIC_FIXTURE') and os.environ['AGY_DYNAMIC_FIXTURE']!='no-command':",'if False:')
            return s.replace("send(dict(id=msg['id'], result=metadata()))\n            captured = None\n            continue",
                "send(dict(id=msg['id'], result=metadata()))\n            time.sleep(.05)\n            update(dict(sessionUpdate='available_commands_update', availableCommands=[dict(name='run-workflow')]))\n            captured = None\n            continue",1)
        with self.workspace(transform=transform) as (root,handle,task,thread,status,events,wait):
            self.approve(handle,task,status,wait)
            thread.join(6)
            self.assertEqual(status()['state'],'completed',status().get('settlement_note'))

    def test_async_answer_without_newline_finishes_before_unload(self):
        def transform(s):
            return s.replace('text=\'{"A":{"sum":5},"B":{"difference":3},"verified":true}\\n\'',
                             'text=\'{"A":{"sum":5},"B":{"difference":3},"verified":true}\'')
        with self.workspace('async',transform=transform) as (root,handle,task,thread,status,events,wait):
            self.approve(handle,task,status,wait)
            wait(lambda: bool(status().get('native_observation')))
            (root/'workflow-release').touch()
            thread.join(6)
            self.assertEqual(status()['state'],'completed',status().get('settlement_note'))
            self.assertEqual(json.loads('\n'.join(e['text'] for e in events() if e['kind']=='answer'))['A']['sum'],5)

    def test_other_parent_failure_requires_final_answer_after_failure(self):
        failure="                update(dict(sessionUpdate='tool_call',toolCallId='other',name='verify',kind='other',status='failed'))\n"
        for recovered in (True,False):
            def transform(s):
                if recovered:
                    return s.replace("if fault!='async':\n                text(","if fault!='async':\n"+failure+"                text(",1)
                return s.replace("            saved = captured\n            captured = None\n            send(dict(id=msg['id'], result=dict(stopReason='end_turn')))\n            continue",
                    failure[4:]+"            saved = captured\n            captured = None\n            send(dict(id=msg['id'], result=dict(stopReason='end_turn')))\n            continue",1)
            with self.subTest(recovered=recovered), self.workspace(transform=transform) as (root,handle,task,thread,status,events,wait):
                self.approve(handle,task,status,wait)
                thread.join(6)
                self.assertEqual(status()['state'],'completed' if recovered else 'unknown')
                if not recovered:
                    self.assertNotIn('native_cleanup',status())
                    self.assertFalse(any(r.get('method')=='session/close' for r in self.requests(root)))

    def test_load_communication_stall_after_approval_still_times_out(self):
        from agy_native import NativeOwner
        original=NativeOwner.request
        def request(owner,method,params,**kw):
            if method=='session/load': kw['timeout']=.2
            return original(owner,method,params,**kw)
        def transform(s):
            return s.replace("            for entry in saved:\n                if os.environ['AGY_DYNAMIC_FIXTURE']=='renamed-workflow'",
                "            if saved: time.sleep(.4)\n            for entry in saved:\n                if os.environ['AGY_DYNAMIC_FIXTURE']=='renamed-workflow'",1)
        with patch.object(NativeOwner,'request',request), self.workspace(transform=transform) as (root,handle,task,thread,status,events,wait):
            self.approve(handle,task,status,wait)
            thread.join(6)
            self.assertEqual(status()['state'],'unknown')
            self.assertIn('control did not settle',status().get('settlement_note',''))
            self.assertNotIn('native_cleanup',status())

if __name__ == '__main__':
    unittest.main()
