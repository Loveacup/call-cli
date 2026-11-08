import unittest
from updates import update_decision


class UpdateTests(unittest.TestCase):
    def test_check_does_not_upgrade_without_separate_authorization(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from types import SimpleNamespace
        from bridge import Store, Origin
        from updates import ensure_current
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root)/'tasks.db')
            task = store.create(Origin('test','s','k'), 'local', 'omp', root, 'test', False)
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                if args == ['acpx','--version']: return SimpleNamespace(returncode=0, stdout=b'0.22.0')
                if args == ['npm','view','acpx','dist-tags.latest','--json']: return SimpleNamespace(returncode=0, stdout=b'"0.22.1"')
                raise AssertionError('Check must not perform maintenance')
            with patch('updates.DEFAULT_LOCK_ROOT', Path(root)/'locks'), patch('updates.subprocess.run', side_effect=run):
                result = ensure_current(store, task)
            self.assertEqual(result['decision'], 'update_available')
            self.assertEqual(len(calls), 2)

    def test_authorized_maintenance_never_installs_while_another_task_is_active(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from types import SimpleNamespace
        from bridge import Store, Origin
        from updates import ensure_current
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root)/'tasks.db'); origin = Origin('test','s','k')
            active = store.create(origin, 'local', 'omp', root, 'active', False)
            store.update(active['id'], state='running')
            task = store.create(origin, 'local', 'omp', root, 'check', False)
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                if args == ['acpx','--version']: out = '0.22.0'
                elif args[:2] == ['npm','view']: out = '"0.22.1"'
                elif args == ['npm','root','-g']: out = '/fixture'
                elif args[:2] == ['node','-e']: out = '{"name":"acpx","repository":"https://github.com/openclaw/acpx"}'
                else: raise AssertionError('Must not install while busy')
                return SimpleNamespace(returncode=0, stdout=out.encode())
            with patch('updates.DEFAULT_LOCK_ROOT', Path(root)/'locks'), patch('updates.subprocess.run', side_effect=run):
                self.assertEqual(ensure_current(store, task, allow_upgrade=True)['decision'], 'deferred_busy')
            self.assertFalse(any('install' in call for call in calls))

    def test_registry_scalar_and_singleton_array(self):
        from updates import registry_version
        self.assertEqual(registry_version('"0.20.0"'),'0.20.0')
        self.assertEqual(registry_version('["0.20.0"]'),'0.20.0')
        with self.assertRaises(ValueError):registry_version('null')
        with self.assertRaises(ValueError):registry_version('["0.20.0","0.20.1"]')

    def test_shared_execution_lock_blocks_upgrade_across_stores(self):
        from updates import device_lock
        import tempfile,pathlib
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()/'.hermes/cache/scratch') as d:
            root=pathlib.Path(d)
            a={'device':'alias-a','device_key':'same-install'}
            b={'device':'alias-b','device_key':'same-install'}
            with device_lock(a,root=root) as acquired:
                self.assertTrue(acquired)
                with device_lock(b,exclusive=True,wait=False,root=root) as upgrade:
                    self.assertFalse(upgrade)
            with device_lock(b,exclusive=True,wait=False,root=root) as upgrade:
                self.assertTrue(upgrade)

    def test_update_only_compatible_official_idle_install(self):
        self.assertEqual(update_decision('0.20.0','0.20.0',True,False),'current')
        self.assertEqual(update_decision('0.20.0','0.20.1',True,False),'upgrade')
        self.assertEqual(update_decision('0.20.0','0.20.1',True,True),'deferred_busy')
        self.assertEqual(update_decision('0.20.0','0.21.0',True,False),'review_version_change')
        self.assertEqual(update_decision('0.20.0','0.20.1',False,False),'review_install_source')
        self.assertEqual(update_decision('0.21.0','0.20.0',True,False),'ahead_no_downgrade')
        self.assertEqual(update_decision('custom','0.20.1',True,False),'review_version_change')


if __name__ == '__main__':unittest.main()
