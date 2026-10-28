"""Explicit migration retains complete source bytes and rejects unresolved owners."""
from copy import deepcopy
import unittest
from scripts.migrate_body_control_gateway import migrate
from tests.brain.test_body_control import Fixture


class GatewayMigrationTests(Fixture,unittest.TestCase):
    def legacy(self):
        self.authority.start();self.authority.close()
        store=self.authority.store;state=store.read()
        state.update(schema=1,binding=dict(robot_serial='test-robot',action_endpoint='tcp://old:5556',
                                         dream='http://nav.invalid',vla='http://vla.invalid',relay='http://old-relay.invalid'))
        store.write(state)
        return store
    def test_preview_does_not_write_and_apply_keeps_original_bytes(self):
        store=self.legacy();raw=store.path.read_bytes()
        plan=migrate(self.root,self.authority.binding)
        self.assertFalse(plan['applied']);self.assertEqual(raw,store.path.read_bytes())
        result=migrate(self.root,self.authority.binding,apply=True)
        from pathlib import Path
        self.assertEqual(Path(result['backup']).read_bytes(),raw)
        self.assertEqual(store.read()['schema'],2)
        self.assertEqual(store.read()['binding'],self.authority.binding)
        self.assertIsNone(store.read()['authority_id'])
    def test_unknown_owner_cannot_be_migrated_or_erased(self):
        store=self.legacy();state=store.read();state['current']='unknown'
        state['leases']['unknown']=dict(state='recovery_required');store.write(state)
        raw=store.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'Unresolved'):migrate(self.root,self.authority.binding,apply=True)
        self.assertEqual(store.path.read_bytes(),raw)
    def test_live_writer_blocks_migration(self):
        self.authority.start()
        with self.assertRaises(BlockingIOError):migrate(self.root,self.authority.binding,apply=True)

    def test_preserve_unknown_migration_never_releases_or_replays_old_control(self):
        store=self.legacy();state=store.read()
        state['leases']['legacy']=dict(lease_id='legacy',task_id='old-task',command_id='old-command',
            state='recovery_required',target='navigation',authority_id='old-authority',ownership_epoch=1,evidence={})
        state['current']='legacy';store.write(state)
        old=deepcopy(state)
        result=migrate(self.root,self.authority.binding,apply=True,preserve_unresolved=True)
        updated=store.read()
        self.assertTrue(result['unresolved_preserved']);self.assertEqual(updated['current'],'legacy')
        self.assertTrue(updated['leases']['legacy']['legacy_control'])
        self.assertEqual(updated['leases']['legacy']['state'],'recovery_required')
        self.assertEqual(updated['requests'],old['requests'])
        from brain.kernel.body_control import BodyControl
        current=BodyControl(self.config,self.root,self.driver,self.pool);self.addCleanup(current.close)
        with self.assertRaises(Exception):current.start()
        self.assertEqual(store.read()['current'],'legacy')
        with self.assertRaises(Exception):current.assert_new_task_allowed()
        self.assertFalse(self.wire.calls)
