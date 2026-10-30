import json
import sqlite3
import unittest
import makemoney as app
import tenant_runtime
import workflow_store
import test_profile_access as existing


class TenantRuntimeTests(unittest.TestCase):
    setUp=existing.ProfileAccessTests.setUp
    acting=existing.ProfileAccessTests.acting
    trade=existing.ProfileAccessTests.trade
    row=existing.ProfileAccessTests.row
    update=existing.ProfileAccessTests.update

    def test_reference_and_trade_ownership_are_idempotent_without_rewriting_positions(self):
        tid=self.trade();self.update(tid,status='OPEN',broker_position_id='original',policy_json='{"stop":95}')
        before=self.row(tid)
        with self.acting('owner'):owner=app.access.trade_broker_owner(tid)
        app.ensure_db();app.ensure_db()
        self.assertEqual(self.row(tid),before)
        with self.acting('owner'):
            self.assertEqual(app.access.trade_broker_owner(tid),owner)
            self.assertEqual(app.access.execution_readiness(1)['admin_reference']['provider'],'etoro')

    def test_destination_plan_never_changes_admin_reference_or_existing_or_new_trade_owner(self):
        tid=self.trade()
        with self.acting('owner'):
            pid=app.access.create_draft('IBKR plan','ibkr')
            target=next(p['broker_account_id'] for p in app.access.list_profiles() if p['id']==pid)
            app.access.plan_entry_destination(1,target)
            report=app.access.execution_readiness(1)
            self.assertEqual(report['admin_reference']['broker_account_id'],1)
            self.assertEqual(report['active_destination']['broker_account_id'],1)
            self.assertEqual(report['planned_destination']['broker_account_id'],target)
            self.assertFalse(report['additional_runtime_ready'])
            from brokers.contracts import validate_runtime_binding
            conn=app.get_connection()
            validate_runtime_binding(conn,'etoro')
            with self.assertRaises(ValueError):validate_runtime_binding(conn,'ibkr')
            conn.close()
            self.assertEqual(app.access.trade_broker_owner(tid)['broker_account_id'],1)
        new=self.trade()
        with self.acting('owner'):self.assertEqual(app.access.trade_broker_owner(new)['broker_account_id'],1)
        with self.acting('owner',pid):
            with self.assertRaises(PermissionError):app.access.require_profile('operator')

    def test_owner_cannot_route_to_another_users_connection(self):
        with self.acting('unassigned'):pid=app.access.create_draft('Private other','ibkr')
        with self.acting('admin'):target=next(p['broker_account_id'] for p in app.access.list_profiles() if p['id']==pid)
        with self.acting('owner'):
            with self.assertRaises(PermissionError):app.access.plan_entry_destination(1,target)
            with self.assertRaises(PermissionError):app.access.execution_readiness(pid)
        for role in ('viewer','operator'):
            with self.acting(role):
                with self.assertRaises(PermissionError):app.access.plan_entry_destination(1,1)

    def test_database_prevents_rebinding_reference_trade_and_recovery(self):
        tid=self.trade();c=app.get_connection()
        c.execute("INSERT INTO broker_actions(trade_id,kind,state,created_at,updated_at,next_check_at,payload) VALUES (?,'entry','RESOLVED',0,0,0,'{}')",(tid,));c.commit()
        for query in ("UPDATE trade_broker_ownership SET broker_account_id=2", "DELETE FROM trade_broker_ownership",
                      "UPDATE platform_broker_roles SET broker_account_id=2", "DELETE FROM platform_broker_roles",
                      "UPDATE broker_action_ownership SET broker_account_id=2", "DELETE FROM broker_action_ownership",
                      "UPDATE broker_accounts SET account_ref='replacement' WHERE id=1",
                      "UPDATE broker_connections SET provider='ibkr' WHERE id=1") :
            with self.assertRaises(sqlite3.IntegrityError):c.execute(query)
            c.rollback()
        self.assertEqual(c.execute('SELECT broker_account_id FROM broker_action_ownership').fetchone()[0],1)
        c.close()

    def test_analysis_archive_restore_preserves_original_broker_binding(self):
        tid=self.trade();self.update(tid,created_at='2020-01-01 00:00:00')
        c=app.get_connection();before=tenant_runtime.trade_owner(c,tid)
        self.assertIn(tid,workflow_store.archive_analysis(c,3))
        self.assertEqual(tenant_runtime.trade_owner(c,tid),before)
        workflow_store.restore_analysis(c,tid)
        self.assertEqual(tenant_runtime.trade_owner(c,tid),before);c.close()

    def test_draft_and_reference_metadata_contains_no_secret_values(self):
        with self.acting('owner'):
            pid=app.access.create_draft('Private planned broker','ibkr')
            report=app.access.execution_readiness(pid)
            self.assertFalse(report['additional_runtime_ready'])
            self.assertNotIn('credentials_ref',json.dumps(report))
            self.assertNotIn('api_key',json.dumps(report))

    def test_direct_trade_ownership_lookup_respects_private_profile_membership(self):
        with self.acting('unassigned'):pid=app.access.create_draft('Private owner','ibkr')
        c=app.get_connection()
        tid=c.execute("INSERT INTO trades(symbol,side,leverage,exit_target,status,trading_profile_id) VALUES ('PRIVATE','LONG',2,1,'ANALYSIS_ONLY',?)",(pid,)).lastrowid
        c.commit();c.close()
        with self.acting('owner'):
            with self.assertRaises(PermissionError):app.access.trade_broker_owner(tid)
        with self.acting('unassigned',pid):
            self.assertEqual(app.access.trade_broker_owner(tid)['profile_id'],pid)

    def test_owner_ui_exposes_a_plan_without_activation_controls(self):
        ui=existing.ProfileAccessTests.ui(self,'owner')
        ui.run();ui.button(key='open_profile_from_header').click().run()
        self.assertFalse(ui.exception,[e.message for e in ui.exception])
        self.assertTrue(any(b.label=='Save routing plan' for b in ui.button))
        self.assertFalse(any(b.label=='Activate runtime' for b in ui.button))

    def test_destination_changes_cannot_overwrite_admin_reference_configuration(self):
        with self.acting('admin'):
            app.access.validate_reference_configuration({'FINBOT_BROKER_PROVIDER':'etoro'})
            with self.assertRaises(ValueError):app.access.validate_reference_configuration({'FINBOT_BROKER_PROVIDER':'ibkr'})
            with self.assertRaises(ValueError):app.access.validate_reference_configuration({'ETORO_DEMO_TRADING':'false'})
        with self.acting('owner'):
            with self.assertRaises(PermissionError):app.access.validate_reference_configuration({'FINBOT_BROKER_PROVIDER':'etoro'})
