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
        self.called=(name,args)
        return json.dumps({'session_id':'proc-owned','pid':123,'notify_on_complete':True})


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

    def test_register_exposes_real_bound_tool(self):
        mod=load_plugin();ctx=Ctx();mod.register(ctx)
        self.assertEqual(ctx.registration['name'],'call_cli')
        self.assertTrue(callable(ctx.registration['handler']))
        self.assertTrue(ctx.registration['check_fn']())
        ctx.enabled=False
        self.assertFalse(ctx.registration['check_fn']())


if __name__=='__main__':unittest.main()
