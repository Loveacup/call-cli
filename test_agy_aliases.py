"""Table-driven regressions for AGY model aliases (on/4n/g4* bare/s/t)."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agy_native import NativeOwner, ProtocolError
from backends import ModelSelectionError, prepare_start
import collab
from collab import Collaboration
from omp_bridge import HostBridge
from test_plugin import Ctx, load_plugin


ALIAS_BASE_MAP = (
    ('oc', 'gemini-pro-orchestrator'),
    ('oa', 'gemini-pro-orchestrator-argon'),
    ('on', 'gemini-pro-orchestrator-neon'),
    ('4c', 'gemini-4-carbon'),
    ('4a', 'gemini-4-argon'),
    ('4h', 'gemini-4-hydrogen'),
    ('4n', 'gemini-4-neon'),
    ('g4c', 'gemini-4-carbon'),
    ('g4a', 'gemini-4-argon'),
    ('g4h', 'gemini-4-hydrogen'),
    ('g4n', 'gemini-4-neon'),
)

ALL_SHORTHAND_CASES = [
    (
        base + suffix,
        expected_model,
        'native' if suffix else 'off',
        'teamwork' if suffix == 't' else ('swarm' if suffix == 's' else None),
        'teamwork' if suffix == 't' else ('swarm' if suffix == 's' else 'single'),
    )
    for base, expected_model in ALIAS_BASE_MAP
    for suffix in ('', 's', 't')
]


def _neon_catalog():
    """Extend the static AGY catalog fixture with Neon standalone/family models."""
    catalog = json.loads(
        (Path(__file__).parent / 'fixtures/agy_orchestrator_catalog.json').read_text()
    )
    neon_models = [
        {'modelId': 'gemini-4-neon-low', 'name': 'Gemini 4 Neon (Low)'},
        {'modelId': 'gemini-4-neon-medium', 'name': 'Gemini 4 Neon (Medium)'},
        {'modelId': 'gemini-4-neon-high', 'name': 'Gemini 4 Neon (High)'},
        {'modelId': 'gemini-4-neon-xhigh', 'name': 'Gemini 4 Neon (XHigh)'},
        {'modelId': 'gemini-pro-orchestrator-neon', 'name': 'Orchestrator (Neon)'},
    ]
    neon_options = [
        {'value': 'gemini-4-neon', 'name': 'Gemini 4 Neon'},
        {'value': 'gemini-pro-orchestrator-neon', 'name': 'Orchestrator (Neon)'},
    ]

    def extend_meta(meta, *, include_xhigh_thought=False):
        meta['models']['availableModels'] = list(meta['models']['availableModels']) + neon_models
        for opt in meta['configOptions']:
            if opt['id'] == 'model':
                opt['options'] = list(opt['options']) + neon_options
            elif opt['id'] == 'thought_level' and include_xhigh_thought:
                opt['options'] = list(opt['options']) + [{'value': 'xhigh', 'name': 'XHigh'}]

    extend_meta(catalog['initial'], include_xhigh_thought=False)
    for selected_meta in catalog['selected'].values():
        extend_meta(selected_meta, include_xhigh_thought=False)

    orch_neon = json.loads(json.dumps(catalog['selected']['gemini-pro-orchestrator']))
    orch_neon['models']['currentModelId'] = 'gemini-pro-orchestrator-neon'
    for opt in orch_neon['configOptions']:
        if opt['id'] == 'model':
            opt['currentValue'] = 'gemini-pro-orchestrator-neon'
    catalog['selected']['gemini-pro-orchestrator-neon'] = orch_neon

    g4n_meta = json.loads(json.dumps(catalog['initial']))
    extend_meta(g4n_meta, include_xhigh_thought=True)
    g4n_meta['models']['currentModelId'] = 'gemini-4-neon-medium'
    for opt in g4n_meta['configOptions']:
        if opt['id'] == 'model':
            opt['currentValue'] = 'gemini-4-neon'
        elif opt['id'] == 'thought_level':
            opt['currentValue'] = 'medium'
    catalog['selected']['gemini-4-neon'] = g4n_meta
    return catalog


class AgyAliasTests(unittest.TestCase):
    def test_complete_shorthand_expansion_table_and_default_ocs(self):
        self.assertEqual(len(ALL_SHORTHAND_CASES), 33)
        default_fields = prepare_start('agy')
        self.assertEqual(
            default_fields,
            {
                'agy_approval': 'skip-permissions',
                'execution_policy': 'native-skip',
                'model': 'gemini-pro-orchestrator',
                'agy_swarm': 'native',
                'agy_mode': 'swarm',
            },
        )
        self.assertNotIn('effort', default_fields)

        for shorthand, expected_model, expected_swarm, expected_mode, _ in ALL_SHORTHAND_CASES:
            with self.subTest(shorthand=shorthand):
                fields = prepare_start('agy', shorthand)
                self.assertEqual(fields['model'], expected_model)
                self.assertEqual(fields['agy_swarm'], expected_swarm)
                self.assertEqual(fields.get('agy_mode'), expected_mode)
                self.assertEqual(fields['agy_approval'], 'skip-permissions')
                self.assertEqual(fields['execution_policy'], 'native-skip')
                self.assertNotIn('effort', fields)

        for short_4, short_g4 in (('4c', 'g4c'), ('4a', 'g4a'), ('4h', 'g4h'), ('4n', 'g4n')):
            for suffix in ('', 's', 't'):
                with self.subTest(pair=(short_4 + suffix, short_g4 + suffix)):
                    self.assertEqual(
                        prepare_start('agy', short_4 + suffix),
                        prepare_start('agy', short_g4 + suffix),
                    )

    def test_effort_passthrough_matching_explicit_options_and_worker_model(self):
        effort_cases = (
            ('g4n', 'xhigh', 'gemini-4-neon', 'off', None),
            ('4ns', 'xhigh', 'gemini-4-neon', 'native', 'swarm'),
            ('g4nt', 'high', 'gemini-4-neon', 'native', 'teamwork'),
            ('4n', 'low', 'gemini-4-neon', 'off', None),
            ('g4cs', 'medium', 'gemini-4-carbon', 'native', 'swarm'),
            ('g4at', 'low', 'gemini-4-argon', 'native', 'teamwork'),
            ('g4hs', 'xhigh', 'gemini-4-hydrogen', 'native', 'swarm'),
        )
        for shorthand, effort, exp_model, exp_swarm, exp_mode in effort_cases:
            with self.subTest(shorthand=shorthand, effort=effort):
                fields = prepare_start('agy', shorthand, effort)
                self.assertEqual(fields['model'], exp_model)
                self.assertEqual(fields['effort'], effort)
                self.assertEqual(fields['agy_swarm'], exp_swarm)
                self.assertEqual(fields.get('agy_mode'), exp_mode)

        for bad_effort in ('', '   ', ' low', 'low '):
            with self.subTest(bad_effort=bad_effort), self.assertRaises(ValueError):
                prepare_start('agy', 'g4ns', bad_effort)

        for shorthand, _, expected_swarm, _, intent in ALL_SHORTHAND_CASES:
            expected_model = dict(ALIAS_BASE_MAP)[shorthand[:-1] if shorthand[-1:] in {'s', 't'} else shorthand]
            for explicit_swarm in (None, expected_swarm):
                with self.subTest(shorthand=shorthand, agy_mode=intent, agy_swarm=explicit_swarm):
                    fields = prepare_start('agy', shorthand, agy_mode=intent, agy_swarm=explicit_swarm)
                    self.assertEqual(
                        (fields['model'], fields['agy_swarm'], fields['agy_mode']),
                        (expected_model, expected_swarm, intent),
                    )

        for worker_shorthand in ('4ns', 'g4ns', '4nt', 'g4nt', 'ons', 'ont', 'g4cs', 'g4ht'):
            with self.subTest(worker_shorthand=worker_shorthand):
                fields = prepare_start('agy', worker_shorthand, agy_worker_model='gemini-4-hydrogen-medium')
                self.assertEqual(fields['agy_worker_model'], 'gemini-4-hydrogen-medium')

        for bare, _ in ALIAS_BASE_MAP:
            with self.subTest(bare=bare), self.assertRaisesRegex(ValueError, 'requires native'):
                prepare_start('agy', bare, agy_worker_model='gemini-4-hydrogen-medium')

    def test_mode_and_route_conflict_matrix_never_creates_or_launches(self):
        with tempfile.TemporaryDirectory() as root:
            launched = []
            c = Collaboration(Path(root) / 'tasks.db', launch=launched.append)
            client = c.connect('omp')['client_id']

            for shorthand, _, expected_swarm, _, intent in ALL_SHORTHAND_CASES:
                for bad_mode in ('single', 'swarm', 'teamwork', 'dynamic'):
                    if bad_mode == intent:
                        continue
                    with self.subTest(shorthand=shorthand, bad_mode=bad_mode):
                        with self.assertRaises(ModelSelectionError) as ctx:
                            c.start(client, agent='agy', cwd=root, prompt='never send', model=shorthand, agy_mode=bad_mode)
                        expected_text = 'Dynamic requires' if bad_mode == 'dynamic' else 'conflict'
                        self.assertIn(expected_text, str(ctx.exception))

                bad_swarms = ('native', 'typo') if expected_swarm == 'off' else ('off', 'typo')
                for bad_swarm in bad_swarms:
                    with self.subTest(shorthand=shorthand, bad_swarm=bad_swarm):
                        with self.assertRaises(ModelSelectionError):
                            c.start(client, agent='agy', cwd=root, prompt='never send', model=shorthand, agy_swarm=bad_swarm)

            explicit_conflicts = (
                dict(agy_mode='single', agy_swarm='native'),
                dict(agy_mode='swarm', agy_swarm='off'),
                dict(agy_mode='teamwork', agy_swarm='off'),
                dict(agy_mode='dynamic', agy_swarm='off'),
                dict(agy_mode='dynamic', model='gemini-4-neon', agy_worker_model='gemini-4-hydrogen'),
            )
            for kwargs in explicit_conflicts:
                with self.subTest(kwargs=kwargs), self.assertRaises(ModelSelectionError):
                    c.start(client, agent='agy', cwd=root, prompt='never send', **kwargs)

            self.assertEqual(c.list(client), [])
            self.assertEqual(launched, [])

    def test_malformed_shorthand_table_never_dispatches(self):
        malformed = (
            'ON', 'On', 'ONS', 'ONT',
            '4N', '4NS', '4NT',
            'G4N', 'G4NS', 'G4NT',
            'G4C', 'G4CS', 'G4CT',
            'G4A', 'G4AS', 'G4AT',
            'G4H', 'G4HS', 'G4HT',
            'onss', 'onst', 'onts',
            '4nss', '4nst', '4nts',
            'g4nss', 'g4nst', 'g4nts',
            'g4css', 'g4ast', 'g4hts',
            'on/oc', 'on+oa', '4n+g4n', 'g4n/4n', 'g4c,g4a', 'g4n_g4h', 'g4n:4n',
            '4n4c', 'g4ng4c',
            'on s', 'on t', 'g4n s', 'g4n t', '4n s',
            'on-low', 'ons-high', 'ont-medium', 'on-xhigh',
            '4n-low', '4ns-medium', '4nt-high', '4n-xhigh', '4ns-xhigh',
            'g4n-low', 'g4n-medium', 'g4n-high', 'g4n-xhigh', 'g4ns-xhigh', 'g4nt-XHIGH',
            '4h-xhigh', 'g4h-xhigh', 'g4hs-xhigh', 'g4c-medium', 'g4a-low',
        )
        with tempfile.TemporaryDirectory() as root:
            launched = []
            c = Collaboration(Path(root) / 'tasks.db', launch=launched.append)
            client = c.connect('omp')['client_id']

            for bad in malformed:
                with self.subTest(bad=bad):
                    with self.assertRaisesRegex(ModelSelectionError, 'Invalid AGY shorthand'):
                        c.start(client, agent='agy', cwd=root, prompt='never send', model=bad)

            for bad_dyn in ('ON', 'G4NS', 'g4n-xhigh', 'onss'):
                with self.subTest(bad_dyn=bad_dyn):
                    with self.assertRaisesRegex(ModelSelectionError, 'Invalid AGY shorthand'):
                        c.start(client, agent='agy', cwd=root, prompt='never send', model=bad_dyn, agy_mode='dynamic')

            for bad_literal in (' on', 'g4n ', ' 4n', '', 42, True):
                with self.subTest(bad_literal=bad_literal):
                    with self.assertRaises(ValueError):
                        c.start(client, agent='agy', cwd=root, prompt='never send', model=bad_literal)

            self.assertEqual(c.list(client), [])
            self.assertEqual(launched, [])

    def test_full_advertised_ids_opaque_literals_and_non_agy_unchanged(self):
        full_ids = (
            ('gemini-pro-orchestrator-neon', 'native'),
            ('gemini-4-neon', 'native'),
            ('gemini-4-neon-low', 'native'),
            ('gemini-4-neon-medium', 'native'),
            ('gemini-4-neon-high', 'native'),
            ('gemini-4-neon-xhigh', 'native'),
            ('gemini-pro-orchestrator', 'native'),
            ('gemini-pro-orchestrator-argon', 'off'),
            ('gemini-4-carbon', 'native'),
            ('gemini-4-carbon-low', 'native'),
            ('gemini-4-argon', 'off'),
            ('gemini-4-argon-medium', 'off'),
            ('gemini-4-hydrogen', 'native'),
            ('gemini-4-hydrogen-xhigh', 'native'),
        )
        for model_id, default_swarm in full_ids:
            with self.subTest(model_id=model_id):
                fields = prepare_start('agy', model_id)
                self.assertEqual((fields['model'], fields['agy_swarm']), (model_id, default_swarm))
                for mode, route in (('single', 'off'), ('swarm', 'native'), ('teamwork', 'native'), ('dynamic', 'native')):
                    explicit = prepare_start('agy', model_id, agy_mode=mode)
                    self.assertEqual((explicit['model'], explicit['agy_mode'], explicit['agy_swarm']), (model_id, mode, route))
                self.assertEqual(prepare_start('agy', model_id, agy_swarm='off')['agy_swarm'], 'off')
                self.assertEqual(prepare_start('agy', model_id, agy_swarm='native')['agy_swarm'], 'native')

        opaque_literals = (
            'on-special', 'onyx-exact', 'online-model', 'one-shot',
            'g4-special', 'g4nano-exact', '4nano-exact',
            'oc-special', 'ocelot-exact', 'octopus-exact',
        )
        for literal in opaque_literals:
            with self.subTest(literal=literal):
                fields = prepare_start('agy', literal)
                self.assertEqual((fields['model'], fields['agy_swarm']), (literal, 'native'))

        for agent in ('omp', 'codex', 'pi'):
            for model in ('on', 'ons', 'ont', '4n', 'g4n', 'g4ns', 'g4ct', 'g4n-xhigh', 'onss'):
                with self.subTest(agent=agent, model=model):
                    self.assertEqual(prepare_start(agent, model), {'model': model, 'execution_policy': 'native-skip'})

    def test_shared_prepare_start_parity_across_cli_hermes_and_omp(self):
        start_cases = (
            ('on', None, 'gemini-pro-orchestrator-neon', 'off', None),
            ('ons', None, 'gemini-pro-orchestrator-neon', 'native', 'swarm'),
            ('ont', None, 'gemini-pro-orchestrator-neon', 'native', 'teamwork'),
            ('4n', 'xhigh', 'gemini-4-neon', 'off', None),
            ('4ns', 'xhigh', 'gemini-4-neon', 'native', 'swarm'),
            ('4nt', None, 'gemini-4-neon', 'native', 'teamwork'),
            ('g4n', 'low', 'gemini-4-neon', 'off', None),
            ('g4ns', 'xhigh', 'gemini-4-neon', 'native', 'swarm'),
            ('g4nt', 'high', 'gemini-4-neon', 'native', 'teamwork'),
            ('g4c', None, 'gemini-4-carbon', 'off', None),
            ('g4cs', 'medium', 'gemini-4-carbon', 'native', 'swarm'),
            ('g4ct', None, 'gemini-4-carbon', 'native', 'teamwork'),
            ('g4a', None, 'gemini-4-argon', 'off', None),
            ('g4as', 'low', 'gemini-4-argon', 'native', 'swarm'),
            ('g4at', None, 'gemini-4-argon', 'native', 'teamwork'),
            ('g4h', None, 'gemini-4-hydrogen', 'off', None),
            ('g4hs', 'xhigh', 'gemini-4-hydrogen', 'native', 'swarm'),
            ('g4ht', None, 'gemini-4-hydrogen', 'native', 'teamwork'),
        )
        reject_cases = (
            dict(model='on', agy_mode='swarm'),
            dict(model='g4ns', agy_mode='dynamic'),
            dict(model='ont', agy_swarm='off'),
            dict(model='g4n-xhigh'),
        )

        # 1) CLI (collab.main)
        with tempfile.TemporaryDirectory() as root:
            launches = []
            c = Collaboration(Path(root) / 'tasks.db', launch=launches.append)
            client = c.connect('omp')['client_id']
            for model, effort, exp_model, exp_swarm, exp_mode in start_cases:
                with self.subTest(surface='cli', model=model, effort=effort):
                    argv = ['--db', str(c.store.path), '--client', client, 'start', '--agent', 'agy',
                            '--cwd', root, '--prompt', 'synthetic', '--model', model]
                    if effort is not None:
                        argv += ['--effort', effort]
                    out = StringIO()
                    with patch.object(collab, 'Collaboration', return_value=c), redirect_stdout(out):
                        rc = collab.main(argv)
                    self.assertEqual(rc, 0, out.getvalue())
                    task = json.loads(out.getvalue())
                    self.assertEqual((task['model'], task.get('effort'), task['agy_swarm'], task.get('agy_mode')),
                                     (exp_model, effort, exp_swarm, exp_mode))

            before_count = len(c.list(client))
            for bad in reject_cases:
                with self.subTest(surface='cli_reject', bad=bad):
                    argv = ['--db', str(c.store.path), '--client', client, 'start', '--agent', 'agy',
                            '--cwd', root, '--prompt', 'never send', '--model', bad['model']]
                    if 'agy_mode' in bad:
                        argv += ['--agy-mode', bad['agy_mode']]
                    if 'agy_swarm' in bad:
                        argv += ['--agy-swarm', bad['agy_swarm']]
                    out = StringIO()
                    with patch.object(collab, 'Collaboration', return_value=c), redirect_stdout(out):
                        rc = collab.main(argv)
                    self.assertEqual(rc, 1)
                    payload = json.loads(out.getvalue())
                    self.assertEqual(payload['error'], 'ModelSelectionError')
            self.assertEqual(len(c.list(client)), before_count)

        # 2) Hermes Coordinator
        mod = load_plugin()
        with tempfile.TemporaryDirectory() as root:
            env = {'HERMES_SESSION_ID': 's1', 'HERMES_SESSION_KEY': 'k1'}
            ctx = Ctx()
            coord = mod.Coordinator(ctx, Path(root), lambda k, d='': env.get(k, d))
            for model, effort, exp_model, exp_swarm, exp_mode in start_cases:
                with self.subTest(surface='hermes', model=model, effort=effort):
                    args = dict(action='start', agent='agy', cwd=root, prompt='synthetic', model=model)
                    if effort is not None:
                        args['effort'] = effort
                    task = coord.handle(args)['task']
                    self.assertEqual((task['model'], task.get('effort'), task['agy_swarm'], task.get('agy_mode')),
                                     (exp_model, effort, exp_swarm, exp_mode))
            before_count = len(coord.handle({'action': 'list'})['tasks'])
            for bad in reject_cases:
                with self.subTest(surface='hermes_reject', bad=bad):
                    with self.assertRaises(ValueError) as err_ctx:
                        coord.handle(dict(action='start', agent='agy', cwd=root, prompt='never send', **bad))
                    self.assertEqual(type(err_ctx.exception).__name__, 'ModelSelectionError')
            self.assertEqual(len(coord.handle({'action': 'list'})['tasks']), before_count)

        # 3) OMP HostBridge + real omp_bridge.py subprocess JSON stdin/stdout
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / 'omp.db'
            launches = []
            bridge = HostBridge(db_path, launch=launches.append)
            host = {'session_id': 'omp-s1', 'agent_id': 'main'}
            cap = bridge.bind(host)
            for model, effort, exp_model, exp_swarm, exp_mode in start_cases:
                with self.subTest(surface='omp', model=model, effort=effort):
                    args = dict(action='start', agent='agy', cwd=root, prompt='synthetic', model=model)
                    if effort is not None:
                        args['effort'] = effort
                    task = bridge.handle(host, cap, args)['task']
                    self.assertEqual((task['model'], task.get('effort'), task['agy_swarm'], task.get('agy_mode')),
                                     (exp_model, effort, exp_swarm, exp_mode))

            before_tasks = bridge.handle(host, cap, {'action': 'list'})['tasks']
            for bad in reject_cases:
                with self.subTest(surface='omp_reject_subproc', bad=bad):
                    req = {
                        'db': str(db_path),
                        'host': host,
                        'capability': cap,
                        'args': dict(action='start', agent='agy', cwd=root, prompt='never send', **bad),
                    }
                    proc = subprocess.run(
                        [sys.executable, str(Path(__file__).with_name('omp_bridge.py'))],
                        input=json.dumps(req),
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(proc.returncode, 1, proc.stderr)
                    err = json.loads(proc.stdout)
                    self.assertEqual(err['error'], 'ModelSelectionError')
            self.assertEqual(bridge.handle(host, cap, {'action': 'list'})['tasks'], before_tasks)

    def test_historical_legacy_tasks_not_reparsed_or_backfilled(self):
        legacy_records = (
            {},
            {'model': 'on', 'agy_swarm': 'native'},
            {'model': 'g4n', 'agy_swarm': 'native'},
            {'model': 'gemini-pro-orchestrator-neon', 'agy_swarm': 'off'},
            {'model': 'gemini-4-neon', 'agy_swarm': 'native', 'effort': 'xhigh'},
        )
        with tempfile.TemporaryDirectory() as root:
            c = Collaboration(Path(root) / 'tasks.db', launch=lambda task: None)
            client = c.connect('omp')['client_id']
            for fields in legacy_records:
                with self.subTest(fields=fields):
                    old = c.store.create(c.origin(client), 'local', 'agy', root, 'legacy', False)
                    c.store.update(old['id'], state='completed', **fields)
                    before = c.store.get(old['id'])
                    c.list(client)
                    c.status(client, old['id'])
                    self.assertEqual(c.store.get(old['id']), before)
                    continued = c.intervene(client, old['id'], 'continue')
                    for key in ('model', 'effort', 'agy_swarm', 'agy_mode'):
                        self.assertEqual(continued.get(key), before.get(key))

    def test_downstream_native_owner_select_validates_neon_and_g4_families(self):
        neon_cat = _neon_catalog()
        orig_cat = json.loads(
            (Path(__file__).parent / 'fixtures/agy_orchestrator_catalog.json').read_text()
        )

        def make_owner(fields):
            owner = object.__new__(NativeOwner)
            owner.session_id = 'owned'
            owner.task = dict(fields)
            return owner

        # 1) ons/ont resolve to gemini-pro-orchestrator-neon without effort, fail on effort or unadvertised catalog
        for shorthand in ('ons', 'ont'):
            fields = prepare_start('agy', shorthand)
            owner = make_owner(fields)
            current = ['gemini-4-carbon']
            def req(method, params, **kw):
                current[0] = params['value']
                return {}, []
            def load():
                return neon_cat['selected'][current[0]], []
            with patch.object(owner, 'request', side_effect=req), patch.object(owner, 'load', side_effect=load):
                res = owner.select(neon_cat['initial'])
            self.assertEqual((res['model'], res['effort']), ('gemini-pro-orchestrator-neon', None))
            self.assertTrue(res['native_selection']['readback_verified'])

            owner_err = make_owner(prepare_start('agy', shorthand, 'low'))
            with patch.object(owner_err, 'request') as r_mock, self.assertRaises(ProtocolError):
                owner_err.select(neon_cat['initial'])
            r_mock.assert_not_called()

            owner_unadv = make_owner(fields)
            with patch.object(owner_unadv, 'request') as r_mock, self.assertRaises(ProtocolError):
                owner_unadv.select(orig_cat['initial'])
            r_mock.assert_not_called()

        # 2) g4ns/4ns/g4nt/4nt without effort resolve using session's advertised current thought_level
        for shorthand in ('g4ns', '4ns', 'g4nt', '4nt'):
            with self.subTest(shorthand=shorthand, effort=None):
                fields = prepare_start('agy', shorthand)
                owner = make_owner(fields)
                current = ['gemini-4-carbon']
                def req(method, params, **kw):
                    if params.get('configId') == 'model':
                        current[0] = params['value']
                    return {}, []
                def load():
                    return neon_cat['selected'][current[0]], []
                with patch.object(owner, 'request', side_effect=req), patch.object(owner, 'load', side_effect=load):
                    res = owner.select(neon_cat['initial'])
                self.assertEqual((res['model'], res['effort']), ('gemini-4-neon-medium', 'medium'))
                self.assertTrue(res['native_selection']['readback_verified'])

        # 3) g4ns/4nt with xhigh starting from standalone orchestrator metadata loads family xhigh option and passes readback
        for shorthand in ('g4ns', '4nt'):
            with self.subTest(shorthand=shorthand, effort='xhigh'):
                fields = prepare_start('agy', shorthand, 'xhigh')
                owner = make_owner(fields)
                state = {'model': 'gemini-pro-orchestrator', 'effort': 'medium'}
                def req(method, params, **kw):
                    if params.get('configId') == 'model':
                        state['model'] = params['value']
                    elif params.get('configId') == 'thought_level':
                        state['effort'] = params['value']
                    return {}, []
                def load():
                    meta = json.loads(json.dumps(neon_cat['selected'][state['model']]))
                    meta['models']['currentModelId'] = state['model'] + '-' + state['effort']
                    for opt in meta['configOptions']:
                        if opt['id'] == 'thought_level':
                            opt['currentValue'] = state['effort']
                    return meta, []
                with patch.object(owner, 'request', side_effect=req), patch.object(owner, 'load', side_effect=load):
                    res = owner.select(neon_cat['selected']['gemini-pro-orchestrator'])
                self.assertEqual((res['model'], res['effort']), ('gemini-4-neon-xhigh', 'xhigh'))
                self.assertTrue(res['native_selection']['readback_verified'])

        # 4) Unadvertised effort or unadvertised gemini-4-neon family fails closed before prompt
        bad_effort_owner = make_owner(prepare_start('agy', 'g4ns', 'not-advertised'))
        with patch.object(bad_effort_owner, 'request') as r_mock, self.assertRaises(ProtocolError):
            bad_effort_owner.select(neon_cat['selected']['gemini-4-neon'])
        r_mock.assert_not_called()

        unadv_family_owner = make_owner(prepare_start('agy', 'g4ns'))
        with patch.object(unadv_family_owner, 'request') as r_mock, self.assertRaises(ProtocolError):
            unadv_family_owner.select(orig_cat['initial'])
        r_mock.assert_not_called()

        # 5) g4cs/g4as/g4hs match 4cs/4as/4hs against advertised family/effort catalog
        for g4_short, effort, expected_exact in (
            ('g4cs', 'low', 'gemini-4-carbon-low'),
            ('g4as', 'medium', 'gemini-4-argon-medium'),
            ('g4hs', 'low', 'gemini-4-hydrogen-low'),
        ):
            with self.subTest(g4_short=g4_short, effort=effort):
                fields = prepare_start('agy', g4_short, effort)
                owner = make_owner(fields)
                family = fields['model']
                state = {'model': 'gemini-4-carbon', 'effort': 'medium'}
                def req(method, params, **kw):
                    if params.get('configId') == 'model':
                        state['model'] = params['value']
                    elif params.get('configId') == 'thought_level':
                        state['effort'] = params['value']
                    return {}, []
                def load():
                    meta = json.loads(json.dumps(neon_cat['initial']))
                    meta['models']['currentModelId'] = state['model'] + '-' + state['effort']
                    for opt in meta['configOptions']:
                        if opt['id'] == 'model':
                            opt['currentValue'] = state['model']
                        elif opt['id'] == 'thought_level':
                            opt['currentValue'] = state['effort']
                    return meta, []
                with patch.object(owner, 'request', side_effect=req), patch.object(owner, 'load', side_effect=load):
                    res = owner.select(neon_cat['initial'])
                self.assertEqual((res['model'], res['effort']), (expected_exact, effort))
                self.assertEqual(state['model'], family)
                self.assertTrue(res['native_selection']['readback_verified'])


if __name__ == '__main__':
    unittest.main()
