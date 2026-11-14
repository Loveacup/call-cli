"""Recovery advice follows the public surface's actual exact-close capability."""
import tempfile
import unittest
from pathlib import Path
from collab import Collaboration
from recovery import recovery_card
from fixtures.close_backend import CloseBackend
from unittest.mock import patch


class RecoveryCardTests(unittest.TestCase):
    def test_native_unknown_card_offers_receipt_only_recovery_without_second_owner(self):
        task=dict(id='native',device='local',state='unknown',agy_swarm='native',turn=1,session='logical',
            origin={},write=True,cwd='/work',native_cleanup=dict(kind='verified-cancellation',session_unloaded=True))
        card=recovery_card(task,host='hermes-native')
        self.assertIn('receipt',card['next_action'])
        self.assertIn('reconcile',card['next_action'])
        self.assertIn('no new owner',card['next_action'])
        self.assertFalse(card['capabilities']['exact_record_reconcile'])

    def test_unknown_cards_distinguish_missing_remote_and_available_exact_record(self):
        with tempfile.TemporaryDirectory() as td:
            for mode in ('missing','remote','exact'):
                with self.subTest(mode=mode):
                    c=Collaboration(Path(td)/(mode+'.db'),launch=lambda task:None)
                    client=c.connect('pi')['client_id']
                    task=c.start(client,cwd=td,prompt='synthetic')
                    fields=dict(state='unknown')
                    if mode!='missing': fields['acpx_record_id']='synthetic-record'
                    if mode=='remote': fields['device']='remote-device'
                    c.store.update(task['id'],**fields)
                    card=c.status(client,task['id'])['recovery']
                    self.assertEqual(card['capabilities']['exact_record_reconcile'],mode=='exact')
                    if mode=='exact':
                        self.assertIn('close',card['next_action'])
                        backend=CloseBackend()
                        with patch('release.NativeBackend',return_value=backend):
                            self.assertEqual(c.close(client,task['id'])['state'],'closed')
                        self.assertEqual(backend.closes,0)
                    else:
                        self.assertIn('retain HOLD',card['next_action'])
                        self.assertNotIn('reconcile exact record',card['next_action'])
                        with patch('release.NativeBackend') as backend:
                            self.assertEqual(c.close(client,task['id'])['state'],'unknown')
                            backend.assert_not_called()
            native=dict(c.store.get(task['id']),initiating_surface='hermes-native',device='local',state='unknown')
            self.assertIn('reconcile',recovery_card(native,host='hermes-native')['next_action'])
            native['state']='closed'
            self.assertFalse(recovery_card(native)['capabilities']['exact_record_reconcile'])
            native.update(state='unknown',initiating_surface='unverified-surface')
            self.assertTrue(recovery_card(native)['capabilities']['exact_record_reconcile'])
            self.assertIn('available entry point',recovery_card(native)['next_action'])

    def test_legacy_and_cross_entry_cards_use_task_capability_and_current_caller(self):
        from omp_bridge import HostBridge
        with tempfile.TemporaryDirectory() as td:
            for adapter in ('cli','omp'):
                with self.subTest(adapter=adapter):
                    if adapter=='cli':
                        c=Collaboration(Path(td)/'cli.db',launch=lambda task:None)
                        client=c.connect('pi')['client_id']
                        status=lambda tid:c.status(client,tid)
                        task=c.start(client,cwd=td,prompt='synthetic')
                    else:
                        c=HostBridge(Path(td)/'omp.db',launch=lambda task:None)
                        host=dict(session_id='synthetic-host',agent_id='synthetic-agent')
                        client=c.bind(host)
                        status=lambda tid:c.handle(host,client,dict(action='status',task_id=tid))['task']
                        task=c.handle(host,client,dict(action='start',cwd=td,prompt='synthetic'))['task']
                    for surface in (None,'hermes-native','unverified-surface'):
                        with self.subTest(creating_surface=surface):
                            with c.store.connect() as db:
                                row=c.store.get(task['id'])
                                row.update(state='unknown',acpx_record_id='synthetic-record')
                                if surface is None: row.pop('initiating_surface',None)
                                else: row['initiating_surface']=surface
                                c.store._save(db,row)
                            card=status(task['id'])['recovery']
                            self.assertTrue(card['capabilities']['exact_record_reconcile'])
                            self.assertIn('close (probe-only)',card['next_action'])
                            self.assertNotIn('reconcile exact record',card['next_action'])
                            self.assertIn('reconcile exact record',recovery_card(row,host='hermes-native')['next_action'])
                            for unknown in (None,'unverified-entry'):
                                neutral=recovery_card(row,host=unknown)
                                self.assertTrue(neutral['capabilities']['exact_record_reconcile'])
                                self.assertIn('available entry point',neutral['next_action'])
                                self.assertNotIn('close (probe-only)',neutral['next_action'])
                                self.assertNotIn('reconcile exact record',neutral['next_action'])
                            self.assertEqual(c.store.get(task['id']),row)
                    backend=CloseBackend()
                    with patch('release.NativeBackend',return_value=backend):
                        self.assertEqual(c.close(client,task['id'])['state'],'closed')
                    self.assertEqual(backend.closes,0)


if __name__=='__main__': unittest.main()
