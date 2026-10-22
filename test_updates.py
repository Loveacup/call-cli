import unittest
from updates import update_decision


class UpdateTests(unittest.TestCase):
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
