import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from collab import Collaboration
from runner import control
from backends import backend_for


class AgyTests(unittest.TestCase):
    def test_approval_is_persistent_and_explicit_reads_overrides_write(self):
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            client = c.connect('codex')['client_id']
            task = c.start(client, agent='agy', cwd=root, prompt='inspect', model='advertised',
                           write=True, agy_approval='approve-reads')
            c.store.update(task['id'], state='completed')
            task = c.intervene(client, task['id'], 'continue')
            task.update(acpx_record_id='11111111-1111-4111-8111-111111111111', execution_version='0.22.0')
            options = json.loads(backend_for(task['agent']).transport(task)[1])['options']
            self.assertEqual(options['permissionMode'], 'approve-reads')
            self.assertEqual(c.status(client, task['id'])['agy_approval'], 'approve-reads')

    def test_new_agy_approval_and_legacy_other_agents_remain_distinct(self):
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            client = c.connect('codex')['client_id']
            task = c.start(client, agent='agy', cwd=root, prompt='inspect', model='advertised')
            self.assertEqual(task['agy_approval'], 'skip-permissions')
            self.assertEqual(task['execution_policy'], 'native-skip')
            explicit_all = c.start(client, agent='agy', cwd=root, prompt='inspect', model='advertised', agy_approval='approve-all')
            self.assertEqual(explicit_all['agy_approval'], 'approve-all')
            self.assertNotIn('execution_policy', explicit_all)
            for agent in ('omp', 'codex', 'pi'):
                with self.assertRaises(ValueError):
                    c.start(client, agent=agent, cwd=root, prompt='inspect', agy_approval='approve-all')
                other = c.start(client, agent=agent, cwd=root, prompt='inspect')
                self.assertNotIn('agy_approval', other)
                self.assertEqual(other['execution_policy'], 'native-skip')
                for write in (False, True):
                    other_task = dict(other, write=write, acpx_record_id='11111111-1111-4111-8111-111111111111', execution_version='0.22.0')
                    options = json.loads(backend_for(agent).transport(other_task)[1])['options']
                    self.assertEqual(options['permissionMode'], 'approve-all')
            for agent in ('agy', 'omp', 'codex', 'pi'):
                for write in (False, True):
                    legacy = dict(task, agent=agent, write=write)
                    legacy.pop('agy_approval', None)
                    legacy.pop('execution_policy', None)
                    legacy.update(acpx_record_id='11111111-1111-4111-8111-111111111111', execution_version='0.22.0')
                    options = json.loads(backend_for(legacy['agent']).transport(legacy)[1])['options']
                    self.assertEqual(options['permissionMode'], 'approve-all' if write else 'approve-reads')
            with self.assertRaises(ValueError):
                c.start(client, agent='agy', cwd=root, prompt='inspect', model='advertised', agy_approval='typo')

    def test_agy_model_survives_continuation_and_transport(self):
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            client = c.connect('codex')['client_id']
            t = c.start(client, agent='agy', agy_swarm='off', cwd=root, prompt='inspect', model='advertised-family', effort='low')
            c.store.update(t['id'], state='completed')
            t = c.intervene(client, t['id'], 'continue')
            self.assertEqual(t['model'], 'advertised-family')
            self.assertEqual(t['effort'], 'low')
            t.update(acpx_record_id='11111111-1111-4111-8111-111111111111', execution_version='0.22.0')
            _, payload, _ = backend_for(t['agent']).transport(t)
            self.assertEqual(json.loads(payload)['options']['model'], t['model'])
            self.assertEqual(json.loads(payload)['options']['effort'], 'low')
            with patch('acpx_backend.subprocess.run') as run:
                control(t, 'new')
                args = run.call_args.args[0]
                self.assertEqual(args[args.index('--agent')+1], 'agy acp --dangerously-skip-permissions')
                self.assertIn('--approve-all', args)
                self.assertNotIn('agy', args)
                self.assertEqual(args[args.index('--model')+1], t['model'])

    def test_selection_controls_and_readback_fail_closed(self):
        from types import SimpleNamespace
        from acpx_backend import AcpxBackend
        backend = AcpxBackend()
        task = {'model':'live-family', 'effort':'low', 'session':'owned'}
        def response(t, action, **kw):
            if action == 'show':
                return SimpleNamespace(returncode=0, stdout=json.dumps({'name':'owned','acpx':{'current_model_id':'live-family','config_options':[{'id':'thought_level','currentValue':'low'}]}}).encode())
            return SimpleNamespace(returncode=0)
        with patch.object(backend, 'control', side_effect=response) as ctl:
            self.assertEqual(backend.apply_selection(task), {'model':'live-family','effort':'low'})
            self.assertEqual([c.args[1] for c in ctl.call_args_list], ['model','effort','show'])
        with patch.object(backend, 'control', return_value=SimpleNamespace(returncode=1)) as ctl:
            with self.assertRaises(RuntimeError): backend.apply_selection(task)
            self.assertEqual(ctl.call_count, 1)
        self.assertEqual(backend.apply_selection({'session':'legacy'}), {})

    def test_approval_does_not_reserve_read_task_but_declared_writers_conflict_across_clis(self):
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            a = c.connect('omp')['client_id']
            b = c.connect('pi')['client_id']
            writer = c.start(a, agent='omp', cwd=root, prompt='edit', write=True)
            reader = c.start(b, agent='agy', cwd=root, prompt='inspect', model='advertised')
            self.assertEqual((reader['agy_approval'], reader['execution_policy'], reader['write']),
                             ('skip-permissions', 'native-skip', False))
            with self.assertRaisesRegex(ValueError, writer['id']):
                c.start(b, agent='agy', cwd=root, prompt='edit', model='advertised', write=True)

    def test_model_specific_swarm_defaults_and_explicit_overrides_persist(self):
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            client = c.connect('codex')['client_id']
            for model, explicit, expected in [(None, None, 'native'), ('gemini-4-carbon-medium', None, 'native'),
                    ('gemini-4-argon-low', None, 'off'), ('gemini-4-argon', None, 'off'),
                    ('gemini-4-argon-low', 'native', 'native'), ('gemini-4-carbon-medium', 'off', 'off'),
                    (None, 'off', 'off')]:
                with self.subTest(model=model, explicit=explicit):
                    task = c.start(client, agent='agy', model=model, agy_swarm=explicit, cwd=root, prompt='inspect')
                    self.assertEqual(task['agy_swarm'], expected)
                    c.store.update(task['id'], state='completed')
                    continued = c.intervene(client, task['id'], 'continue')
                    self.assertEqual(continued['agy_swarm'], expected)
            task = c.start(client, agent='agy', model='gemini-4-argon-low', agy_swarm='native',
                           agy_worker_model='gemini-4-hydrogen-low', cwd=root, prompt='polish')
            self.assertEqual(task['agy_worker_model'], 'gemini-4-hydrogen-low')

    def test_shorthand_model_and_route_are_persisted_before_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            launched=[]
            c=Collaboration(Path(root)/'tasks.db',launch=launched.append)
            client=c.connect('codex')['client_id']
            cases=((None,'gemini-pro-orchestrator','native'),
                   ('oc','gemini-pro-orchestrator','off'),('ocs','gemini-pro-orchestrator','native'),
                   ('oa','gemini-pro-orchestrator-argon','off'),('oas','gemini-pro-orchestrator-argon','native'),
                   ('4c','gemini-4-carbon','off'),('4cs','gemini-4-carbon','native'),
                   ('4a','gemini-4-argon','off'),('4as','gemini-4-argon','native'),
                   ('4h','gemini-4-hydrogen','off'),('4hs','gemini-4-hydrogen','native'))
            for shorthand,model,mode in cases:
                with self.subTest(shorthand=shorthand):
                    task=c.start(client,agent='agy',cwd=root,prompt='synthetic',model=shorthand)
                    self.assertEqual((task['model'],task['agy_swarm']),(model,mode))
                    self.assertEqual((launched[-1]['model'],launched[-1]['agy_swarm']),(model,mode))
                    self.assertEqual(c.status(client,task['id'])['model'],model)

    def test_shorthand_route_conflicts_reject_without_task_or_launch(self):
        with tempfile.TemporaryDirectory() as root:
            launched=[]
            c=Collaboration(Path(root)/'tasks.db',launch=launched.append)
            client=c.connect('omp')['client_id']
            for base in ('oc','oa','4c','4a','4h'):
                for model,mode in ((base,'native'),(base+'s','off'),(base,'typo')):
                    with self.subTest(model=model,mode=mode):
                        with self.assertRaisesRegex(ValueError,'conflict|agy_swarm'):
                            c.start(client,agent='agy',cwd=root,prompt='never send',model=model,agy_swarm=mode)
            self.assertEqual(c.list(client),[])
            self.assertEqual(launched,[])

    def test_teamwork_shorthand_and_explicit_route_persist_before_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            launched=[]
            c=Collaboration(Path(root)/'tasks.db',launch=launched.append)
            client=c.connect('omp')['client_id']
            for fields,expected in [({'model':'oct'},'gemini-pro-orchestrator'),
                    ({'model':'oat'},'gemini-pro-orchestrator-argon'),
                    ({'model':'4ct'},'gemini-4-carbon'),({'model':'4at'},'gemini-4-argon'),
                    ({'model':'4ht'},'gemini-4-hydrogen'),
                    ({'agy_mode':'teamwork'},'gemini-pro-orchestrator')]:
                task=c.start(client,agent='agy',cwd=root,prompt='native task',**fields)
                self.assertEqual((task['model'],task['agy_mode'],task['agy_swarm']),
                    (expected,'teamwork','native'))
                self.assertEqual(launched[-1]['agy_mode'],'teamwork')
                self.assertEqual(c.status(client,task['id'])['agy_approval'],'skip-permissions')
            for fields in ({'model':'oct','agy_mode':'swarm'}, {'model':'ocs','agy_mode':'teamwork'},
                    {'model':'oct','agy_swarm':'off'}):
                with self.assertRaisesRegex(ValueError,'conflict'):
                    c.start(client,agent='agy',cwd=root,prompt='never send',**fields)
            self.assertEqual(len(c.list(client)),6)

    def test_malformed_shorthand_never_dispatches(self):
        with tempfile.TemporaryDirectory() as root:
            launched=[]
            c=Collaboration(Path(root)/'tasks.db',launch=launched.append)
            client=c.connect('omp')['client_id']
            for model in ('OC','OCS','4AS','ocss','ocst','oats','4css','4ast','oc/oa','4a+4c',
                          'oc,oa','oc_oa','oc:oa','4a4c','oc s','ocs-low',' oc','4a ', '',42,True,[],{}):
                with self.subTest(model=model):
                    with self.assertRaises(ValueError):
                        c.start(client,agent='agy',cwd=root,prompt='never send',model=model)
            self.assertEqual(c.list(client),[])
            self.assertEqual(launched,[])

    def test_shorthand_matching_options_and_exact_ids_keep_their_contracts(self):
        from backends import prepare_start
        for model,mode,expected in (('oc','off','gemini-pro-orchestrator'),
                ('ocs','native','gemini-pro-orchestrator'),('4a','off','gemini-4-argon'),
                ('4as','native','gemini-4-argon')):
            fields=prepare_start('agy',model,agy_swarm=mode)
            self.assertEqual((fields['model'],fields['agy_swarm']),(expected,mode))
        for exact,mode in (('gemini-pro-orchestrator','native'),('gemini-pro-orchestrator-argon','off'),
                ('gemini-4-carbon-low','native'),('gemini-4-argon-medium','off'),
                ('advertised-model','native'),('ocelot-exact','native'),
                ('octopus-exact','native'),('oc-special','native')):
            self.assertEqual(prepare_start('agy',exact)['model'],exact)
            self.assertEqual(prepare_start('agy',exact)['agy_swarm'],mode)
            self.assertEqual(prepare_start('agy',exact,agy_swarm='off')['agy_swarm'],'off')
        fields=prepare_start('agy','4as','low',agy_worker_model='gemini-4-hydrogen-medium')
        self.assertEqual((fields['effort'],fields['agy_worker_model']),('low','gemini-4-hydrogen-medium'))
        with self.assertRaisesRegex(ValueError,'requires native'):
            prepare_start('agy','4a',agy_worker_model='gemini-4-hydrogen-medium')
        for agent in ('omp','codex','pi'):
            for model in ('oc','ocs','oct','4as','ocss'):
                self.assertEqual(prepare_start(agent,model),{'model':model,'execution_policy':'native-skip'})

    def test_legacy_model_intent_is_not_reparsed_on_continuation(self):
        with tempfile.TemporaryDirectory() as root:
            c=Collaboration(Path(root)/'tasks.db',launch=lambda task:None)
            client=c.connect('omp')['client_id']
            for fields in ({},{'model':'oc','agy_swarm':'native'},
                           {'model':'gemini-4-carbon','agy_swarm':'native'},
                           {'model':'gemini-pro-orchestrator','agy_swarm':'off'}):
                old=c.store.create(c.origin(client),'local','agy',root,'old',False)
                c.store.update(old['id'],state='completed',**fields)
                before=c.store.get(old['id'])
                c.list(client); c.status(client,old['id'])
                self.assertEqual(c.store.get(old['id']),before)
                continued=c.intervene(client,old['id'],'continue')
                for key in ('model','agy_swarm'):
                    self.assertEqual(continued.get(key),before.get(key))

    def test_ocs_default_and_orchestrator_overrides_do_not_migrate_old_tasks(self):
        from backends import prepare_start
        with tempfile.TemporaryDirectory() as root:
            c=Collaboration(Path(root)/'tasks.db',launch=lambda task:None)
            client=c.connect('omp')['client_id']
            for model,mode,expected_model,expected_mode in (
                (None,None,'gemini-pro-orchestrator','native'),
                (None,'off','gemini-pro-orchestrator','off'),
                ('gemini-pro-orchestrator',None,'gemini-pro-orchestrator','native'),
                ('gemini-pro-orchestrator-argon',None,'gemini-pro-orchestrator-argon','off'),
                ('gemini-pro-orchestrator-argon','native','gemini-pro-orchestrator-argon','native'),
                ('gemini-4-carbon',None,'gemini-4-carbon','native'),
                ('gemini-4-argon',None,'gemini-4-argon','off')):
                task=c.start(client,agent='agy',cwd=root,prompt='synthetic',model=model,agy_swarm=mode)
                self.assertEqual((task.get('model'),task['agy_swarm']),(expected_model,expected_mode))
            old=c.store.create(c.origin(client),'local','agy',root,'old',False)
            before=c.store.get(old['id'])
            c.list(client); c.status(client,old['id'])
            self.assertEqual(c.store.get(old['id']),before)
            for agent in ('omp','codex','pi'):
                self.assertEqual(prepare_start(agent),{'execution_policy':'native-skip'})

    def test_fatal_without_structured_auth_and_late_results_do_not_gain_auth_cause(self):
        from events import EventDecoder
        from agy import AGY_FATAL_LINE, outcome_note
        for category in (None, 'executor_401', 'PRIVATE_CATEGORY'):
            decoder = EventDecoder(shared_request_id='owned:1')
            decoder.feed({'type':'callcli.shared.event', 'requestId':'owned:1', 'event':{
                'type':'text_delta', 'stream':'output', 'tag':'agent_message_chunk', 'text':AGY_FATAL_LINE+'\n'}})
            decoder.feed({'type':'callcli.shared.result', 'requestId':'owned:1',
                          'status':'completed', 'stopReason':'end_turn', 'errorCategory':category})
            decoder.feed({'type':'callcli.shared.result', 'requestId':'owned:1',
                          'status':'failed', 'stopReason':None, 'errorCategory':'auth_required'})
            self.assertIsNone(decoder.error_category)
            note = outcome_note(decoder, 0, 'completed')
            self.assertIn('outcome unknown', note)
            self.assertNotIn('authentication', note)
            self.assertNotIn('401', note)
            self.assertNotIn('PRIVATE', note)

    def test_native_skip_cli_flags_and_env_across_all_four_backends(self):
        from types import SimpleNamespace
        from acpx_backend import AcpxBackend
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root)/'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']

            omp_task = c.start(client, agent='omp', cwd=root, prompt='inspect')
            with patch('acpx_backend.subprocess.run') as run:
                control(omp_task, 'new')
                args = run.call_args.args[0]
                self.assertEqual(args[args.index('--agent')+1], 'omp acp --auto-approve --allow-home')
                self.assertIn('--approve-all', args)
                self.assertEqual(args[args.index('--non-interactive-permissions')+1], 'deny')
            win_omp = dict(omp_task, device='winbox', shell='powershell', cwd='C:/work')
            self.assertEqual(backend_for('omp').agent_args(win_omp), ['--agent', 'omp acp --auto-approve --allow-home'])
            self.assertEqual(backend_for('omp').agent_argv(win_omp), ['omp', 'acp', '--auto-approve', '--allow-home'])
            with patch('acpx_backend.subprocess.run') as run:
                import base64
                control(win_omp, 'new')
                win_cmd = run.call_args.args[0]
                self.assertEqual(win_cmd[0], 'ssh')
                ps_script = base64.b64decode(win_cmd[-1].split()[-1]).decode('utf-16le')
                self.assertIn("'--' '--cwd'", ps_script)
                self.assertIn('--auto-approve', ps_script)
                self.assertIn('--allow-home', ps_script)
                self.assertIn('node', ps_script)
                win_payload = json.loads(run.call_args.kwargs['input'])
                self.assertEqual(win_payload['options']['agentArgv'], ['omp', 'acp', '--auto-approve', '--allow-home'])
                self.assertEqual(win_payload['options']['permissionMode'], 'approve-all')
                self.assertEqual(win_payload['options']['action'], 'new')
                bound = dict(win_omp, acpx_record_id='11111111-1111-4111-8111-111111111111', acpx_session_id='saved-acp')
                for action in ('show', 'status', 'cancel', 'close', 'model', 'mode'):
                    control(dict(bound, model='selected'), action)
                    options = json.loads(run.call_args.kwargs['input'])['options']
                    self.assertEqual(options['recordId'], bound['acpx_record_id'])
                    self.assertEqual(options['backendSessionId'], 'saved-acp')

            codex_task = c.start(client, agent='codex', cwd=root, prompt='inspect')
            codex_task.update(acpx_record_id='11111111-1111-4111-8111-111111111111', execution_version='0.22.0')
            payload = json.loads(backend_for('codex').transport(codex_task)[1])
            self.assertEqual(payload['options']['permissionMode'], 'approve-all')
            self.assertEqual(payload['options']['agentEnv'], {'INITIAL_AGENT_MODE': 'agent-full-access'})
            with patch('acpx_backend.subprocess.run') as run:
                control(codex_task, 'new')
                self.assertEqual(run.call_args.kwargs['env']['INITIAL_AGENT_MODE'], 'agent-full-access')
                self.assertNotIn('env', run.call_args.args[0][0])
            ssh_posix = AcpxBackend().control(dict(codex_task, device='macbook', shell='posix'), 'new') if False else None
            del ssh_posix
            with patch('acpx_backend.subprocess.run') as run:
                control(dict(codex_task, device='macbook', shell='posix'), 'new')
                self.assertIn('INITIAL_AGENT_MODE=agent-full-access', run.call_args.args[0][-1])
                self.assertNotIn('env', run.call_args.kwargs)
                control(dict(codex_task, device='winbox', shell='powershell'), 'mode')
                self.assertEqual(run.call_args.args[0][0], 'ssh')

            pi_task = c.start(client, agent='pi', cwd=root, prompt='inspect')
            pi_task.update(acpx_record_id='11111111-1111-4111-8111-111111111111', execution_version='0.22.0')
            pi_opts = json.loads(backend_for('pi').transport(pi_task)[1])['options']
            self.assertEqual(pi_opts['permissionMode'], 'approve-all')
            with patch('acpx_backend.subprocess.run') as run:
                control(pi_task, 'new')
                self.assertIn('--approve-all', run.call_args.args[0])

            agy_skip = c.start(client, agent='agy', cwd=root, prompt='inspect', model='advertised')
            self.assertIn('skip-permissions', backend_for('agy').prompt_guidance(agy_skip))
            self.assertEqual(backend_for('agy').agent_args(dict(agy_skip, agy_swarm='off', device='winbox', shell='powershell')),
                             ['--agent', 'agy acp --dangerously-skip-permissions'])
            with patch('acpx_backend.subprocess.run') as run:
                import base64
                control(dict(agy_skip, agy_swarm='off', device='winbox', shell='powershell', cwd='C:/work'), 'new')
                ps_script = base64.b64decode(run.call_args.args[0][-1].split()[-1]).decode('utf-16le')
                self.assertIn('--dangerously-skip-permissions', ps_script)
                win_agy_payload = json.loads(run.call_args.kwargs['input'])
                self.assertEqual(win_agy_payload['options']['agentArgv'], ['agy', 'acp', '--dangerously-skip-permissions'])
                self.assertEqual(win_agy_payload['options']['permissionMode'], 'approve-all')
            agy_explicit = c.start(client, agent='agy', cwd=root, prompt='inspect', model='advertised', agy_approval='approve-all')
            self.assertEqual(backend_for('agy').agent_args(agy_explicit), ['--agent', 'agy acp'])
            self.assertEqual(backend_for('agy').agent_args(dict(agy_explicit, agy_swarm='off', device='winbox', shell='powershell')),
                             ['--agent', 'agy acp'])

            legacy = dict(codex_task)
            legacy.pop('execution_policy', None)
            self.assertEqual(backend_for('codex').agent_env(legacy), {})
            self.assertEqual(backend_for('omp').agent_args(dict(omp_task, execution_policy=None)), ['omp'])

    def test_codex_native_skip_session_create_sets_agent_full_access_mode(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from acpx_backend import AcpxBackend
        calls = []
        class _Store:
            def __init__(self, task): self.task = dict(task)
            def update(self, _tid, **kw): self.task.update(kw); return dict(self.task)
            def get(self, _tid): return dict(self.task)
        backend = AcpxBackend()
        task = {'id': 't1', 'agent': 'codex', 'cwd': '/tmp', 'device': 'local', 'shell': 'posix',
                'session': 's1', 'turn': 1, 'state': 'running', 'write': False,
                'version_check': {}, 'execution_policy': 'native-skip', 'prompt': 'hi'}
        def fake_control(t, action, timeout=20):
            calls.append(action)
            if action == 'new':
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    'action': 'session_ensured', 'acpxRecordId': '11111111-1111-4111-8111-111111111111', 'acpxSessionId': 'acp-1'}).encode())
            if action == 'mode':
                return SimpleNamespace(returncode=1, stdout=b'')
            return SimpleNamespace(returncode=0, stdout=b'')
        emitted = []
        with patch('updates.device_lock', side_effect=lambda *a, **kw: nullcontext(True)):
            with patch.object(backend, 'control', side_effect=fake_control):
                res = backend.run(_Store(task), task, lambda kind, text, **kw: emitted.append((kind, text, kw)))
        self.assertEqual(calls, ['new', 'mode'])
        self.assertEqual((res.state, res.stop_reason), ('unknown', None))
        self.assertIn('reason=setup_failed', res.note)
        self.assertEqual(emitted[-1][2]['reason_code'], 'setup_failed')

    def test_native_owner_skip_permissions_argv_and_allow_always_fallback(self):
        from types import SimpleNamespace
        from agy_native import NativeOwner
        from transport import command as transport_cmd
        with patch('agy_native.subprocess.Popen') as popen:
            popen.return_value = SimpleNamespace(stdout=SimpleNamespace(fileno=lambda: 0))
            with patch('agy_native.selectors.DefaultSelector') as sel:
                sel.return_value = SimpleNamespace(register=lambda *a: None)
                owner = NativeOwner({'device': 'local', 'shell': 'posix', 'cwd': '/tmp',
                                     'execution_policy': 'native-skip', 'agy_approval': 'skip-permissions'})
        self.assertEqual(popen.call_args.args[0], ['agy', 'acp', '--disable-telemetry', '--dangerously-skip-permissions'])
        sent = []
        owner.session_id = 'owned'
        owner.task = {'execution_policy': 'native-skip', 'agy_approval': 'skip-permissions'}
        owner.permission_decisions = []
        owner.send = sent.append
        owner._callback({'id': 9, 'method': 'session/request_permission', 'params': {
            'sessionId': 'owned',
            'toolCall': {'toolCallId': 't1', 'kind': 'execute'},
            'options': [{'optionId': 'always', 'kind': 'allow_always'}, {'optionId': 'deny', 'kind': 'reject_once'}],
        }})
        self.assertEqual(sent[-1]['result']['outcome'], {'outcome': 'selected', 'optionId': 'always'})
        self.assertEqual((owner.permission_decisions[-1]['choice'], owner.permission_decisions[-1]['allowed']),
                         ('allow_always', True))
        self.assertEqual(transport_cmd('local', 'posix', ['acpx'], env={'INITIAL_AGENT_MODE': 'agent-full-access'}),
                         ['env', 'INITIAL_AGENT_MODE=agent-full-access', 'acpx'])
        with self.assertRaises(ValueError):
            transport_cmd('local', 'posix', ['acpx'], env={'bad-key': '1'})
