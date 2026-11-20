import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


def load_plugin():
    import sys
    if 'callcli_plugin' in sys.modules:
        return sys.modules['callcli_plugin']
    spec=importlib.util.spec_from_file_location('callcli_plugin',Path(__file__).with_name('__init__.py'),submodule_search_locations=[str(Path(__file__).parent)])
    mod=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=mod
    spec.loader.exec_module(mod)
    return mod


class Ctx:
    profile_name='default'
    enabled=True
    def get_config(self,key,default=None):return self.enabled if key=='enabled' else default
    def register_tool(self,**kwargs):self.registration=kwargs
    def dispatch_tool(self,name,args,**kwargs):
        if not hasattr(self,'calls'):self.calls=[]
        self.called=(name,args)
        self.calls.append((name,args))
        return json.dumps({'session_id':f'proc-owned-{len(self.calls)}','pid':123,'notify_on_complete':True})



class HostTimeoutTests(unittest.TestCase):
    def test_host_rejects_numeric_limits_but_preserves_legacy_continuations(self):
        mod = load_plugin()
        with tempfile.TemporaryDirectory() as d:
            env = {'HERMES_SESSION_ID':'s1', 'HERMES_SESSION_KEY':'key'}
            ctx = Ctx(); c = mod.Coordinator(ctx, Path(d), lambda k,x='':env.get(k,x))
            for value in (30, None):
                with self.assertRaises(ValueError):
                    c.handle({'action':'start','agent':'omp','cwd':d,'prompt':'inspect','timeout':value})
            self.assertFalse(hasattr(ctx, 'calls'))
            self.assertEqual(c.handle({'action':'list'})['tasks'], [])
            task = c.handle({'action':'start','agent':'omp','cwd':d,'prompt':'inspect'})['task']
            self.assertNotIn('timeout', ctx.called[1])
            store = mod.Store(Path(d)/'call-cli/tasks.sqlite3')
            store.update(task['id'], state='completed', timeout=300)
            bounded = c.handle({'action':'reply','task_id':task['id'],'prompt':'continue','expected_turn':1,'command_id':'bounded'})['task']
            self.assertEqual((bounded['turn'], bounded['timeout'], ctx.called[1]['timeout']), (2, 300, 480))
            with self.assertRaises(ValueError):
                c.handle({'action':'intervene','task_id':task['id'],'prompt':'revise',
                          'command_id':'unsafe','clear_timeout':True})
            self.assertEqual(store.get(task['id'])['timeout'], 300)
            store.update(task['id'], state='completed')
            cleared = c.handle({'action':'reply','task_id':task['id'],'prompt':'continue','clear_timeout':True,'expected_turn':2,'command_id':'clear'})['task']
            self.assertEqual((cleared['turn'], cleared['timeout']), (3, None))
            self.assertNotIn('timeout', ctx.called[1])

class PluginTests(unittest.TestCase):
    def test_hermes_shorthand_resolves_and_rejects_before_launch(self):
        mod=load_plugin()
        with tempfile.TemporaryDirectory() as root:
            env={'HERMES_SESSION_ID':'s1','HERMES_SESSION_KEY':'key'}
            ctx=Ctx(); c=mod.Coordinator(ctx,Path(root),lambda k,x='':env.get(k,x))
            for model,mode,reason in (('ocs','off','conflict'),('4a','native','conflict'),
                                      ('4ast',None,'Invalid AGY shorthand')):
                with self.assertRaisesRegex(ValueError,reason):
                    c.handle(dict(action='start',agent='agy',cwd=root,prompt='never send',model=model,agy_swarm=mode))
            self.assertEqual(c.handle({'action':'list'})['tasks'],[])
            self.assertFalse(hasattr(ctx,'calls'))
            for model,expected,mode in ((None,'gemini-pro-orchestrator','native'),
                    ('oc','gemini-pro-orchestrator','off'),('oas','gemini-pro-orchestrator-argon','native'),
                    ('4a','gemini-4-argon','off'),('4as','gemini-4-argon','native')):
                task=c.handle(dict(action='start',agent='agy',cwd=root,prompt='synthetic',model=model))['task']
                self.assertEqual((task['model'],task['agy_swarm']),(expected,mode))
            self.assertIn('teamwork',mod.SCHEMA['parameters']['properties']['agy_mode']['enum'])
            for fields in ({'model':'oct'}, {'model':'gemini-pro-orchestrator','agy_mode':'teamwork'}):
                task=c.handle(dict(action='start',agent='agy',cwd=root,prompt='native request',**fields))['task']
                self.assertEqual((task['model'],task['agy_mode'],task['agy_swarm']),
                    ('gemini-pro-orchestrator','teamwork','native'))

    def test_repeated_plugin_load_retains_patchable_native_submodule(self):
        import sys
        first = load_plugin()
        second = load_plugin()
        self.assertIs(second.native_continuity, sys.modules[second.Store.__module__])
        self.assertIs(first.Store, second.Store)

    def test_dialogue_schema_and_live_owner_submission_do_not_launch_or_cancel(self):
        mod = load_plugin()
        self.assertIn('dialogue', mod.SCHEMA['parameters']['properties']['action']['enum'])
        with tempfile.TemporaryDirectory() as d:
            env = {'HERMES_SESSION_ID':'s1', 'HERMES_SESSION_KEY':'key'}
            ctx = Ctx(); c = mod.Coordinator(ctx, Path(d), lambda k,x='':env.get(k,x))
            task = c.handle(dict(action='start', agent='agy', cwd=d, prompt='owned'))['task']
            store = mod.Store(Path(d)/'call-cli/tasks.sqlite3')
            store.update(task['id'], state='running', runner_pid=123)
            store.native_input_open(task['id'], 1, task['launch_id'], 1)
            args = dict(action='dialogue', task_id=task['id'], prompt='revise', expected_turn=1, command_id='d1')
            admitted = c.handle(args)['task']
            self.assertEqual((admitted['state'], admitted['turn']), ('running', 1))
            self.assertEqual(admitted['commands']['d1']['status'], 'accepted')
            self.assertEqual(len(ctx.calls), 1)
            self.assertEqual(c.handle(args)['task'], admitted)
            for field in ('expected_turn', 'command_id'):
                missing = dict(args); missing.pop(field)
                with self.assertRaises(ValueError):
                    c.handle(missing)
            sup = c.handle(dict(action='dialogue', task_id=task['id'], prompt='revise 2', expected_turn=1,
                                command_id='d2', target_command_id='d1'))['task']
            self.assertEqual(sup['commands']['d1']['status'], 'superseded')
            self.assertEqual(sup['commands']['d2']['status'], 'accepted')
            wd = c.handle(dict(action='dialogue', task_id=task['id'], expected_turn=1,
                               command_id='w2', target_command_id='d2', withdraw=True))['task']
            self.assertEqual(wd['commands']['d2']['status'], 'withdrawn')
            with self.assertRaises(ValueError):
                c.handle(dict(action='status', task_id=task['id'], withdraw=True))

    def test_launch_reuses_native_notification_route_without_model_supplied_origin(self):
        mod=load_plugin()
        with tempfile.TemporaryDirectory() as d:
            env={'HERMES_SESSION_ID':'s1','HERMES_SESSION_KEY':'key-topic1','HERMES_SESSION_PLATFORM':'telegram',
                 'HERMES_SESSION_CHAT_ID':'chat','HERMES_SESSION_THREAD_ID':'topic1'}
            ctx=Ctx(); coordinator=mod.Coordinator(ctx,Path(d),lambda k,x='':env.get(k,x))
            result=coordinator.handle({'action':'start','device':'local','agent':'omp','cwd':d,'prompt':'inspect'})
            self.assertEqual(result['task']['origin']['thread_id'],'topic1')
            self.assertEqual(ctx.called[0],'terminal')
            self.assertTrue(ctx.called[1]['background'])
            self.assertTrue(ctx.called[1]['notify'])
            self.assertEqual(ctx.called[1]['heartbeat'],60)
            self.assertNotIn('origin',mod.SCHEMA['parameters']['properties'])
            task=result['task']['id']
            env['HERMES_SESSION_THREAD_ID']='topic2'
            with self.assertRaises(PermissionError):coordinator.handle({'action':'cancel','task_id':task})
            ctx.enabled=False
            with self.assertRaises(PermissionError):coordinator.handle({'action':'list'})

    def test_agy_schema_and_model_persist_through_hermes_reply(self):
        mod = load_plugin()
        self.assertIn('agy', mod.SCHEMA['parameters']['properties']['agent']['enum'])
        self.assertIn('model', mod.SCHEMA['parameters']['properties'])
        with tempfile.TemporaryDirectory() as d:
            env = {'HERMES_SESSION_ID':'s1', 'HERMES_SESSION_KEY':'key'}
            ctx = Ctx(); c = mod.Coordinator(ctx, Path(d), lambda k,x='':env.get(k,x))
            task = c.handle({'action':'start','agent':'agy','cwd':d,'prompt':'inspect','model':'live-family','effort':'low',
                             'agy_approval':'approve-reads'})['task']
            store = mod.Store(Path(d)/'call-cli/tasks.sqlite3')
            store.update(task['id'], state='completed')
            resumed = c.handle({'action':'reply','task_id':task['id'],'prompt':'continue','expected_turn':1,'command_id':'continue'})['task']
            self.assertEqual(resumed['model'], 'live-family')
            self.assertEqual(resumed['effort'], 'low')
            self.assertEqual(resumed['session'], task['session'])
            self.assertEqual(resumed['agy_approval'], 'approve-reads')
            default = c.handle({'action':'start','agent':'agy','cwd':d,'prompt':'inspect','model':'live-family'})['task']
            self.assertEqual(default['agy_approval'], 'skip-permissions')
            self.assertEqual(default['execution_policy'], 'native-skip')
            with self.assertRaises(ValueError):
                c.handle({'action':'start','agent':'omp','cwd':d,'prompt':'inspect','agy_approval':'approve-all'})

    def test_hermes_intervention_waits_for_confirmation_then_resumes_once(self):
        mod=load_plugin()
        with tempfile.TemporaryDirectory() as d:
            env={'HERMES_SESSION_ID':'s1','HERMES_SESSION_KEY':'key-topic1','HERMES_SESSION_PLATFORM':'telegram',
                 'HERMES_SESSION_CHAT_ID':'chat','HERMES_SESSION_THREAD_ID':'topic1'}
            ctx=Ctx(); c=mod.Coordinator(ctx,Path(d),lambda k,x='':env.get(k,x))
            task=c.handle({'action':'start','device':'local','agent':'agy','cwd':d,'prompt':'first','model':'family','effort':'low'})['task']
            store=mod.Store(Path(d)/'call-cli/tasks.sqlite3')
            store.update(task['id'],state='running')
            pending=c.handle({'action':'intervene','task_id':task['id'],'prompt':'next','command_id':'stable'})
            self.assertEqual(pending['task']['state'],'cancelling')
            self.assertEqual(len(ctx.calls),1)
            origin=mod.capture_origin(lambda k,x='':env.get(k,x),'default')
            self.assertEqual(store.get(task['id'],origin)['state'],'cancelling')
            with self.assertRaises(ValueError):
                c.handle({'action':'intervene','task_id':task['id'],'prompt':'different','command_id':'stable'})
            c.handle({'action':'intervene','task_id':task['id'],'prompt':'next','command_id':'stable'})
            self.assertEqual(len(ctx.calls),1)
            store.update(task['id'],state='unknown')
            unknown=c.handle({'action':'status','task_id':task['id']})['task']
            self.assertEqual(unknown['state'],'unknown')
            self.assertEqual(len(ctx.calls),1)
            store.update(task['id'],state='cancelled')
            before_bytes=Path(store.path).read_bytes()
            c.handle({'action':'events','task_id':task['id'],'after':0})
            settled_status=c.handle({'action':'status','task_id':task['id']})
            self.assertEqual((settled_status['task']['turn'],settled_status['task']['state']),(1,'cancelled'))
            self.assertIn('retry intervene',settled_status['note'])
            self.assertEqual(len(ctx.calls),1)
            self.assertEqual(Path(store.path).read_bytes(),before_bytes)
            c.handle({'action':'intervene','task_id':task['id'],'prompt':'next','command_id':'stable'})
            self.assertEqual(len(ctx.calls),2)
            live=store.get(task['id'],origin)
            self.assertEqual((live['turn'],live['state'],live['prompt']), (2,'queued','next'))
            self.assertEqual((live['model'],live['effort'],live['session']),('family','low',task['session']))
            c.handle({'action':'status','task_id':task['id']})
            self.assertEqual(len(ctx.calls),2)
            c.handle({'action':'intervene','task_id':task['id'],'prompt':'next','command_id':'stable'})
            self.assertEqual(len(ctx.calls),2)

    def test_hermes_intervention_requires_id_and_session_ownership(self):
        mod=load_plugin()
        with tempfile.TemporaryDirectory() as d:
            env={'HERMES_SESSION_ID':'s1','HERMES_SESSION_KEY':'key-topic1','HERMES_SESSION_PLATFORM':'telegram',
                 'HERMES_SESSION_CHAT_ID':'chat','HERMES_SESSION_THREAD_ID':'topic1'}
            ctx=Ctx(); c=mod.Coordinator(ctx,Path(d),lambda k,x='':env.get(k,x))
            task=c.handle({'action':'start','device':'local','agent':'omp','cwd':d,'prompt':'first'})['task']
            with self.assertRaises(ValueError):
                c.handle({'action':'intervene','task_id':task['id'],'prompt':'next'})
            env['HERMES_SESSION_THREAD_ID']='topic1'
            started=c.handle({'action':'intervene','task_id':task['id'],'prompt':'next','command_id':'allowed'})
            self.assertEqual(started['task']['state'],'queued')
            self.assertEqual(len(ctx.calls),1)
            env['HERMES_SESSION_THREAD_ID']='topic2'
            with self.assertRaises(PermissionError):
                c.handle({'action':'status','task_id':task['id']})
            with self.assertRaises(PermissionError):
                c.handle({'action':'intervene','task_id':task['id'],'prompt':'again','command_id':'other'})

    def test_hermes_cancel_withdraws_intervention_and_reply_cannot_bypass(self):
        mod=load_plugin()
        with tempfile.TemporaryDirectory() as d:
            env={'HERMES_SESSION_ID':'s1','HERMES_SESSION_KEY':'key','HERMES_SESSION_PLATFORM':'telegram'}
            ctx=Ctx(); c=mod.Coordinator(ctx,Path(d),lambda k,x='':env.get(k,x))
            task=c.handle({'action':'start','device':'local','agent':'omp','cwd':d,'prompt':'first'})['task']
            store=mod.Store(Path(d)/'call-cli/tasks.sqlite3')
            store.update(task['id'],state='running')
            c.handle({'action':'intervene','task_id':task['id'],'prompt':'next','command_id':'one'})
            result=c.handle({'action':'cancel','task_id':task['id']})
            self.assertEqual(result['task']['intervention']['status'],'withdrawn')
            with self.assertRaises(ValueError):
                c.handle({'action':'reply','task_id':task['id'],'question_id':'q','prompt':'bypass'})

    def test_preprovider_cancelled_first_turn_can_create_revision_session(self):
        from test_runner import RunnerBehaviorTests
        fixture=RunnerBehaviorTests(); fixture.setUp()
        try:
            mod=load_plugin(); ctx=Ctx()
            env={'HERMES_SESSION_ID':'s1','HERMES_SESSION_KEY':'key'}
            c=mod.Coordinator(ctx,fixture.root,lambda k,x='':env.get(k,x))
            task=c.handle({'action':'start','agent':'omp','cwd':str(fixture.root),'prompt':'old'})['task']
            store=mod.Store(fixture.root/'call-cli/tasks.sqlite3')
            store.update(task['id'],state='running')
            intent={'action':'intervene','task_id':task['id'],'prompt':'new','command_id':'preprovider','expected_turn':1}
            c.handle(intent)
            store.finish_turn(task['id'],1,'cancelled',0,'cancelled')
            self.assertEqual(len(ctx.calls),1,'settlement alone must not launch a successor')
            self.assertEqual(c.handle({'action':'status','task_id':task['id']})['task']['turn'],1)
            c.handle({'action':'events','task_id':task['id'],'after':0})
            self.assertEqual(store.get(task['id'])['turn'],1)
            self.assertEqual(len(ctx.calls),1,'status and events must stay pure read-only')
            c.handle(intent)
            self.assertEqual(len(ctx.calls),2)
            self.assertEqual(store.get(task['id'])['turn'],2)
            admitted=store.get(task['id'])
            self.assertEqual((admitted['commands']['preprovider']['command_id'],admitted['commands']['preprovider']['expected_turn'],
                admitted['commands']['preprovider']['source_turn'],admitted['commands']['preprovider']['target_turn']),('preprovider',1,1,2))
            c.handle({'action':'events','task_id':task['id'],'after':0})
            c.handle({'action':'status','task_id':task['id']})
            c.handle(intent)
            self.assertEqual(len(ctx.calls),2,'controller reads and command replay must not duplicate target launch')
            self.assertEqual(__import__('test_dispatch').run_dispatched(store.path, task['id']),0)
            final=store.get(task['id'])
            self.assertEqual(final['state'],'completed')
            self.assertTrue(final['acpx_created'])
            self.assertFalse(final['needs_session'])
            self.assertEqual(final['commands']['preprovider']['status'],'target_completed')
            origin=mod.capture_origin(lambda k,x='':env.get(k,x),'default')
            self.assertEqual([e['text'] for e in store.events(task['id'],origin) if e['kind']=='answer'],['FAKE_ANSWER'])
            calls=[json.loads(line) for line in (fixture.root/'calls.jsonl').read_text().splitlines()]
            self.assertEqual(sum('new' in call for call in calls),1)
        finally:
            fixture.tearDown()

    def test_late_dispatch_receipt_cannot_overwrite_new_turn(self):
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as d:
                mod=load_plugin(); ctx=Ctx()
                env={'HERMES_SESSION_ID':'s1','HERMES_SESSION_KEY':'key'}
                c=mod.Coordinator(ctx,Path(d),lambda k,x='':env.get(k,x))
                store=mod.Store(Path(d)/'call-cli/tasks.sqlite3')
                original=ctx.dispatch_tool
                def dispatch(name,args,**kwargs):
                    receipt=original(name,args)
                    task=store.list(mod.capture_origin(c.get_env,'default'))[0]
                    if task['turn']==1:
                        store.update(task['id'],state='running')
                        store.finish_turn(task['id'],1,'completed',0,'end_turn')
                        c.handle({'action':'intervene','task_id':task['id'],'prompt':'next','command_id':'next'})
                        if failure:raise RuntimeError('late dispatch failure')
                    else:
                        store.update(task['id'],state='running')
                    return receipt
                ctx.dispatch_tool=dispatch
                args={'action':'start','agent':'omp','cwd':d,'prompt':'first'}
                if failure:
                    with self.assertRaisesRegex(RuntimeError,'late dispatch'):c.handle(args)
                else:c.handle(args)
                task=store.list(mod.capture_origin(c.get_env,'default'))[0]
                self.assertEqual((task['turn'],task['state'],task['process_id']),
                                 (2,'running','proc-owned-2'))
                cancelled=c.handle({'action':'cancel','task_id':task['id']})['task']
                self.assertEqual(cancelled['state'],'cancelling')
                self.assertEqual(len(ctx.calls),2)

    def test_legacy_preprovider_cancel_uses_durable_event_to_create_once(self):
        from test_runner import RunnerBehaviorTests
        from unittest.mock import patch
        import runner
        fixture=RunnerBehaviorTests(); fixture.setUp()
        try:
            mod=load_plugin(); ctx=Ctx()
            env={'HERMES_SESSION_ID':'s1','HERMES_SESSION_KEY':'key'}
            c=mod.Coordinator(ctx,fixture.root,lambda k,x='':env.get(k,x))
            store=mod.Store(fixture.root/'call-cli/tasks.sqlite3')
            origin=mod.capture_origin(c.get_env,'default')
            task=store.create(origin,'local','omp',str(fixture.root),'old',False)
            store.update(task['id'],timeout=900)
            def cancel_before_provider(*args):
                c.handle({'action':'cancel','task_id':task['id']})
                return {'decision':'current'}
            with patch('updates.ensure_current',side_effect=cancel_before_provider):
                self.assertEqual(__import__('test_dispatch').run_dispatched(store.path, task['id']),0)
            self.assertNotIn('session_creation_attempted',store.get(task['id']))
            c.handle({'action':'intervene','task_id':task['id'],'prompt':'new','command_id':'legacy'})
            self.assertEqual(__import__('test_dispatch').run_dispatched(store.path, task['id']),0)
            final=store.get(task['id'])
            self.assertEqual((final['turn'],final['state']),(2,'completed'))
            self.assertTrue(final['acpx_created'])
            calls=[json.loads(line) for line in (fixture.root/'calls.jsonl').read_text().splitlines()]
            self.assertEqual(sum('new' in call for call in calls),1)
            self.assertEqual(len(ctx.calls),1)
        finally:fixture.tearDown()

    def test_legacy_without_creation_evidence_refuses_before_intervention(self):
        with tempfile.TemporaryDirectory() as d:
            mod=load_plugin(); ctx=Ctx()
            env={'HERMES_SESSION_ID':'s1','HERMES_SESSION_KEY':'key'}
            c=mod.Coordinator(ctx,Path(d),lambda k,x='':env.get(k,x))
            store=mod.Store(Path(d)/'call-cli/tasks.sqlite3')
            origin=mod.capture_origin(c.get_env,'default')
            for state in ('cancelled','running','completed'):
                with self.subTest(state=state):
                    task=store.create(origin,'local','omp',d,'old',False)
                    before=store.update(task['id'],state=state,timeout=900)
                    with self.assertRaisesRegex(ValueError,'new start'):
                        c.handle({'action':'intervene','task_id':task['id'],'prompt':'new','command_id':'legacy'})
                    self.assertEqual(store.get(task['id']),before)
            self.assertFalse(hasattr(ctx,'calls'))

    def test_dispatch_uncertainty_is_preserved_without_relaunch(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as d:
            mod=load_plugin(); ctx=Ctx()
            env={'HERMES_SESSION_ID':'s1','HERMES_SESSION_KEY':'key'}
            c=mod.Coordinator(ctx,Path(d),lambda k,x='':env.get(k,x))
            ctx.dispatch_tool=Mock(side_effect=RuntimeError('receipt lost'))
            with self.assertRaisesRegex(RuntimeError,'receipt lost'):
                c.handle({'action':'start','agent':'omp','cwd':d,'prompt':'first'})
            store=mod.Store(Path(d)/'call-cli/tasks.sqlite3')
            task=store.list(mod.capture_origin(c.get_env,'default'))[0]
            self.assertEqual(task['state'],'unknown')
            # Even a late success for this exact launch supplies only the process,
            # not evidence that the unknown runner outcome became successful.
            store.finish_launch(task['id'],1,'wrong-identity','wrong-process')
            self.assertEqual(store.get(task['id']),task)
            store.finish_launch(task['id'],1,task['launch_id'],'late-process')
            self.assertEqual(c.handle({'action':'status','task_id':task['id']})['task']['state'],'unknown')
            with self.assertRaises(ValueError):c.launch(store,task)
            self.assertEqual(ctx.dispatch_tool.call_count,1)

    def test_legacy_verified_record_resumes_without_creation(self):
        from test_runner import RunnerBehaviorTests
        import runner
        fixture=RunnerBehaviorTests(); fixture.setUp()
        try:
            mod=load_plugin(); ctx=Ctx()
            env={'HERMES_SESSION_ID':'s1','HERMES_SESSION_KEY':'key'}
            c=mod.Coordinator(ctx,fixture.root,lambda k,x='':env.get(k,x))
            task=c.handle({'action':'start','agent':'omp',
                           'cwd':str(fixture.root),'prompt':'first'})['task']
            store=mod.Store(fixture.root/'call-cli/tasks.sqlite3')
            self.assertEqual(__import__('test_dispatch').run_dispatched(store.path, task['id']),0)
            with store.connect() as db:
                current=json.loads(db.execute('SELECT data FROM tasks WHERE id=?',(task['id'],)).fetchone()[0])
                for key in ('needs_session','acpx_created','session_creation_attempted'):current.pop(key)
                store._save(db,current)
            record=current['acpx_record_id']
            c.handle({'action':'intervene','task_id':task['id'],'prompt':'next','command_id':'existing'})
            self.assertEqual(__import__('test_dispatch').run_dispatched(store.path, task['id']),0)
            self.assertEqual(store.get(task['id'])['acpx_record_id'],record)
            calls=[json.loads(line) for line in (fixture.root/'calls.jsonl').read_text().splitlines()]
            self.assertEqual(sum('new' in call for call in calls),1)
        finally:fixture.tearDown()

    def test_register_exposes_real_bound_tool(self):
        mod=load_plugin();ctx=Ctx();mod.register(ctx)
        self.assertEqual(ctx.registration['name'],'call_cli')
        self.assertIn('dialogue', ctx.registration['schema']['parameters']['properties']['action']['enum'])
        self.assertTrue(callable(ctx.registration['handler']))
        self.assertTrue(ctx.registration['check_fn']())
        ctx.enabled=False
        self.assertFalse(ctx.registration['check_fn']())

    def test_hermes_origin_context_runtime_links_and_intra_origin_group_isolation(self):
        import sqlite3
        from contextlib import closing
        mod = load_plugin()
        self.assertIn('group', mod.SCHEMA['parameters']['properties'])
        self.assertEqual(mod.SCHEMA['parameters']['properties']['group']['type'], 'string')
        self.assertNotIn('origin', mod.SCHEMA['parameters']['properties'])
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            hermes_home = root / 'hermes-home'
            hermes_home.mkdir()
            with closing(sqlite3.connect(hermes_home / 'state.db')) as db:
                db.executescript('''
                    CREATE TABLE schema_version(version INTEGER NOT NULL);
                    INSERT INTO schema_version VALUES (32);
                    CREATE TABLE sessions(id TEXT PRIMARY KEY, parent_session_id TEXT,
                        end_reason TEXT, ended_at REAL, model_config TEXT, source TEXT,
                        profile_name TEXT);
                    INSERT INTO sessions VALUES ('s1', NULL, 'compression', 1.0, NULL, 'telegram', NULL);
                    INSERT INTO sessions VALUES ('s2', 's1', NULL, NULL, '{}', 'telegram', NULL);
                ''')
            env = {
                'HERMES_SESSION_ID': 's1', 'HERMES_SESSION_KEY': 'k-shared',
                'HERMES_SESSION_PLATFORM': 'telegram', 'HERMES_SESSION_CHAT_ID': 'chat1',
                'HERMES_SESSION_THREAD_ID': 'topic1', 'HERMES_SESSION_SOURCE': 'telegram',
                'HERMES_SESSION_CHAT_TYPE': 'forum',
                'HERMES_SESSION_CHAT_NAME': '\x1b[31mOps Room\x1b[0m',
                'HERMES_SESSION_TOPIC_NAME': '  Trace  Topic  ',
                'HERMES_SESSION_TITLE': 'Thread Title',
            }
            ctx = Ctx()
            c = mod.Coordinator(ctx, root / 'plugin-data', lambda k, x='': env.get(k, x),
                                hermes_home=hermes_home, hermes_root=hermes_home)
            t1 = c.handle({'action': 'start', 'agent': 'omp', 'cwd': str(root),
                           'prompt': 'first', 'group': ' ops '})['task']
            c.handle({'action': 'start', 'agent': 'omp', 'cwd': str(root),
                      'prompt': 'second', 'group': 'other'})
            self.assertEqual(t1['group'], 'ops')
            self.assertEqual(t1['origin_context']['surface'], 'hermes-native')
            self.assertEqual(t1['origin_context']['display_name'], 'Trace Topic')
            self.assertEqual(t1['runtime_links']['process_id'], 'proc-owned-1')
            self.assertEqual(t1['runtime_links']['task_id'], t1['id'])

            env_other = dict(env, HERMES_SESSION_ID='foreign-s', HERMES_SESSION_KEY='k-other', HERMES_SESSION_THREAD_ID='topic2')
            c_other = mod.Coordinator(ctx, root / 'plugin-data', lambda k, x='': env_other.get(k, x),
                                      hermes_home=hermes_home, hermes_root=hermes_home)
            t_foreign = c_other.handle({'action': 'start', 'agent': 'omp', 'cwd': str(root),
                                        'prompt': 'foreign', 'group': 'ops'})['task']

            env['HERMES_SESSION_ID'] = 's2'
            listed = c.handle({'action': 'list', 'group': 'ops'})['tasks']
            self.assertEqual([t['id'] for t in listed], [t1['id']])
            self.assertEqual(listed[0]['runtime_links']['task_id'], t1['id'])
            self.assertNotIn(t_foreign['id'], [t['id'] for t in listed])

            rec_resp = c.handle({'action': 'recovery_status', 'task_id': t1['id']})
            rec = rec_resp['resource_recovery']
            self.assertEqual(set(rec), {'task_id', 'turn', 'business_state', 'resource_state', 'write', 'receipt'})
            self.assertEqual(rec_resp['diagnostic']['agent'], 'omp')
            self.assertEqual(rec_resp['diagnostic']['intervention_status'], 'none')
            self.assertIsNone(rec_resp['diagnostic']['native_diagnostic'])

            for bad_action in ('status', 'cancel', 'recovery_status'):
                with self.assertRaises(ValueError):
                    c.handle({'action': bad_action, 'task_id': t1['id'], 'group': 'ops'})
            for bad_action_args in ({'action': 'start', 'agent': 'omp', 'cwd': str(root), 'prompt': 'x', 'group': '   '},
                                    {'action': 'list', 'group': ''}):
                with self.assertRaises(ValueError):
                    c.handle(bad_action_args)

    def test_question_ids_schema_contract(self):
        mod = load_plugin()
        props = mod.SCHEMA['parameters']['properties']
        self.assertIn('question_ids', props)
        self.assertEqual(props['question_ids']['type'], 'array')
        self.assertEqual(props['question_ids']['items'], {'type': 'string'})
        self.assertEqual(props['question_ids']['minItems'], 1)
        self.assertEqual(props['question_id']['type'], 'string')
        self.assertNotIn('items', props['question_id'])
        ctx = Ctx()
        mod.register(ctx)
        reg_props = ctx.registration['schema']['parameters']['properties']
        self.assertEqual(reg_props['question_ids'], props['question_ids'])
        self.assertEqual(reg_props['question_id']['type'], 'string')

    def test_dialogue_question_ids_idempotency_raw_bytes_and_release(self):
        mod = load_plugin()
        with tempfile.TemporaryDirectory() as d:
            env = {'HERMES_SESSION_ID': 's1', 'HERMES_SESSION_KEY': 'key'}
            ctx = Ctx()
            c = mod.Coordinator(ctx, Path(d), lambda k, x='': env.get(k, x))
            task = c.handle(dict(action='start', agent='agy', cwd=d, prompt='owned'))['task']
            store = mod.Store(Path(d) / 'call-cli/tasks.sqlite3')
            store.update(task['id'], state='running', runner_pid=123)
            store.native_input_open(task['id'], 1, task['launch_id'], 1)
            store.event(task['id'], '1:2', 'question', 'Q1 first')
            store.event(task['id'], '1:3', 'question', 'Q2 second')
            store.event(task['id'], ' 1:raw ', 'question', 'Q3 raw')

            args = dict(action='dialogue', task_id=task['id'], prompt='answer both',
                        expected_turn=1, command_id='d1', question_ids=['1:2', '1:3'])
            admitted = c.handle(args)['task']
            self.assertEqual(len(ctx.calls), 1)
            self.assertEqual(admitted['commands']['d1']['status'], 'accepted')
            self.assertEqual(admitted['commands']['d1']['question_ids'], ['1:2', '1:3'])
            self.assertIsNone(admitted['commands']['d1']['question_id'])
            self.assertEqual([q['id'] for q in admitted['commands']['d1']['questions']], ['1:2', '1:3'])
            self.assertEqual(admitted['questions'], ['1:2', '1:3', ' 1:raw '])

            replayed = c.handle(dict(args, question_ids=['1:3', '1:2']))['task']
            self.assertEqual(replayed, admitted)
            self.assertEqual(len(ctx.calls), 1)
            with self.assertRaisesRegex(ValueError, 'different parameters'):
                c.handle(dict(args, question_ids=['1:2']))

            with self.assertRaisesRegex(ValueError, 'already has an admitted answer'):
                c.handle(dict(action='dialogue', task_id=task['id'], prompt='dup',
                              expected_turn=1, command_id='d_dup', question_ids=['1:3']))

            sup = c.handle(dict(action='dialogue', task_id=task['id'], prompt='rebind first',
                                expected_turn=1, command_id='d2', target_command_id='d1',
                                question_ids=['1:2']))['task']
            self.assertEqual(sup['commands']['d1']['status'], 'superseded')
            self.assertEqual(sup['commands']['d2']['status'], 'accepted')

            released_rebind = c.handle(dict(action='dialogue', task_id=task['id'], prompt='answer 1:3',
                                            expected_turn=1, command_id='d3', question_ids=['1:3']))['task']
            self.assertEqual(released_rebind['commands']['d3']['status'], 'accepted')
            self.assertEqual(released_rebind['commands']['d3']['question_ids'], ['1:3'])

            wd = c.handle(dict(action='dialogue', task_id=task['id'], expected_turn=1,
                               command_id='w2', target_command_id='d2', withdraw=True))['task']
            self.assertEqual(wd['commands']['d2']['status'], 'withdrawn')
            c.handle(dict(action='dialogue', task_id=task['id'], expected_turn=1,
                          command_id='w3', target_command_id='d3', withdraw=True))

            multi_rebound = c.handle(dict(action='dialogue', task_id=task['id'], prompt='reanswer both',
                                          expected_turn=1, command_id='d4', question_ids=['1:2', '1:3']))['task']
            self.assertEqual(multi_rebound['commands']['d4']['status'], 'accepted')
            self.assertEqual(multi_rebound['commands']['d4']['question_ids'], ['1:2', '1:3'])

            wd_multi = c.handle(dict(action='dialogue', task_id=task['id'], expected_turn=1,
                                     command_id='w4', target_command_id='d4', withdraw=True))['task']
            self.assertEqual(wd_multi['commands']['d4']['status'], 'withdrawn')
            reanswered_after_wd = c.handle(dict(action='dialogue', task_id=task['id'], prompt='after multi withdraw',
                                                expected_turn=1, command_id='d5', question_ids=['1:2', '1:3']))['task']
            self.assertEqual(reanswered_after_wd['commands']['d5']['status'], 'accepted')
            self.assertEqual(reanswered_after_wd['commands']['d5']['question_ids'], ['1:2', '1:3'])

            with self.assertRaisesRegex(ValueError, 'exact raw bytes'):
                c.handle(dict(action='dialogue', task_id=task['id'], prompt='trimmed',
                              expected_turn=1, command_id='d_trim', question_ids=['1:raw']))

            raw_ok = c.handle(dict(action='dialogue', task_id=task['id'], prompt='raw ok',
                                   expected_turn=1, command_id='d_raw', question_ids=[' 1:raw ']))['task']
            self.assertEqual(raw_ok['commands']['d_raw']['status'], 'accepted')
            self.assertEqual(raw_ok['commands']['d_raw']['question_ids'], [' 1:raw '])
            self.assertEqual([q['id'] for q in raw_ok['commands']['d_raw']['questions']], [' 1:raw '])

    def test_question_ids_mutual_exclusion_and_strict_validation(self):
        mod = load_plugin()
        with tempfile.TemporaryDirectory() as d:
            env = {'HERMES_SESSION_ID': 's1', 'HERMES_SESSION_KEY': 'key'}
            ctx = Ctx()
            c = mod.Coordinator(ctx, Path(d), lambda k, x='': env.get(k, x))
            store = mod.Store(Path(d) / 'call-cli/tasks.sqlite3')

            dlg_task = c.handle(dict(action='start', agent='agy', cwd=d, prompt='owned'))['task']
            store.update(dlg_task['id'], state='running', runner_pid=123)
            store.native_input_open(dlg_task['id'], 1, dlg_task['launch_id'], 1)
            store.event(dlg_task['id'], '1:2', 'question', 'Q1 first')
            store.event(dlg_task['id'], ' 1:raw ', 'question', 'Q2 raw')

            for bad_action in ('status', 'intervene', 'start', 'list', 'cancel'):
                payload = {'action': bad_action, 'question_ids': ['1:2']}
                if bad_action == 'start':
                    payload.update(agent='omp', cwd=d, prompt='bad')
                elif bad_action != 'list':
                    payload['task_id'] = dlg_task['id']
                    if bad_action == 'intervene':
                        payload.update(prompt='revise', command_id='bad_iv')
                with self.assertRaisesRegex(ValueError, 'question_ids requires reply or dialogue'):
                    c.handle(payload)

            c.handle(dict(action='dialogue', task_id=dlg_task['id'], prompt='held',
                          expected_turn=1, command_id='d_hold', question_ids=['1:2']))
            with self.assertRaisesRegex(ValueError, 'withdraw does not accept'):
                c.handle(dict(action='dialogue', task_id=dlg_task['id'], expected_turn=1,
                              command_id='w_bad', target_command_id='d_hold', withdraw=True,
                              question_ids=['1:2']))
            c.handle(dict(action='dialogue', task_id=dlg_task['id'], expected_turn=1,
                          command_id='w_clean', target_command_id='d_hold', withdraw=True))

            reply_task = c.handle(dict(action='start', agent='omp', cwd=d, prompt='reply owned'))['task']
            store.update(reply_task['id'], state='waiting_reply')
            store.event(reply_task['id'], '1:2', 'question', 'Q1 first')
            store.event(reply_task['id'], ' 1:raw ', 'question', 'Q2 raw')

            invalid_cases = [
                ({'question_id': '1:2', 'question_ids': [' 1:raw ']}, 'mutually exclusive'),
                ({'question_id': '', 'question_ids': ['1:2']}, 'mutually exclusive'),
                ({'question_ids': []}, 'nonempty list'),
                ({'question_ids': None}, 'nonempty list'),
                ({'question_ids': '1:2,1:3'}, 'nonempty list'),
                ({'question_ids': ['']}, 'nonempty strings'),
                ({'question_ids': [1]}, 'nonempty strings'),
                ({'question_ids': ['1:2', '1:2']}, 'duplicates'),
                ({'question_ids': ['1:raw']}, 'exact raw bytes'),
                ({'question_id': '1:2,1:3'}, 'exact raw bytes'),
            ]
            for extra, pattern in invalid_cases:
                with self.subTest(action='dialogue', extra=extra):
                    req = dict(action='dialogue', task_id=dlg_task['id'], prompt='bad',
                               expected_turn=1, command_id='d_bad')
                    req.update(extra)
                    with self.assertRaisesRegex(ValueError, pattern):
                        c.handle(req)
                with self.subTest(action='reply', extra=extra):
                    req = dict(action='reply', task_id=reply_task['id'], prompt='bad',
                               expected_turn=1, command_id='r_bad')
                    req.update(extra)
                    with self.assertRaisesRegex(ValueError, pattern):
                        c.handle(req)

    def test_reply_question_ids_subset_merge_clear_timeout_idempotency_and_no_question(self):
        mod = load_plugin()
        with tempfile.TemporaryDirectory() as d:
            env = {'HERMES_SESSION_ID': 's1', 'HERMES_SESSION_KEY': 'key'}
            ctx = Ctx()
            c = mod.Coordinator(ctx, Path(d), lambda k, x='': env.get(k, x))
            store = mod.Store(Path(d) / 'call-cli/tasks.sqlite3')

            t1 = c.handle(dict(action='start', agent='omp', cwd=d, prompt='first'))['task']
            store.update(t1['id'], state='waiting_reply', timeout=300)
            store.event(t1['id'], '1:2', 'question', 'Q1 first')
            store.event(t1['id'], '1:3', 'question', 'Q2 second')
            store.event(t1['id'], '1:4', 'question', 'Q3 third')

            sub = c.handle(dict(action='reply', task_id=t1['id'], prompt='shared',
                                question_ids=['1:2', '1:3'], clear_timeout=True,
                                expected_turn=1, command_id='r1'))
            self.assertEqual(sub['task']['state'], 'waiting_reply')
            self.assertEqual(sub['task']['turn'], 1)
            self.assertEqual(sub['task']['questions'], ['1:4'])
            self.assertEqual(sub['task']['question_id'], '1:4')
            self.assertEqual(sub['task']['answers'], {'1:2': 'shared', '1:3': 'shared'})
            self.assertEqual(sub['note'], 'Answer recorded; other pending questions remain')
            calls_before = len(ctx.calls)

            sub_replay = c.handle(dict(action='reply', task_id=t1['id'], prompt='shared',
                                       question_ids=['1:3', '1:2'], clear_timeout=True,
                                       expected_turn=1, command_id='r1'))
            self.assertEqual(sub_replay['task'], sub['task'])
            self.assertEqual(len(ctx.calls), calls_before)

            merged = c.handle(dict(action='reply', task_id=t1['id'], prompt='last',
                                   question_id='1:4', expected_turn=1, command_id='r2'))
            self.assertEqual((merged['task']['turn'], merged['task']['state']), (2, 'queued'))
            self.assertEqual(merged['task']['questions'], [])
            self.assertIsNone(merged['task']['timeout'])
            self.assertNotIn('timeout', ctx.called[1])
            self.assertEqual(len(ctx.calls), calls_before + 1)
            self.assertEqual(store.get(t1['id'])['prompt'], '1:2: shared\n1:3: shared\n1:4: last')

            t2 = c.handle(dict(action='start', agent='omp', cwd=d, prompt='single q'))['task']
            store.update(t2['id'], state='waiting_reply', timeout=300)
            store.event(t2['id'], '1:2', 'question', 'Q1 only')
            single = c.handle(dict(action='reply', task_id=t2['id'], prompt='shared',
                                   question_ids=['1:2'], clear_timeout=True,
                                   expected_turn=1, command_id='s1'))
            self.assertEqual((single['task']['turn'], single['task']['state'], single['task']['timeout']),
                             (2, 'queued', None))
            self.assertNotIn('timeout', ctx.called[1])
            self.assertEqual(store.get(t2['id'])['prompt'], 'shared')
            calls_after_single = len(ctx.calls)
            single_replay = c.handle(dict(action='reply', task_id=t2['id'], prompt='shared',
                                          question_ids=['1:2'], clear_timeout=True,
                                          expected_turn=1, command_id='s1'))
            self.assertEqual(single_replay['note'], 'Reply already admitted; no duplicate launch')
            self.assertEqual(len(ctx.calls), calls_after_single)

            t3 = c.handle(dict(action='start', agent='omp', cwd=d, prompt='multi launch'))['task']
            store.update(t3['id'], state='waiting_reply')
            store.event(t3['id'], '1:2', 'question', 'Q1 first')
            store.event(t3['id'], '1:3', 'question', 'Q2 second')
            both = c.handle(dict(action='reply', task_id=t3['id'], prompt='shared',
                                 question_ids=['1:2', '1:3'], expected_turn=1, command_id='m1'))
            self.assertEqual((both['task']['turn'], both['task']['state']), (2, 'queued'))
            self.assertEqual(store.get(t3['id'])['prompt'], '1:2: shared\n1:3: shared')
            calls_after_both = len(ctx.calls)
            both_replay = c.handle(dict(action='reply', task_id=t3['id'], prompt='shared',
                                        question_ids=['1:3', '1:2'], expected_turn=1, command_id='m1'))
            self.assertEqual(both_replay['note'], 'Reply already admitted; no duplicate launch')
            self.assertEqual(len(ctx.calls), calls_after_both)

            t4 = c.handle(dict(action='start', agent='omp', cwd=d, prompt='raw q'))['task']
            store.update(t4['id'], state='waiting_reply')
            store.event(t4['id'], ' 1:raw ', 'question', 'Q1 raw')
            raw_reply = c.handle(dict(action='reply', task_id=t4['id'], prompt='raw ok',
                                      question_ids=[' 1:raw '], expected_turn=1, command_id='raw1'))
            self.assertEqual((raw_reply['task']['turn'], raw_reply['task']['state']), (2, 'queued'))
            self.assertEqual(store.get(t4['id'])['prompt'], 'raw ok')

            t5 = c.handle(dict(action='start', agent='omp', cwd=d, prompt='settled'))['task']
            store.update(t5['id'], state='completed')
            no_q = c.handle(dict(action='reply', task_id=t5['id'], prompt='continue',
                                 expected_turn=1, command_id='noq'))
            self.assertEqual((no_q['task']['turn'], no_q['task']['state']), (2, 'queued'))
            self.assertEqual(store.get(t5['id'])['prompt'], 'continue')
            calls_after_noq = len(ctx.calls)
            no_q_replay = c.handle(dict(action='reply', task_id=t5['id'], prompt='continue',
                                        expected_turn=1, command_id='noq'))
            self.assertEqual(no_q_replay['note'], 'Reply already admitted; no duplicate launch')
            self.assertEqual(len(ctx.calls), calls_after_noq)

    def test_blocker_interaction_seq_schema_validation_forwarding_and_idempotency(self):
        mod = load_plugin()
        prop = mod.SCHEMA['parameters']['properties']['blocker_interaction_seq']
        self.assertEqual(prop['type'], 'integer')
        self.assertEqual(prop['minimum'], 0)

        with tempfile.TemporaryDirectory() as d:
            env = {'HERMES_SESSION_ID': 's1', 'HERMES_SESSION_KEY': 'key'}
            ctx = Ctx()
            c = mod.Coordinator(ctx, Path(d), lambda k, x='': env.get(k, x))
            store = mod.Store(Path(d) / 'call-cli/tasks.sqlite3')

            dlg_task = c.handle(dict(action='start', agent='agy', cwd=d, prompt='owned'))['task']
            store.update(dlg_task['id'], state='running', runner_pid=123,
                         native_observation=dict(
                             turn=1, input_available=True, question_ids=[],
                             question_blockers=[
                                 dict(interaction_seq=0, reason='incomplete_question', available_actions=['observe', 'dialogue', 'cancel'], blocks_completion=True),
                                 dict(interaction_seq=2, reason='history_ambiguous', available_actions=['inspect_origin', 'reconcile_hold'], blocks_completion=True),
                             ],
                         ))
            store.native_input_open(dlg_task['id'], 1, dlg_task['launch_id'], 1)
            store.event(dlg_task['id'], '1:2', 'question', 'Q1 first')

            reply_task = c.handle(dict(action='start', agent='omp', cwd=d, prompt='reply owned'))['task']
            store.update(reply_task['id'], state='waiting_reply')
            store.event(reply_task['id'], '1:2', 'question', 'Q1 first')

            for bad_action, extra in (
                ('reply', dict(task_id=reply_task['id'], prompt='ans', question_id='1:2', expected_turn=1, command_id='r-blk')),
                ('status', dict(task_id=dlg_task['id'])),
                ('intervene', dict(task_id=dlg_task['id'], prompt='rev', command_id='i-blk')),
                ('list', dict()),
                ('start', dict(agent='omp', cwd=d, prompt='bad')),
                ('cancel', dict(task_id=dlg_task['id'])),
            ):
                with self.subTest(bad_action=bad_action), self.assertRaisesRegex(ValueError, 'blocker_interaction_seq requires dialogue'):
                    c.handle(dict(action=bad_action, blocker_interaction_seq=0, **extra))

            for bad_val in (False, True, None, -1, 1.5, '0', [0]):
                with self.subTest(bad_val=bad_val), self.assertRaises(ValueError):
                    c.handle(dict(action='dialogue', task_id=dlg_task['id'], prompt='bad',
                                  expected_turn=1, command_id='d-bad-val', blocker_interaction_seq=bad_val))

            for mix in (
                dict(question_id='1:2', blocker_interaction_seq=0),
                dict(question_id='', blocker_interaction_seq=0),
                dict(question_ids=['1:2'], blocker_interaction_seq=0),
            ):
                with self.subTest(mix=mix), self.assertRaises(ValueError):
                    c.handle(dict(action='dialogue', task_id=dlg_task['id'], prompt='mix',
                                  expected_turn=1, command_id='d-mix', **mix))

            # Non-matching or degraded blocker rejected
            for missing_seq in (1, 2):
                with self.assertRaisesRegex(ValueError, 'incomplete_question'):
                    c.handle(dict(action='dialogue', task_id=dlg_task['id'], prompt='miss',
                                  expected_turn=1, command_id=f'd-miss-{missing_seq}', blocker_interaction_seq=missing_seq))

            # blocker_interaction_seq=0 accepted, withdraw/supersede releases occupancy, settled retry remains idempotent
            d0 = c.handle(dict(action='dialogue', task_id=dlg_task['id'], prompt='review 0',
                               expected_turn=1, command_id='d0', blocker_interaction_seq=0))
            self.assertEqual(d0['task']['commands']['d0']['blocker_interaction_seq'], 0)
            with self.assertRaises(ValueError):
                c.handle(dict(action='dialogue', task_id=dlg_task['id'], expected_turn=1,
                              command_id='w-bad', target_command_id='d0', withdraw=True, blocker_interaction_seq=0))
            c.handle(dict(action='dialogue', task_id=dlg_task['id'], expected_turn=1,
                          command_id='w0', target_command_id='d0', withdraw=True))
            d1 = c.handle(dict(action='dialogue', task_id=dlg_task['id'], prompt='review 0 v2',
                               expected_turn=1, command_id='d1', blocker_interaction_seq=0))
            self.assertEqual(d1['task']['commands']['d1']['blocker_interaction_seq'], 0)

            store.native_dialogue_claim(dlg_task['id'], 1, dlg_task['launch_id'], 1)
            store.native_dialogue_state(dlg_task['id'], 1, dlg_task['launch_id'], 1, 'd1', 'send_started')
            store.native_dialogue_state(dlg_task['id'], 1, dlg_task['launch_id'], 1, 'd1', 'returned')
            store.update(dlg_task['id'], state='completed', questions=[], question_id='',
                         native_input=dict(accepting=False, settling=True, turn=1, launch_id=dlg_task['launch_id'], epoch=1),
                         native_observation=dict(turn=1, input_available=False, question_ids=[], question_blockers=[]))
            retried = c.handle(dict(action='dialogue', task_id=dlg_task['id'], prompt='review 0 v2',
                                    expected_turn=1, command_id='d1', blocker_interaction_seq=0))
            self.assertEqual(retried['task']['state'], 'completed')
            self.assertEqual(retried['task']['commands']['d1']['blocker_interaction_seq'], 0)

    def test_pure_read_actions_never_initialize_missing_database(self):
        import sqlite3
        mod = load_plugin()
        with tempfile.TemporaryDirectory() as d:
            env = {'HERMES_SESSION_ID': 's1', 'HERMES_SESSION_KEY': 'key'}
            ctx = Ctx()
            c = mod.Coordinator(ctx, Path(d), lambda k, x='': env.get(k, x))
            db_dir = Path(d) / 'call-cli'
            self.assertEqual(c.handle({'action': 'list'}), {'tasks': [], 'recovery': []})
            self.assertFalse(db_dir.exists())
            for action, expected in (('status', KeyError), ('events', KeyError),
                                     ('recovery_status', KeyError)):
                with self.subTest(action=action), self.assertRaises(expected):
                    c.handle({'action': action, 'task_id': 'nonexistent'})
                self.assertFalse(db_dir.exists())

    def test_registered_hermes_start_handler_canonicalizes_ons_g4hs_before_launch(self):
        from contextlib import closing
        import shlex
        import sqlite3
        import sys
        import types
        from unittest.mock import patch

        mod = load_plugin()
        expected_runner = Path(mod.__file__).with_name('runner.py').resolve()

        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            hermes_home = root / 'hermes-home'
            work = root / 'work'
            hermes_home.mkdir()
            work.mkdir()
            expected_db = root / 'plugin-data' / 'call-cli' / 'tasks.sqlite3'
            env = {'HERMES_SESSION_ID': 's1', 'HERMES_SESSION_KEY': 'key'}

            def read_db_rows():
                if not expected_db.exists():
                    return {}
                with closing(sqlite3.connect(expected_db.as_uri() + '?mode=ro', uri=True)) as db:
                    return {row[0]: json.loads(row[1])
                            for row in db.execute('SELECT id, data FROM tasks').fetchall()}

            test_case = self

            class RecordingCtx(Ctx):
                def __init__(self):
                    self.calls = []
                    self.snapshots = []

                def dispatch_tool(self, name, args, **kwargs):
                    test_case.assertEqual(name, 'terminal')
                    argv = shlex.split(args['command'])
                    test_case.assertIn('--db', argv)
                    test_case.assertIn('--task', argv)
                    test_case.assertEqual(Path(argv[1]).resolve(), expected_runner)
                    test_case.assertEqual(Path(argv[argv.index('--db') + 1]).resolve(), expected_db)
                    task_id = argv[argv.index('--task') + 1]
                    rows = read_db_rows()
                    test_case.assertIn(task_id, rows)
                    self.snapshots.append(rows[task_id])
                    self.calls.append((name, args))
                    return json.dumps({
                        'session_id': f'proc-owned-{len(self.calls)}',
                        'pid': 123,
                        'notify_on_complete': True,
                    })

            constants = types.ModuleType('hermes_constants')
            constants.get_hermes_home = lambda: hermes_home
            constants.get_default_hermes_root = lambda **kw: root
            profiles = types.ModuleType('hermes_cli.profiles')
            profiles.get_active_profile_name = lambda: 'default'
            session = types.ModuleType('gateway.session_context')
            session.get_session_env = lambda k, d='': env.get(k, d)
            modules = {
                'hermes_constants': constants,
                'hermes_cli.profiles': profiles,
                'gateway.session_context': session,
            }

            ctx = RecordingCtx()
            with patch.dict(sys.modules, modules):
                mod.register(ctx)
                props = ctx.registration['schema']['parameters']['properties']
                self.assertIn('oc/oa/on/4c/4a/4h/4n/g4c/g4a/g4h/g4n', props['model']['description'])
                handler = ctx.registration['handler']

                for shorthand, expected_model in (
                    ('ons', 'gemini-pro-orchestrator-neon'),
                    ('g4hs', 'gemini-4-hydrogen'),
                ):
                    with self.subTest(shorthand=shorthand):
                        res = json.loads(handler({
                            'action': 'start',
                            'agent': 'agy',
                            'cwd': str(work),
                            'prompt': 'synthetic',
                            'model': shorthand,
                        }))
                        self.assertNotIn('error', res)
                        task = res['task']
                        self.assertEqual(
                            (task['model'], task['agy_swarm'], task.get('agy_mode')),
                            (expected_model, 'native', 'swarm'),
                        )
                        snap = ctx.snapshots[-1]
                        self.assertEqual(snap['id'], task['id'])
                        self.assertEqual(
                            (snap['model'], snap['agy_swarm'], snap.get('agy_mode')),
                            (expected_model, 'native', 'swarm'),
                        )
                        self.assertNotEqual(snap['model'], shorthand)

                calls_before = len(ctx.calls)
                rows_before = len(read_db_rows())
                for bad_extra in (
                    {'model': 'ons', 'agy_swarm': 'off'},
                    {'model': 'g4hs', 'agy_mode': 'single'},
                    {'model': 'g4hs', 'agy_mode': 'dynamic'},
                ):
                    with self.subTest(mismatch=bad_extra):
                        res = json.loads(handler({
                            'action': 'start',
                            'agent': 'agy',
                            'cwd': str(work),
                            'prompt': 'synthetic',
                            **bad_extra,
                        }))
                        self.assertEqual(res.get('error_type'), 'ModelSelectionError')
                        self.assertEqual(len(ctx.calls), calls_before)
                        self.assertEqual(len(read_db_rows()), rows_before)

                for extra, expected in (
                    ({'model': 'gemini-pro-orchestrator-neon', 'agy_swarm': 'off'},
                     ('gemini-pro-orchestrator-neon', 'off', None)),
                    ({'model': 'gemini-4-hydrogen'},
                     ('gemini-4-hydrogen', 'native', 'swarm')),
                    ({'model': 'gemini-4-argon'},
                     ('gemini-4-argon', 'off', None)),
                    ({'model': 'gemini-4-hydrogen', 'agy_mode': 'dynamic'},
                     ('gemini-4-hydrogen', 'native', 'dynamic')),
                ):
                    with self.subTest(full_id=extra):
                        res = json.loads(handler({
                            'action': 'start',
                            'agent': 'agy',
                            'cwd': str(work),
                            'prompt': 'synthetic',
                            **extra,
                        }))
                        self.assertNotIn('error', res)
                        task = res['task']
                        self.assertEqual(
                            (task['model'], task['agy_swarm'], task.get('agy_mode')),
                            expected,
                        )
                        snap = ctx.snapshots[-1]
                        self.assertEqual(snap['id'], task['id'])
                        self.assertEqual(
                            (snap['model'], snap['agy_swarm'], snap.get('agy_mode')),
                            expected,
                        )


if __name__=='__main__':unittest.main()
