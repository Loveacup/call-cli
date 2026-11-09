import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


def load_plugin():
    spec=importlib.util.spec_from_file_location('callcli_plugin',Path(__file__).with_name('__init__.py'),submodule_search_locations=[str(Path(__file__).parent)])
    mod=importlib.util.module_from_spec(spec)
    import sys
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


class PluginTests(unittest.TestCase):
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
            resumed = c.handle({'action':'reply','task_id':task['id'],'prompt':'continue'})['task']
            self.assertEqual(resumed['model'], 'live-family')
            self.assertEqual(resumed['effort'], 'low')
            self.assertEqual(resumed['session'], task['session'])
            self.assertEqual(resumed['agy_approval'], 'approve-reads')
            default = c.handle({'action':'start','agent':'agy','cwd':d,'prompt':'inspect','model':'live-family'})['task']
            self.assertEqual(default['agy_approval'], 'approve-all')
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
            c.handle({'action':'events','task_id':task['id'],'after':0})
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
            c.handle({'action':'intervene','task_id':task['id'],'prompt':'new','command_id':'preprovider'})
            store.finish_turn(task['id'],1,'cancelled',0,'cancelled')
            c.handle({'action':'status','task_id':task['id']})
            self.assertEqual(store.get(task['id'])['turn'],2)
            self.assertEqual(__import__('runner').run_task(store.path,task['id']),0)
            final=store.get(task['id'])
            self.assertEqual(final['state'],'completed')
            self.assertTrue(final['acpx_created'])
            self.assertFalse(final['needs_session'])
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
                self.assertEqual(runner.run_task(store.path,task['id']),0)
            self.assertNotIn('session_creation_attempted',store.get(task['id']))
            c.handle({'action':'intervene','task_id':task['id'],'prompt':'new','command_id':'legacy'})
            self.assertEqual(runner.run_task(store.path,task['id']),0)
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
            self.assertEqual(runner.run_task(store.path,task['id']),0)
            with store.connect() as db:
                current=json.loads(db.execute('SELECT data FROM tasks WHERE id=?',(task['id'],)).fetchone()[0])
                for key in ('needs_session','acpx_created','session_creation_attempted'):current.pop(key)
                store._save(db,current)
            record=current['acpx_record_id']
            c.handle({'action':'intervene','task_id':task['id'],'prompt':'next','command_id':'existing'})
            self.assertEqual(runner.run_task(store.path,task['id']),0)
            self.assertEqual(store.get(task['id'])['acpx_record_id'],record)
            calls=[json.loads(line) for line in (fixture.root/'calls.jsonl').read_text().splitlines()]
            self.assertEqual(sum('new' in call for call in calls),1)
        finally:fixture.tearDown()

    def test_register_exposes_real_bound_tool(self):
        mod=load_plugin();ctx=Ctx();mod.register(ctx)
        self.assertEqual(ctx.registration['name'],'call_cli')
        self.assertTrue(callable(ctx.registration['handler']))
        self.assertTrue(ctx.registration['check_fn']())
        ctx.enabled=False
        self.assertFalse(ctx.registration['check_fn']())


if __name__=='__main__':unittest.main()
