import contextlib
import hashlib
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
                           dict(agy_mode='dynamic', agy_worker_model='gemini-4-hydrogen')):
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
    def workspace(self, fault='normal', surface='generic', transform=None, agy_approval='approve-all'):
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
                start_args = dict(action='start', agent='agy', cwd=directory, prompt='synthetic workflow',
                    agy_mode='dynamic', model='gemini-4-argon', effort='low', write=True)
                if agy_approval is not None:
                    start_args['agy_approval'] = agy_approval
                task = handle(start_args)['task']
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
            st = status()
            self.assertEqual(st['state'], 'cancelled')
            self.assertEqual(st['commands']['queued']['status'], 'not_sent')
            self.assertEqual(st['native_cleanup']['kind'], 'verified-cancellation')
            self.assertIs(st['native_cleanup']['session_unloaded'], True)
            self.assertIs(st['native_cleanup']['all_descendants_proven_stopped'], False)
            self.assertEqual(handle(dict(action='close', task_id=task['id']))['task']['state'], 'closed')
            requests = self.requests(root)
            self.assertEqual(sum(r.get('method')=='session/prompt' for r in requests), 1)
            self.assertEqual(sum(r.get('method')=='session/cancel' for r in requests), 1)
            self.assertEqual(sum(r.get('method')=='session/close' for r in requests), 1)

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

    def test_default_skip_permissions_dynamic_runs_natively_across_hosts(self):
        for surface in ('generic', 'hermes', 'omp'):
            with self.subTest(surface=surface), self.workspace(surface=surface, agy_approval=None) as (root, handle, task, thread, status, events, _):
                self.assertEqual((task['agy_approval'], task['execution_policy']), ('skip-permissions', 'native-skip'))
                thread.join(6)
                self.assertFalse(thread.is_alive())
                argv = json.loads((root/'argv.json').read_text())
                self.assertIn('--dangerously-skip-permissions', argv)
                self.assertNotIn('native_permission', status())
                self.assertFalse(any(e['kind']=='permission' for e in events()))
                self.assertIn('Execute authorized workflows natively under skip-permissions', (root/'prompt').read_text())
                self.assertEqual(status()['state'], 'completed', status().get('settlement_note'))
                self.assertEqual(status()['native_phase'], 'settled')
                self.assertEqual(status()['native_cleanup']['outcome'], 'completed')
                self.assertEqual(handle(dict(action='close', task_id=task['id']))['task']['state'], 'closed')

    def test_skip_permissions_dynamic_unexpected_permission_and_guards(self):
        with self.workspace('force-permission', agy_approval=None) as (root, handle, task, thread, status, events, _):
            thread.join(6)
            self.assertEqual(status()['state'], 'completed', status().get('settlement_note'))
            self.assertNotIn('native_permission', status())
            self.assertEqual(handle(dict(action='close', task_id=task['id']))['task']['state'], 'closed')
        for fault in ('foreign', 'mismatched-script', 'duplicate-option'):
            with self.subTest(fault=fault), self.workspace(fault, agy_approval=None) as (root, handle, task, thread, status, _events, _wait):
                thread.join(6)
                self.assertEqual(status()['state'], 'unknown')
                self.assertNotIn('native_cleanup', status())
        def conflict(source):
            return source.replace("            if fault!='async':\n",
                "            update(dict(sessionUpdate='tool_call_update', toolCallId='workflow', rawInput=dict(Script='mutated'), status='completed'))\n            if fault!='async':\n", 1)
        with self.workspace(agy_approval=None, transform=conflict) as (root, handle, task, thread, status, _events, _wait):
            thread.join(6)
            self.assertEqual(status()['state'], 'unknown')
            self.assertNotIn('native_cleanup', status())

    def test_skip_permissions_dynamic_requires_real_script_identity_and_allows_sparse_after_identity(self):
        missing_old = "                rawInput=dict(Script='changed' if fault=='changed-executed-script' else script),\n"
        rejected = {
            'missing-script': lambda s: s.replace(missing_old, '', 1),
            'in-progress-then-sparse-completed': lambda s: (
                s.replace(missing_old, '', 1)
                 .replace("                status='in_progress' if fault=='async' else 'failed' if fault=='failed' else 'completed'))\n",
                          "                status='in_progress'))\n"
                          "            update(dict(sessionUpdate='tool_call_update', toolCallId='workflow', status='completed'))\n", 1)
            ),
            'in-progress-then-late-script': lambda s: (
                s.replace(missing_old, '', 1)
                 .replace("                status='in_progress' if fault=='async' else 'failed' if fault=='failed' else 'completed'))\n",
                          "                status='in_progress'))\n"
                          "            update(dict(sessionUpdate='tool_call_update', toolCallId='workflow', rawInput=dict(Script=script), status='completed'))\n", 1)
            ),
            'script-path': lambda s: s.replace(missing_old, "                rawInput=dict(ScriptPath='wf.py'),\n", 1),
            'empty-script': lambda s: s.replace(missing_old, "                rawInput=dict(Script=''),\n", 1),
        }
        for label, transform in rejected.items():
            with self.subTest(case=label), self.workspace(agy_approval=None, transform=transform) as (root, handle, task, thread, status, events, _wait):
                thread.join(6)
                final = status()
                self.assertFalse(thread.is_alive())
                self.assertEqual(final['state'], 'unknown', final.get('settlement_note'))
                self.assertEqual(sum(e['kind'] == 'permission' for e in events()), 0)
                self.assertNotIn('native_cleanup', final)
                self.assertFalse(any(r.get('method') == 'session/close' for r in self.requests(root)))
                self.assertEqual(handle(dict(action='close', task_id=task['id']))['task']['state'], 'unknown')

        for suffix_case, extra_raw in (('omitted', ''), ('empty-obj', ', rawInput={}')):
            def valid_sparse(s):
                return (s.replace("                status='in_progress' if fault=='async' else 'failed' if fault=='failed' else 'completed'))\n",
                                  "                status='in_progress'))\n"
                                  "            update(dict(sessionUpdate='tool_call_update', toolCallId='workflow', status='completed'" + extra_raw + "))\n", 1))
            with self.subTest(sparse=suffix_case), self.workspace(agy_approval=None, transform=valid_sparse) as (root, handle, task, thread, status, events, _wait):
                thread.join(6)
                final = status()
                self.assertFalse(thread.is_alive())
                self.assertEqual(final['state'], 'completed', final.get('settlement_note'))
                self.assertEqual(sum(e['kind'] == 'permission' for e in events()), 0)
                self.assertEqual(final['native_cleanup']['outcome'], 'completed')
                self.assertTrue(final['native_cleanup']['session_unloaded'])
                self.assertEqual(handle(dict(action='close', task_id=task['id']))['task']['state'], 'closed')

    def test_cancel_during_async_observation_and_unverified_cancel_hold(self):
        # Pure cancel during dynamic_pending observation settles cancelled with verified unload
        with self.workspace('async') as (root, handle, task, thread, status, _events, await_condition):
            self.approve(handle, task, status, await_condition)
            await_condition(lambda: (
                status().get('native_phase') == 'dynamic_pending'
                and status().get('native_observation', {}).get('workflow_tools') == [{'id': 'workflow', 'status': 'in_progress'}]
            ))
            handle(dict(action='cancel', task_id=task['id']))
            thread.join(6)
            self.assertFalse(thread.is_alive())
            st = status()
            self.assertEqual(st['state'], 'cancelled')
            self.assertEqual(st['native_cleanup']['kind'], 'verified-cancellation')
            self.assertIs(st['native_cleanup']['session_unloaded'], True)
            self.assertIs(st['native_cleanup']['all_descendants_proven_stopped'], False)
            self.assertFalse(st['native_cancel']['resumable'])
            self.assertEqual(handle(dict(action='close', task_id=task['id']))['task']['state'], 'closed')
            requests = self.requests(root)
            self.assertEqual(sum(r.get('method') == 'session/prompt' for r in requests), 1)
            self.assertEqual(sum(r.get('method') == 'session/cancel' for r in requests), 1)
            self.assertEqual(sum(r.get('method') == 'session/close' for r in requests), 1)

        # Unverified cancel: ACP disconnect on cancel or close-fail on cancel retains unknown HOLD & writer lock
        unverified_cases = {
            'disconnect-on-cancel-block': (
                'block',
                lambda s: s.replace(
                    "                    with (root/'requests.jsonl').open('a') as f:\n"
                    "                        f.write(json.dumps(pending)+'\\n')\n"
                    "                    if pending.get('method')=='session/cancel': break",
                    "                    with (root/'requests.jsonl').open('a') as f:\n"
                    "                        f.write(json.dumps(pending)+'\\n')\n"
                    "                    if pending.get('method')=='session/cancel': raise SystemExit(7)",
                    1,
                ),
            ),
            'disconnect-on-cancel-async': (
                'async',
                lambda s: s.replace(
                    "    elif method == 'session/cancel':\n        (root/'cancelled').touch()\n        continue",
                    "    elif method == 'session/cancel':\n        (root/'cancelled').touch()\n        raise SystemExit(7)",
                    1,
                ),
            ),
            'close-fail-on-cancel-async': (
                'async',
                lambda s: s.replace(
                    "if os.environ.get('AGY_DYNAMIC_FIXTURE')=='close-fail':",
                    "if True:",
                    1,
                ),
            ),
        }
        for label, (fault, transform) in unverified_cases.items():
            with self.subTest(case=label), self.workspace(fault, transform=transform) as (root, handle, task, thread, status, _events, await_condition):
                self.approve(handle, task, status, await_condition)
                if fault == 'block':
                    await_condition(lambda: (root / 'workflow-active').exists())
                else:
                    await_condition(lambda: status().get('native_phase') == 'dynamic_pending')
                handle(dict(action='cancel', task_id=task['id']))
                thread.join(6)
                self.assertFalse(thread.is_alive())
                st = status()
                self.assertEqual(st['state'], 'unknown')
                self.assertNotIn('native_cleanup', st)
                self.assertFalse(st.get('native_cancel', {}).get('resumable'))
                self.assertTrue(st.get('native_cancel', {}).get('write_lock_retained'))
                with self.assertRaisesRegex(ValueError, 'already owned'):
                    handle(dict(action='start', agent='omp', cwd=str(root), prompt='conflicting writer', write=True))
                self.assertEqual(handle(dict(action='close', task_id=task['id']))['task']['state'], 'unknown')

    def _run_resumed_dynamic_case(self, *, agy_approval, mode='normal'):
        fake_agy = '''import json, os, sys
from pathlib import Path
state = Path(os.environ['AGY_DYN_RESUME_STATE'])
mode = os.environ.get('AGY_DYN_RESUME_MODE', 'normal')
sid = 'dyn-resume-session-1'
model = 'gemini-4-argon'
effort = 'low'
closed = False
is_resume = False
loads = 0
history_file = state / 'history.json'
history = json.loads(history_file.read_text()) if history_file.exists() else []
t2_script = 'print("turn2-verified")'

def send(obj):
    sys.stdout.write(json.dumps(obj) + '\\n')
    sys.stdout.flush()

def notify(u):
    send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': sid, 'update': u}})

def meta():
    models_list = [{'modelId': 'gemini-4-argon-low'}, {'modelId': 'gemini-4-argon-medium'}]
    model_opts = [{'value': 'gemini-4-argon'}]
    if mode == 'reset-config-on-resume':
        models_list.append({'modelId': 'gemini-4-carbon-medium'})
        model_opts.insert(0, {'value': 'gemini-4-carbon'})
    return {
        'sessionId': sid,
        'models': {
            'currentModelId': model + '-' + effort,
            'availableModels': models_list,
        },
        'configOptions': [
            {'id': 'model', 'currentValue': model, 'options': model_opts},
            {'id': 'thought_level', 'currentValue': effort, 'options': [{'value': 'low'}, {'value': 'medium'}]},
        ],
    }

def cfg_result():
    if mode == 'reset-config-on-resume':
        return {'configOptions': meta()['configOptions']}
    return meta()

for line in sys.stdin:
    if not line.strip():
        continue
    req = json.loads(line)
    rid, method, params = req.get('id'), req.get('method'), req.get('params', {})
    with (state / 'rpcs.jsonl').open('a') as f:
        f.write(json.dumps({'pid': os.getpid(), 'method': method, 'params': params}) + '\\n')
    if method == 'initialize':
        send({'jsonrpc': '2.0', 'id': rid, 'result': {
            'protocolVersion': 1,
            'agentCapabilities': {'loadSession': True, 'sessionCapabilities': {'close': {}, 'resume': {}}},
        }})
    elif method == 'session/new':
        is_resume = False
        (state / 't1_pid').write_text(str(os.getpid()))
        notify({'sessionUpdate': 'available_commands_update', 'availableCommands': [{'name': 'run-workflow'}]})
        send({'jsonrpc': '2.0', 'id': rid, 'result': meta()})
    elif method == 'session/resume':
        is_resume = True
        if mode == 'reset-config-on-resume':
            model = 'gemini-4-carbon'
            effort = 'medium'
        (state / 't2_pid').write_text(str(os.getpid()))
        (state / 'resumed_sid').write_text(str(params.get('sessionId')))
        (state / 'loads_at_resume').write_text(str(loads))
        send({'jsonrpc': '2.0', 'id': rid, 'result': meta()})
    elif method == 'session/set_config_option':
        if closed:
            send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32602, 'message': 'Session not found: ' + sid}})
        else:
            if params.get('configId') == 'model':
                model = params.get('value')
            elif params.get('configId') == 'thought_level':
                effort = params.get('value')
            send({'jsonrpc': '2.0', 'id': rid, 'result': cfg_result()})
    elif method == 'session/load':
        loads += 1
        for entry in history:
            notify(entry)
        send({'jsonrpc': '2.0', 'id': rid, 'result': meta()})
    elif method == 'session/prompt':
        ptext = ''.join(p.get('text', '') for p in params.get('prompt', []))
        with (state / 'prompts.jsonl').open('a') as f:
            f.write(json.dumps({'is_resume': is_resume, 'text': ptext}) + '\\n')
        if not is_resume:
            if '--dangerously-skip-permissions' not in sys.argv:
                send({'jsonrpc': '2.0', 'id': 'permit-t1', 'method': 'session/request_permission', 'params': {
                    'sessionId': sid,
                    'toolCall': {'toolCallId': 'workflow-t1', 'title': 'run_workflow', 'kind': 'other',
                                 'rawInput': {'Script': 'print("turn1")'}},
                    'options': [{'optionId': 'allow', 'kind': 'allow_once'}, {'optionId': 'deny', 'kind': 'reject_once'}],
                }})
                perm = json.loads(sys.stdin.readline())
                if perm.get('result', {}).get('outcome', {}).get('optionId') != 'allow':
                    send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'end_turn'}})
                    continue
            notify({'sessionUpdate': 'tool_call', 'toolCallId': 'workflow-t1', 'name': 'run_workflow',
                    'kind': 'other', 'rawInput': {'Script': 'print("turn1")'}, 'status': 'in_progress'})
            (state / 't1_active').touch()
            for pending_line in sys.stdin:
                pending = json.loads(pending_line)
                with (state / 'rpcs.jsonl').open('a') as f:
                    f.write(json.dumps({'pid': os.getpid(), 'method': pending.get('method'), 'params': pending.get('params', {})}) + '\\n')
                if pending.get('method') == 'session/cancel':
                    break
            # Simulate Turn 1 persisted history omitting rawInput on Turn 1 workflow entry.
            history.extend([
                {'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': ptext}},
                {'sessionUpdate': 'tool_call', 'toolCallId': 'workflow-t1', 'name': 'run_workflow',
                 'kind': 'other', 'status': 'completed'},
            ])
            if mode == 'predecessor-fatal':
                history.append({'sessionUpdate': 'agent_message_chunk',
                                'content': {'type': 'text', 'text': '\\u26a0 Agent execution terminated due to error.\\n'}})
            history_file.write_text(json.dumps(history))
            send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'cancelled'}})
        else:
            (state / 'loads_before_turn2_prompt').write_text(str(loads))
            (state / 'turn2_selection').write_text(json.dumps({'model': model, 'effort': effort}))
            if '--dangerously-skip-permissions' not in sys.argv:
                send({'jsonrpc': '2.0', 'id': 'permit-t2', 'method': 'session/request_permission', 'params': {
                    'sessionId': sid,
                    'toolCall': {'toolCallId': 'workflow-t2', 'title': 'run_workflow', 'kind': 'other',
                                 'rawInput': {'Script': t2_script}},
                    'options': [{'optionId': 'allow', 'kind': 'allow_once'}, {'optionId': 'deny', 'kind': 'reject_once'}],
                }})
                perm = json.loads(sys.stdin.readline())
                if perm.get('result', {}).get('outcome', {}).get('optionId') != 'allow':
                    send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'end_turn'}})
                    continue
            if mode == 'missing-script':
                notify({'sessionUpdate': 'tool_call', 'toolCallId': 'workflow-t2', 'name': 'run_workflow',
                        'kind': 'other', 'rawInput': {}, 'status': 'completed'})
            elif mode == 'missing-replay-script':
                notify({'sessionUpdate': 'tool_call', 'toolCallId': 'workflow-t2', 'name': 'run_workflow',
                        'kind': 'other', 'rawInput': {'Script': t2_script}, 'status': 'in_progress'})
            else:
                notify({'sessionUpdate': 'tool_call', 'toolCallId': 'workflow-t2', 'name': 'run_workflow',
                        'kind': 'other', 'rawInput': {'Script': t2_script}, 'status': 'completed'})
            if mode == 'current-foreground-fatal':
                notify({'sessionUpdate': 'agent_message_chunk',
                        'content': {'type': 'text', 'text': '\\u26a0 Agent execution terminated due to error.\\n'}})
            notify({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': '{"turn":2,"ok":true}\\n'}})
            if mode != 'missing-boundary':
                history.append({'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': ptext}})
            if mode == 'mutated-script':
                replay_raw = {'Script': 'print("TAMPERED")'}
            elif mode in {'missing-script', 'missing-replay-script'}:
                replay_raw = {}
            else:
                replay_raw = {'Script': t2_script}
            history.append({'sessionUpdate': 'tool_call', 'toolCallId': 'workflow-t2', 'name': 'run_workflow',
                            'kind': 'other', 'rawInput': replay_raw, 'status': 'completed'})
            if mode == 'current-replay-fatal':
                history.append({'sessionUpdate': 'agent_message_chunk',
                                'content': {'type': 'text', 'text': '\\u26a0 Agent execution terminated due to error.\\n'}})
            history.append({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': '{"turn":2,"ok":true}\\n'}})
            history_file.write_text(json.dumps(history))
            send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'end_turn'}})
    elif method == 'session/close':
        closed = True
        send({'jsonrpc': '2.0', 'id': rid, 'result': {}})
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / 'state'
            state_dir.mkdir()
            exe = root / 'agy'
            exe.write_text('#!' + sys.executable + '\n' + fake_agy)
            exe.chmod(0o700)
            with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                            AGY_DYN_RESUME_STATE=str(state_dir), AGY_DYN_RESUME_MODE=mode), \
                    patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                    patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                client = c.connect('hermes')['client_id']
                start_kwargs = dict(agent='agy', cwd=directory, prompt='turn1 dynamic workflow',
                                    agy_mode='dynamic', model='gemini-4-argon', effort='low', write=True)
                if agy_approval is not None:
                    start_kwargs['agy_approval'] = agy_approval
                task = c.start(client, **start_kwargs)
                rc1, rc2 = [], []
                t1 = threading.Thread(target=lambda: rc1.append(
                    run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)))
                t2 = None
                t1.start()
                try:
                    deadline = time.monotonic() + 6
                    if agy_approval == 'approve-all':
                        while not c.status(client, task['id']).get('native_permission', {}).get('status') == 'pending' and time.monotonic() < deadline:
                            time.sleep(0.01)
                        perm1 = c.status(client, task['id'])['native_permission']
                        self.assertEqual(perm1['status'], 'pending')
                        c.permission(client, task['id'], expected_turn=1, command_id='allow-t1',
                                     permission_id=perm1['id'], script_sha256=perm1['script_sha256'], decision='allow_once')
                    while not (state_dir / 't1_active').exists() and t1.is_alive() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue((state_dir / 't1_active').exists())
                    c.intervene(client, task['id'], 'revised turn2 dynamic goal', command_id='iv-t2', expected_turn=1)
                    t1.join(6)
                    self.assertFalse(t1.is_alive())
                    self.assertEqual(rc1, [0])
                    st1 = c.status(client, task['id'])
                    self.assertEqual(st1['state'], 'cancelled')
                    self.assertIs(st1['native_cancel']['resumable'], True)
                    self.assertIs(st1['native_cancel']['session_unloaded'], False)
                    self.assertIs(st1['native_cancel']['write_lock_retained'], True)
                    self.assertEqual(st1['native_resume_session_id'], 'dyn-resume-session-1')
                    with self.assertRaisesRegex(ValueError, 'already owned'):
                        c.start(client, agent='omp', cwd=directory, prompt='conflicting writer', write=True)
                    waited = c.wait(client, task['id'], timeout=0.1)
                    self.assertEqual(waited['task']['turn'], 2)
                    t2_task = c.store.get(task['id'])
                    t2 = threading.Thread(target=lambda: rc2.append(
                        run_task(c.store.path, task['id'], turn=2, launch_id=t2_task['launch_id'],
                                 epoch=t2_task.get('control_epoch', 1))))
                    t2.start()
                    if agy_approval == 'approve-all':
                        deadline2 = time.monotonic() + 6
                        while (c.status(client, task['id']).get('native_permission', {}).get('status') != 'pending'
                               and t2.is_alive() and time.monotonic() < deadline2):
                            time.sleep(0.01)
                        perm2 = c.status(client, task['id']).get('native_permission', {})
                        if perm2.get('status') == 'pending':
                            expected_sha = hashlib.sha256(b'print("turn2-verified")').hexdigest()
                            self.assertEqual(perm2['script_sha256'], expected_sha)
                            c.permission(client, task['id'], expected_turn=2, command_id='allow-t2',
                                         permission_id=perm2['id'], script_sha256=perm2['script_sha256'], decision='allow_once')
                    t2.join(6)
                    self.assertFalse(t2.is_alive())
                    st2 = c.status(client, task['id'])
                    rpcs = [json.loads(ln) for ln in (state_dir / 'rpcs.jsonl').read_text().splitlines()]
                    t1_pid = int((state_dir / 't1_pid').read_text())
                    t2_pid = int((state_dir / 't2_pid').read_text())
                    prompts = [json.loads(ln) for ln in (state_dir / 'prompts.jsonl').read_text().splitlines()]
                    if mode in {'normal', 'predecessor-fatal', 'reset-config-on-resume'}:
                        self.assertEqual(rc2, [0])
                        self.assertEqual(st2['state'], 'completed', st2.get('settlement_note'))
                        self.assertEqual(st2['turn'], 2)
                        self.assertEqual(st2['native_phase'], 'settled')
                        self.assertIs(st2['native_cleanup']['session_unloaded'], True)
                        self.assertEqual(st2['native_cleanup']['outcome'], 'completed')
                        self.assertEqual(st2['native_observation']['workflow_tools'],
                                         [{'id': 'workflow-t2', 'status': 'completed'}])
                        self.assertEqual(json.loads(st2['native_result']['result']), {'turn': 2, 'ok': True})
                        self.assertEqual(json.loads((state_dir / 'turn2_selection').read_text()),
                                         {'model': 'gemini-4-argon', 'effort': 'low'})
                        self.assertEqual((state_dir / 'resumed_sid').read_text(), 'dyn-resume-session-1')
                        self.assertEqual((state_dir / 'loads_at_resume').read_text(),
                                         (state_dir / 'loads_before_turn2_prompt').read_text())
                        t2_rpcs = [r for r in rpcs if r['pid'] == t2_pid]
                        self.assertEqual(sum(r['method'] == 'session/resume' for r in t2_rpcs), 1)
                        self.assertEqual(sum(r['method'] == 'session/new' for r in t2_rpcs), 0)
                        self.assertEqual(sum(r['method'] == 'session/close' for r in t2_rpcs), 1)
                        self.assertEqual(sum(r['method'] == 'session/close' for r in rpcs if r['pid'] == t1_pid), 0)
                        t2_prompts = [p for p in prompts if p['is_resume']]
                        self.assertEqual(len(t2_prompts), 1)
                        self.assertIn('revised turn2 dynamic goal', t2_prompts[0]['text'])
                        self.assertNotIn('turn1 dynamic workflow', t2_prompts[0]['text'])
                        self.assertEqual(c.close(client, task['id'])['state'], 'closed')
                    else:
                        self.assertEqual(rc2, [1])
                        self.assertEqual(st2['state'], 'unknown')
                        self.assertNotIn('native_cleanup', st2)
                        t2_rpcs = [r for r in rpcs if r['pid'] == t2_pid]
                        self.assertEqual(sum(r['method'] == 'session/close' for r in t2_rpcs), 0)
                        with self.assertRaisesRegex(ValueError, 'already owned'):
                            c.start(client, agent='omp', cwd=directory, prompt='conflicting writer after negative', write=True)
                        self.assertEqual(c.close(client, task['id'])['state'], 'unknown')
                finally:
                    if t1.is_alive():
                        c.cancel(client, task['id'])
                        t1.join(3)
                    if t2 is not None and t2.is_alive():
                        c.cancel(client, task['id'])
                        t2.join(3)

    def test_write_active_workflow_intervene_resumes_exact_session_on_turn2(self):
        for approval in ('approve-all', None):
            with self.subTest(agy_approval=approval or 'skip-permissions'):
                self._run_resumed_dynamic_case(agy_approval=approval, mode='normal')

    def test_resumed_dynamic_restores_reset_coordinator_selection_without_preload(self):
        self._run_resumed_dynamic_case(agy_approval=None, mode='reset-config-on-resume')

    def test_resumed_dynamic_turn2_script_identity_negative_holds_without_cleanup(self):
        cases = (
            ('missing-script', None),
            ('mutated-script', 'approve-all'),
            ('missing-replay-script', 'approve-all'),
            ('missing-boundary', None),
        )
        for mode, approval in cases:
            with self.subTest(mode=mode):
                self._run_resumed_dynamic_case(agy_approval=approval, mode=mode)

    def test_resumed_dynamic_predecessor_fatal_ignored_and_current_fatal_holds(self):
        for mode in ('predecessor-fatal', 'current-foreground-fatal', 'current-replay-fatal'):
            with self.subTest(mode=mode):
                self._run_resumed_dynamic_case(agy_approval=None, mode=mode)

    def test_dynamic_retry_salvage_provenance_and_interaction_dedupe(self):
        salvage_shim = r"""#!/usr/bin/env python3
import json, os, sys
SID = 'dyn-salvage-session-1'
mode = os.environ['AGY_DYN_SALVAGE_MODE']
model = 'gemini-4-argon'
effort = 'low'
closed = False
prompted = False
loads = 0
post_dlg_loads = 0
saved = []

def send(obj):
    sys.stdout.write(json.dumps(dict(jsonrpc='2.0', **obj)) + '\n')
    sys.stdout.flush()

def emit(u):
    send(dict(method='session/update', params=dict(sessionId=SID, update=u)))

def meta():
    return {
        'sessionId': SID,
        'models': {
            'currentModelId': model + '-' + effort,
            'availableModels': [{'modelId': 'gemini-4-argon-low'}],
        },
        'configOptions': [
            {'id': 'model', 'currentValue': model, 'options': [{'value': model}]},
            {'id': 'thought_level', 'currentValue': effort, 'options': [{'value': effort}]},
        ],
    }

for line in sys.stdin:
    if not line.strip():
        continue
    req = json.loads(line)
    rid, m, params = req.get('id'), req.get('method'), req.get('params', {})
    if m == 'initialize':
        send(dict(id=rid, result=dict(
            protocolVersion=1,
            agentCapabilities=dict(loadSession=True, sessionCapabilities=dict(close={}, resume={})),
        )))
    elif m == 'session/new':
        send(dict(id=rid, result=meta()))
        emit(dict(sessionUpdate='available_commands_update', availableCommands=[dict(name='run-workflow')]))
    elif m == 'session/set_config_option':
        if closed:
            send(dict(id=rid, error=dict(code=-32602, message='Session not found')))
        else:
            if params.get('configId') == 'model':
                model = params.get('value')
            elif params.get('configId') == 'thought_level':
                effort = params.get('value')
            send(dict(id=rid, result=meta()))
    elif m == 'session/prompt':
        prompted = True
        body = ''.join(p.get('text', '') for p in params.get('prompt', []))
        if not body.startswith('CALLCLI_DIALOGUE '):
            wf_status = 'completed' if mode == 'drained-across-interactions' else 'in_progress'
            saved = [
                dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)),
                dict(sessionUpdate='tool_call', toolCallId='workflow', name='run_workflow', kind='other',
                     rawInput=dict(Script='print("salvage")'), status=wf_status),
            ]
            if mode == 'drained-across-interactions':
                saved.extend([
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Shared dynamic line\n')),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='CALLCLI_QUESTION Q1 Continue?\n')),
                ])
            for e in saved:
                emit(e)
        else:
            saved.append(dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body)))
            if mode == 'replay-question-salvaged':
                saved[1]['status'] = 'completed'
                saved.append(dict(sessionUpdate='agent_message_chunk',
                                  content=dict(type='text', text='Answer after salvaged question\n')))
        send(dict(id=rid, result=dict(stopReason='end_turn')))
    elif m == 'session/load':
        if closed:
            send(dict(id=rid, error=dict(code=-32602, message='Session not found')))
            continue
        if not prompted:
            send(dict(id=rid, result=meta()))
            continue
        loads += 1
        post = []
        if mode == 'drained-across-interactions':
            dlg_seen = any(
                e.get('sessionUpdate') == 'user_message_chunk'
                and 'CALLCLI_DIALOGUE ' in e.get('content', {}).get('text', '')
                for e in saved
            )
            if not dlg_seen:
                replay = list(saved)
            else:
                post_dlg_loads += 1
                replay = list(saved) + [
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Wrong tail\n')),
                    dict(sessionUpdate='tool_call_update', toolCallId='inv-orphan', status='completed'),
                    dict(sessionUpdate='tool_call', toolCallId='inv-orphan', name='read_board', status='completed'),
                ]
                if post_dlg_loads == 1:
                    post = [
                        dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Shared dynamic line\n')),
                        dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Drained live final\n')),
                    ]
        elif mode == 'replay-question-salvaged':
            dlg_seen = any(
                e.get('sessionUpdate') == 'user_message_chunk'
                and 'CALLCLI_DIALOGUE ' in e.get('content', {}).get('text', '')
                for e in saved
            )
            if not dlg_seen:
                if loads == 1:
                    replay = list(saved) + [
                        dict(sessionUpdate='agent_message_chunk',
                             content=dict(type='text', text='CALLCLI_QUESTION Q1 Salvaged question?\n')),
                        dict(sessionUpdate='tool_call_update', toolCallId='inv-orphan', status='completed'),
                        dict(sessionUpdate='tool_call', toolCallId='inv-orphan', name='read_board', status='completed'),
                    ]
                else:
                    replay = [
                        saved[0],
                        dict(saved[1], status='completed'),
                        dict(sessionUpdate='tool_call_update', toolCallId='inv-orphan', status='completed'),
                        dict(sessionUpdate='tool_call', toolCallId='inv-orphan', name='read_board', status='completed'),
                    ]
            else:
                replay = list(saved)
        else:
            if loads == 1:
                replay = list(saved) + [
                    dict(sessionUpdate='agent_message_chunk',
                         content=dict(type='text', text='\u26a0 Agent execution terminated due to error.\n')),
                    dict(sessionUpdate='tool_call_update', toolCallId='inv-orphan', status='completed'),
                    dict(sessionUpdate='tool_call', toolCallId='inv-orphan', name='read_board', status='completed'),
                ]
            else:
                replay = [
                    saved[0],
                    dict(saved[1], status='completed'),
                    dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Normal prose after dropped fatal\n')),
                    dict(sessionUpdate='tool_call_update', toolCallId='inv-orphan', status='completed'),
                    dict(sessionUpdate='tool_call', toolCallId='inv-orphan', name='read_board', status='completed'),
                ]
        for e in replay:
            emit(e)
        send(dict(id=rid, result=meta()))
        for e in post:
            emit(e)
    elif m == 'session/close':
        closed = True
        send(dict(id=rid, result={}))
"""
        for mode in ('drained-across-interactions', 'replay-question-salvaged', 'replay-fatal-holds'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                exe = root / 'agy'
                exe.write_text(salvage_shim)
                exe.chmod(0o700)
                with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ.get('PATH', ''),
                                AGY_DYN_SALVAGE_MODE=mode), \
                        patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                        patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                    client = c.connect('hermes')['client_id']
                    task = c.start(client, agent='agy', cwd=directory, prompt='dynamic salvage test',
                                   agy_mode='dynamic', model='gemini-4-argon', effort='low', write=True)
                    rc = []
                    th = threading.Thread(target=lambda: rc.append(
                        run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)))
                    th.start()
                    try:
                        if mode in {'drained-across-interactions', 'replay-question-salvaged'}:
                            deadline = time.monotonic() + 6
                            while th.is_alive() and not c.status(client, task['id']).get('questions') and time.monotonic() < deadline:
                                time.sleep(0.01)
                            qs = c.status(client, task['id']).get('questions', [])
                            self.assertTrue(qs, c.status(client, task['id']))
                            c.dialogue(client, task['id'], 'proceed', expected_turn=1, command_id='dlg-' + mode, question_id=qs[0])
                        th.join(6)
                        self.assertFalse(th.is_alive())
                        st = c.status(client, task['id'])
                        if mode == 'drained-across-interactions':
                            self.assertEqual(rc, [0], st.get('settlement_note'))
                            self.assertEqual(st['state'], 'completed')
                            self.assertEqual(st['native_result']['interaction_seq'], 1)
                            self.assertEqual(st['native_result']['result'], 'Shared dynamic line\nDrained live final')
                            self.assertNotIn('Wrong tail', st['native_result']['result'])
                            evs = c.events(client, task['id'])
                            shared_seqs = [e.get('interaction_seq') for e in evs
                                           if e['kind'] == 'answer' and e['text'] == 'Shared dynamic line']
                            self.assertEqual(shared_seqs, [0, 1])
                        elif mode == 'replay-question-salvaged':
                            self.assertEqual(rc, [0], st.get('settlement_note'))
                            self.assertEqual(st['state'], 'completed')
                            self.assertEqual(st['native_result']['result'], 'Answer after salvaged question')
                        else:
                            self.assertEqual(rc, [1])
                            self.assertEqual(st['state'], 'unknown')
                            self.assertNotIn('native_cleanup', st)
                            self.assertNotIn('native_result', st)
                    finally:
                        if th.is_alive():
                            c.cancel(client, task['id'])
                            th.join(3)

    def test_same_interaction_post_tool_repeated_answer_preserved_under_degrade(self):
        repeat_shim = r"""#!/usr/bin/env python3
import json, os, sys
SID = 'dyn-repeat-session-1'
mode = os.environ['AGY_DYN_REPEAT_MODE']
model = 'gemini-4-argon'
effort = 'low'
closed = False
prompted = False
loads = 0
body_saved = None

def send(obj):
    sys.stdout.write(json.dumps(dict(jsonrpc='2.0', **obj)) + '\n')
    sys.stdout.flush()

def emit(u):
    send(dict(method='session/update', params=dict(sessionId=SID, update=u)))

def wf(status='completed'):
    return dict(sessionUpdate='tool_call', toolCallId='workflow', name='run_workflow', kind='other',
                rawInput=dict(Script='print("repeat")'), status=status)

def meta():
    return {
        'sessionId': SID,
        'models': {
            'currentModelId': model + '-' + effort,
            'availableModels': [{'modelId': 'gemini-4-argon-low'}],
        },
        'configOptions': [
            {'id': 'model', 'currentValue': model, 'options': [{'value': model}]},
            {'id': 'thought_level', 'currentValue': effort, 'options': [{'value': effort}]},
        ],
    }

for line in sys.stdin:
    if not line.strip():
        continue
    req = json.loads(line)
    rid, m, params = req.get('id'), req.get('method'), req.get('params', {})
    if m == 'initialize':
        send(dict(id=rid, result=dict(
            protocolVersion=1,
            agentCapabilities=dict(loadSession=True, sessionCapabilities=dict(close={}, resume={})),
        )))
    elif m == 'session/new':
        send(dict(id=rid, result=meta()))
        emit(dict(sessionUpdate='available_commands_update', availableCommands=[dict(name='run-workflow')]))
    elif m == 'session/set_config_option':
        if closed:
            send(dict(id=rid, error=dict(code=-32602, message='Session not found')))
        else:
            if params.get('configId') == 'model':
                model = params.get('value')
            elif params.get('configId') == 'thought_level':
                effort = params.get('value')
            send(dict(id=rid, result=meta()))
    elif m == 'session/prompt':
        prompted = True
        body_saved = ''.join(p.get('text', '') for p in params.get('prompt', []))
        emit(wf('completed'))
        emit(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Verified live result: 2 + 3 = 5.\n')))
        send(dict(id=rid, result=dict(stopReason='end_turn')))
    elif m == 'session/load':
        if closed:
            send(dict(id=rid, error=dict(code=-32602, message='Session not found')))
            continue
        if not prompted:
            send(dict(id=rid, result=meta()))
            continue
        loads += 1
        post = []
        emit(dict(sessionUpdate='user_message_chunk', content=dict(type='text', text=body_saved)))
        emit(wf('completed'))
        emit(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Verified live result: 2 + 3 = 5.\n')))
        emit(dict(sessionUpdate='tool_call', toolCallId='next', name='read_board',
                  status='in_progress' if loads == 1 else 'completed'))
        if loads == 1:
            if mode == 'reconciled-post-tool-repeat':
                emit(dict(sessionUpdate='agent_message_chunk',
                          content=dict(type='text', text='Verified live result: 2 + 3 = 5.\n')))
        else:
            if mode in {'reconciled-post-tool-repeat', 'drained-post-tool-repeat'}:
                emit(dict(sessionUpdate='tool_call_update', toolCallId='reordered', status='completed'))
                emit(dict(sessionUpdate='tool_call', toolCallId='reordered', name='read_board', status='completed'))
                emit(dict(sessionUpdate='agent_message_chunk',
                          content=dict(type='text', text='Wrong replay tail: 2 + 3 = 6.\n')))
                if mode == 'drained-post-tool-repeat' and loads == 2:
                    post = [dict(sessionUpdate='agent_message_chunk',
                                 content=dict(type='text', text='Verified live result: 2 + 3 = 5.\n'))]
        send(dict(id=rid, result=meta()))
        for e in post:
            emit(e)
    elif m == 'session/close':
        closed = True
        send(dict(id=rid, result={}))
"""
        for mode in ('reconciled-post-tool-repeat', 'drained-post-tool-repeat', 'post-tool-absent-holds'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                exe = root / 'agy'
                exe.write_text(repeat_shim)
                exe.chmod(0o700)
                with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ.get('PATH', ''),
                                AGY_DYN_REPEAT_MODE=mode), \
                        patch('updates.DEFAULT_LOCK_ROOT', root / 'locks'), \
                        patch('updates.ensure_current', return_value={'decision': 'fixture'}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    c = Collaboration(root / 'tasks.db', launch=lambda task: None)
                    client = c.connect('omp')['client_id']
                    task = c.start(client, agent='agy', cwd=directory, prompt='repeated answer post-tool',
                                   agy_mode='dynamic', model='gemini-4-argon', effort='low', write=True)
                    rc = run_task(c.store.path, task['id'], turn=1, launch_id=task['launch_id'], epoch=1)
                    st = c.status(client, task['id'])
                    evs = c.events(client, task['id'])
                    if mode == 'post-tool-absent-holds':
                        self.assertEqual(rc, 1)
                        self.assertEqual(st['state'], 'unknown')
                        self.assertNotIn('native_result', st)
                        self.assertNotIn('native_cleanup', st)
                    else:
                        self.assertEqual(rc, 0, st.get('settlement_note'))
                        self.assertEqual(st['state'], 'completed')
                        self.assertIs(st['native_observation']['history_degraded'], True)
                        self.assertEqual(st['native_result']['result'], 'Verified live result: 2 + 3 = 5.')
                        self.assertNotIn('Wrong replay tail', st['native_result']['result'])
                        if mode == 'reconciled-post-tool-repeat':
                            ans = [e['text'] for e in evs if e['kind'] == 'answer' and e['text'] == 'Verified live result: 2 + 3 = 5.']
                            self.assertEqual(len(ans), 2)

    def test_b7_sticky_fatal_degraded_preservation_and_diagnostic_unknown(self):
        from agy import AGY_INTERRUPTED_LINE
        from agy_native import NativeOwner

        # 1. Sticky fatal followed by later ordinary prose still fails closed
        def sticky_fatal_fg(s):
            return s.replace(
                "text('workflow failed\\n' if fault=='failed' else '{\"A\":{\"sum\":5},\"B\":{\"difference\":3},\"verified\":true,\"label\":\"revised\"}\\n')",
                "text('\\u26a0 Agent execution terminated due to error.\\nLater ordinary prose.\\n')",
                1,
            )
        with self.workspace(transform=sticky_fatal_fg) as (root, handle, task, thread, status, _events, await_condition):
            self.approve(handle, task, status, await_condition)
            thread.join(6)
            self.assertEqual(status()['state'], 'unknown')
            self.assertNotIn('native_cleanup', status())

        # 2. Degraded inverted_tool_replay: preserves live wire answer when replay replaces it,
        #    and retains HOLD when unrecovered non-workflow parent tool failed after prior answer.
        for case in ('preserve-live-answer', 'unrecovered-parent-failure'):
            extra_fg = (
                "            update(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Prior draft\\n')))\n"
                "            update(dict(sessionUpdate='tool_call', toolCallId='other', name='execute', kind='execute', status='failed'))\n"
                if case == 'unrecovered-parent-failure'
                else "            update(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Live verified dynamic answer\\n')))\n"
            )
            bad_load = (
                "            if not prompted:\n"
                "                send(dict(id=msg['id'], result=metadata()))\n"
                "                captured = None\n"
                "                continue\n"
                "            for entry in saved:\n"
                "                if entry.get('toolCallId') == 'other':\n"
                "                    continue\n"
                "                if entry.get('sessionUpdate') == 'agent_message_chunk' and entry.get('content',{}).get('text','').startswith('Live verified'):\n"
                "                    update(dict(sessionUpdate='agent_message_chunk', content=dict(type='text', text='Wrong tail\\n')))\n"
                "                    continue\n"
                "                update(entry)\n"
                "            update(dict(sessionUpdate='tool_call_update', toolCallId='inv-orphan', status='completed'))\n"
                "            update(dict(sessionUpdate='tool_call', toolCallId='inv-orphan', name='read_board', status='completed'))\n"
            )
            def deg_transform(s):
                s = s.replace(
                    "            if fault!='async':\n                text('workflow failed\\n' if fault=='failed' else '{\"A\":{\"sum\":5},\"B\":{\"difference\":3},\"verified\":true,\"label\":\"revised\"}\\n')\n",
                    extra_fg,
                    1,
                )
                return s.replace(
                    "            for entry in saved:\n                if os.environ['AGY_DYNAMIC_FIXTURE']=='renamed-workflow' and entry.get('toolCallId')=='workflow':\n                    entry = dict(entry,name='read_board')\n                update(entry)\n",
                    bad_load,
                    1,
                )
            with self.subTest(case=case), self.workspace(transform=deg_transform) as (root, handle, task, thread, status, events, await_condition):
                self.approve(handle, task, status, await_condition)
                thread.join(6)
                st = status()
                if case == 'preserve-live-answer':
                    self.assertEqual(st['state'], 'completed', st.get('settlement_note'))
                    self.assertIs(st['native_observation']['history_degraded'], True)
                    self.assertEqual(st['native_result']['result'], 'Live verified dynamic answer')
                    self.assertNotIn('Wrong tail', st['native_result']['result'])
                else:
                    self.assertEqual(st['state'], 'unknown')
                    self.assertNotIn('native_cleanup', st)

        # 3. Diagnostic-only unknown (interrupted / API error + board navigation) unloads & stops with force=False
        for diag_text in (AGY_INTERRUPTED_LINE + '\n', 'API error (attempt 1): 401 UNAUTHENTICATED\nRun /swarm board\n'):
            stops = []
            orig_stop = NativeOwner.stop
            def record_stop(owner, **kwargs):
                stops.append(kwargs.get('force', True))
                return orig_stop(owner, **kwargs)
            def diag_transform(s):
                return s.replace(
                    "text('workflow failed\\n' if fault=='failed' else '{\"A\":{\"sum\":5},\"B\":{\"difference\":3},\"verified\":true,\"label\":\"revised\"}\\n')",
                    f"text({diag_text!r})",
                    1,
                )
            with self.subTest(diag=diag_text), patch.object(NativeOwner, 'stop', record_stop), \
                    self.workspace(transform=diag_transform) as (root, handle, task, thread, status, _events, await_condition):
                self.approve(handle, task, status, await_condition)
                thread.join(6)
                st = status()
                self.assertEqual(st['state'], 'unknown')
                self.assertEqual(st['native_cleanup']['outcome'], 'unknown')
                self.assertIs(st['native_cleanup']['session_unloaded'], True)
                self.assertNotIn('native_result', st)
                self.assertNotIn('native_resume_session_id', st)
                self.assertEqual(stops, [False])
                self.assertEqual(handle(dict(action='close', task_id=task['id']))['task']['state'], 'closed')

if __name__ == '__main__':
    unittest.main()
