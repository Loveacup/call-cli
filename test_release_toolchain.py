"""Exact-record backend child discovery; no DB, model or native sessions."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from release import BackendFailure, NativeBackend


class ReleaseToolchainTests(unittest.TestCase):
    def test_host_setting_reaches_start_continuation_and_native_release_actions(self):
        import importlib
        import shlex
        import sys
        from test_plugin import Ctx, load_plugin
        from fixtures.close_backend import CloseBackend
        mod = load_plugin()
        release = importlib.import_module('callcli_plugin.release')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            selected, bundled = root / 'selected chain', root / 'bundled'
            selected.mkdir()
            bundled.mkdir()
            for directory in (selected, bundled):
                for name in ('node', 'npm', 'acpx'):
                    executable = directory / name
                    executable.write_text('#!/bin/sh\nexit 0\n')
                    executable.chmod(0o700)
            ctx = Ctx()
            ctx.get_config = lambda key, default=None: str(selected) if key == 'local_toolchain_bin' else True if key == 'enabled' else default
            origin_env = {'HERMES_SESSION_ID': 'fixture', 'HERMES_SESSION_KEY': 'fixture'}
            coordinator = mod.Coordinator(ctx, root, lambda key, default='': origin_env.get(key, default))
            store = mod.Store(root / 'call-cli/tasks.sqlite3')
            parent_path = str(bundled) + os.pathsep + os.environ.get('PATH', '')
            with patch.dict(os.environ, {'PATH': parent_path}):
                task = coordinator.handle(dict(action='start', agent='omp', cwd=td, prompt='synthetic'))['task']
                store.update(task['id'], state='completed')
                coordinator.handle(dict(action='reply', task_id=task['id'], expected_turn=1, command_id='continue', prompt='synthetic'))
                # Execute the dispatched environment prefix, replacing business runner with discovery only.
                for _, args in ctx.calls:
                    argv = shlex.split(args['command'])
                    offset = argv.index(sys.executable)
                    result = subprocess.run(argv[:offset] + [sys.executable, '-c',
                        "import json,shutil; print(json.dumps([shutil.which(n) for n in ('node','npm','acpx')]))"],
                        capture_output=True, text=True, check=True)
                    self.assertEqual(json.loads(result.stdout), [str(selected / n) for n in ('node', 'npm', 'acpx')])
                real_backend = release.NativeBackend
                for action in ('close', 'reconcile', 'release_only'):
                    task = coordinator.handle(dict(action='start', agent='omp', cwd=td, prompt='synthetic'))['task']
                    store.update(task['id'], state='unknown', runner_pid=0, acpx_record_id='fixture-record')
                    backend = CloseBackend()
                    adapter = real_backend(toolchain_bin=str(selected))
                    backend.resolve = adapter.resolve
                    response = subprocess.CompletedProcess([], 0, b'{"acp":"fixture-acp"}', b'')
                    with patch.object(release, 'NativeBackend', return_value=backend) as factory, \
                         patch('subprocess.run', return_value=response) as run, \
                         patch('callcli_plugin.updates.device_lock', return_value=nullcontext()):
                        result = coordinator.handle(dict(action=action, task_id=task['id'], expected_turn=1))
                    self.assertEqual(result['task']['state'], 'closed')
                    factory.assert_called_once_with(toolchain_bin=str(selected))
                    self.assertEqual(run.call_args.args[0][0], str(selected / 'node'))
                    self.assertEqual(run.call_args.kwargs['env']['PATH'].split(os.pathsep)[0], str(selected))
                self.assertEqual(os.environ['PATH'], parent_path)
            # A local-only setting does not rewrite the existing SSH launch.
            remote = store.create(mod.capture_origin(coordinator.get_env, 'default'), 'worker', 'omp', '/fixture', 'synthetic', False)
            coordinator.launch(store, remote)
            self.assertEqual(shlex.split(ctx.called[1]['command'])[0], sys.executable)

    def test_explicit_host_toolchain_overrides_bundled_first_without_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            selected, bundled = root / 'selected', root / 'bundled'
            selected.mkdir()
            bundled.mkdir()
            for directory in (selected, bundled):
                for name in ('node', 'npm', 'acpx'):
                    executable = directory / name
                    executable.write_text('#!/bin/sh\nexit 0\n')
                    executable.chmod(0o700)
            parent_path = os.pathsep.join(map(str, (bundled, selected)))
            result = subprocess.CompletedProcess([], 0, b'{"acp":"fixture-acp"}', b'')
            with patch.dict(os.environ, {'PATH': parent_path}), \
                 patch('updates.device_lock', return_value=nullcontext()), \
                 patch('subprocess.run', return_value=result) as run:
                self.assertEqual(NativeBackend(toolchain_bin=str(selected)).resolve({}), {'acp': 'fixture-acp'})
                self.assertEqual(run.call_args.args[0][0], str(selected / 'node'))
                import shutil
                self.assertEqual(shutil.which('npm', path=run.call_args.kwargs['env']['PATH']), str(selected / 'npm'))
                self.assertEqual(os.environ['PATH'], parent_path)
                (selected / 'npm').unlink()
                run.reset_mock()
                with self.assertRaises(BackendFailure) as caught:
                    NativeBackend(toolchain_bin=str(selected)).resolve({})
                self.assertEqual(caught.exception.reason_code, 'npm_executable_not_found')
                run.assert_not_called()

    def test_configured_path_selects_node_and_matching_npm_without_changing_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            toolchain, other = root / 'selected', root / 'other'
            toolchain.mkdir()
            other.mkdir()
            for executable in (toolchain / 'node', toolchain / 'npm', other / 'npm'):
                executable.write_text('#!/bin/sh\nexit 0\n')
                executable.chmod(0o700)
            parent_path = os.pathsep.join(map(str, (other, toolchain)))
            result = subprocess.CompletedProcess([], 0, b'{"acp":"fixture-acp"}', b'')
            with patch.dict(os.environ, {'PATH': parent_path}), \
                 patch('updates.device_lock', return_value=nullcontext()), \
                 patch('subprocess.run', return_value=result) as run:
                self.assertEqual(NativeBackend().resolve({}), {'acp': 'fixture-acp'})
                argv = run.call_args.args[0]
                child_env = run.call_args.kwargs['env']
                self.assertEqual(argv[0], str(toolchain / 'node'))
                self.assertEqual(argv[-1], 'resolve')
                import shutil
                self.assertEqual(shutil.which('npm', path=child_env['PATH']), str(toolchain / 'npm'))
                self.assertEqual(os.environ['PATH'], parent_path)

    def test_missing_node_or_npm_rejects_before_backend_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            node = Path(td) / 'node'
            with patch.dict(os.environ, {'PATH': td}), patch('subprocess.run') as run:
                for expected in ('node_executable_not_found', 'npm_executable_not_found'):
                    with self.subTest(reason=expected), self.assertRaises(BackendFailure) as caught:
                        NativeBackend().resolve({})
                    self.assertEqual((caught.exception.stage, caught.exception.reason_code), ('resolve', expected))
                    node.write_text('#!/bin/sh\nexit 0\n')
                    node.chmod(0o700)
                run.assert_not_called()

    def test_loader_failures_keep_safe_reason_without_raw_error_output(self):
        reasons = ('acpx_version_mismatch', 'shared_runtime_unavailable', 'acpx_package_not_found',
                   'runtime_export_unavailable', 'npm_executable_not_found', 'npm_root_timeout',
                   'PRIVATE_SENTINEL')
        with patch('shutil.which', side_effect=lambda name, **kw: '/fixture/bin/' + name), \
             patch('updates.device_lock', return_value=nullcontext()):
            for reason in reasons:
                result = subprocess.CompletedProcess([], 1, json.dumps({'stage': 'load', 'reason_code': reason}).encode(), b'PRIVATE_SENTINEL')
                with self.subTest(reason=reason), patch('subprocess.run', return_value=result), \
                     self.assertRaises(BackendFailure) as caught:
                    NativeBackend().resolve({})
                self.assertEqual(caught.exception.reason_code, reason if reason != 'PRIVATE_SENTINEL' else 'backend_unavailable')
                self.assertNotIn('PRIVATE_SENTINEL', str(caught.exception.diagnostic))


if __name__ == '__main__':
    unittest.main()
