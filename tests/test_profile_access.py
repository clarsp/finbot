"""Permission boundaries, shared-profile onboarding and account suspension."""
import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import makemoney as app
import test_two_direction_workflow as fixtures

PASSWORD='test-long-password-123'

class ProfileAccessTests(unittest.TestCase):
    trade=fixtures.DatabaseTests.trade
    row=fixtures.DatabaseTests.row
    update=fixtures.DatabaseTests.update
    def setUp(self):
        fixtures.DatabaseTests.setUp(self)
        app.add_user('viewer',PASSWORD)
        app.add_user('operator',PASSWORD,profile_role='operator')
        app.add_user('owner',PASSWORD,profile_role='owner')
        app.add_user('unassigned',PASSWORD,profile_role=None)
        self.tokens={name:app.create_session_token(name) for name in ('admin','viewer','operator','owner','unassigned')}
    def acting(self,name,pid=1):return app.access.web_request(self.tokens[name],pid)

    def test_default_profile_links_account_and_connection_and_migration_is_idempotent(self):
        tid=self.trade();self.update(tid,broker_position_id='p',status='EXIT_PENDING',take_profit_price=106,policy_json='{"holding_hours":24}')
        before=self.row(tid);app.ensure_db();app.ensure_db()
        self.assertEqual(self.row(tid),before)
        c=app.get_connection()
        self.assertEqual(c.execute('SELECT count(*) FROM trading_profiles').fetchone()[0],1)
        self.assertEqual(c.execute('SELECT count(*) FROM broker_accounts').fetchone()[0],1)
        self.assertEqual(c.execute('SELECT count(*) FROM broker_connections').fetchone()[0],1)
        self.assertEqual(c.execute('SELECT trading_profile_id FROM trades WHERE id=?',(tid,)).fetchone()[0],1);c.close()

    def test_viewer_can_read_shared_trades_but_not_mutate(self):
        tid=self.trade()
        with self.acting('viewer'):
            self.assertEqual(app.get_trade_history()[0]['id'],tid)
            for action in [lambda:app.update_agent_rules('execution',{'enabled':False}),
                           lambda:app.set_starting_capital(1),lambda:app.add_user('evil',PASSWORD,True),
                           lambda:app.set_user_admin('viewer',True),lambda:app.set_user_suspended('admin',True),
                           lambda:app.cleanup_stale_analysis(),lambda:app.PositionManagerAgent().recovery_manager(),
                           lambda:app.PositionManagerAgent().audit_configuration(),lambda:app.PerformanceAnalystAgent().run_due()]:
                with self.assertRaises(PermissionError):action()
            with self.assertRaises(PermissionError):asyncio.run(app.place_analysis_candidate_demo_order(tid))
        self.submit.assert_not_awaited();self.adapter.get_positions.assert_not_called()

    def test_operator_can_manage_profile_rules_but_not_users_or_membership(self):
        with self.acting('operator'):
            app.update_agent_rules('execution',{'max_order_units':3})
            with self.assertRaises(PermissionError):app.list_users()
            with self.assertRaises(PermissionError):app.access.set_membership(1,'viewer','operator')
            with self.assertRaises(PermissionError):app.set_user_admin('operator',True)

    def test_owner_can_share_profile_without_granting_platform_admin(self):
        with self.acting('owner'):app.access.set_membership(1,'unassigned','viewer')
        with self.acting('unassigned'):
            self.assertEqual(app.access.require_profile()['effective_role'],'viewer')
            self.assertFalse(app.get_user_profile('unassigned')['is_admin'])
        with self.acting('owner'):
            with self.assertRaises(PermissionError):app.add_user('cannot-register',PASSWORD)

    def test_unassigned_account_cannot_read_shared_data(self):
        self.trade()
        with self.acting('unassigned'):
            self.assertEqual(app.access.list_profiles(),[])
            with self.assertRaises(PermissionError):app.get_trade_history()
            with self.assertRaises(PermissionError):app.get_dashboard_summary()

    def test_draft_profile_model_and_runtime_boundary(self):
        with self.acting('viewer'):
            pid=app.access.create_draft('My future flow','binance')
            app.access.set_membership(pid,'operator','viewer')
            self.assertEqual(app.access.require_profile('owner',pid,runtime=False)['status'],'draft')
        with self.acting('operator',pid):
            self.assertEqual(app.access.require_profile('viewer',runtime=False)['id'],pid)
            with self.assertRaises(PermissionError):app.get_trade_history()
            with self.assertRaises(PermissionError):app.set_starting_capital(2)
        with self.acting('unassigned',pid):
            with self.assertRaises(PermissionError):app.access.require_profile('viewer',runtime=False)
        c=app.get_connection();self.assertEqual(c.execute('SELECT count(*) FROM broker_connections').fetchone()[0],2);c.close()

    def test_revoke_membership_takes_effect_on_existing_session(self):
        with self.acting('owner'):app.access.set_membership(1,'viewer',None)
        with self.acting('viewer'):
            with self.assertRaises(PermissionError):app.get_trade_history()

    def test_suspension_revokes_existing_session_and_blocks_new_login(self):
        token=self.tokens['viewer']
        with self.acting('admin'):app.set_user_suspended('viewer',True)
        self.assertIsNone(app.validate_session_token(token))
        self.assertFalse(app.authenticate_user('viewer',PASSWORD))
        with self.assertRaises(PermissionError):app.create_session_token('viewer')
        with self.acting('admin'):app.set_user_suspended('viewer',False)
        self.assertTrue(app.authenticate_user('viewer',PASSWORD))
        self.assertIsNone(app.validate_session_token(token))

    def test_admin_cannot_suspend_self_or_last_active_admin(self):
        with self.acting('admin'):
            with self.assertRaises(ValueError):app.set_user_suspended('admin',True)
            with self.assertRaises(ValueError):app.set_user_admin('admin',False)
            with self.assertRaises(ValueError):app.delete_user('admin','pretend-other-user')

    def test_default_admin_can_be_suspended_after_other_owner_admin_exists(self):
        app.add_user('clarsp@live.se',PASSWORD,is_admin=True,profile_role='owner')
        token=app.create_session_token('clarsp@live.se')
        with app.access.web_request(token):app.set_user_suspended('admin',True)
        self.assertIsNone(app.validate_session_token(self.tokens['admin']))
        self.assertEqual(app.validate_session_token(token),'clarsp@live.se')
        app.ensure_db()
        self.assertFalse(app.authenticate_user('admin','test-only-password'))

    def test_last_active_profile_owner_must_transfer_before_suspension(self):
        with self.acting('viewer'):pid=app.access.create_draft('Private draft')
        with self.acting('admin'):
            with self.assertRaises(ValueError):app.set_user_suspended('viewer',True)
            app.access.set_membership(pid,'owner','owner')
            app.set_user_suspended('viewer',True)

    def test_users_cannot_change_another_login(self):
        with self.acting('viewer'):
            with self.assertRaises(PermissionError):app.update_user_avatar('admin','account_circle')
            with self.assertRaises(PermissionError):app.change_user_password('admin',PASSWORD,PASSWORD+'!')
            with self.assertRaises(PermissionError):app.get_user_profile('admin')

    def test_web_session_creation_requires_password_authentication(self):
        with app.access.web_request():
            with self.assertRaises(PermissionError):app.create_session_token('admin')
            self.assertTrue(app.authenticate_user('viewer',PASSWORD))
            with self.assertRaises(PermissionError):app.create_session_token('admin')
            self.assertTrue(app.authenticate_user('viewer',PASSWORD))
            token=app.create_session_token('viewer')
            self.assertEqual(app.validate_session_token(token),'viewer')

    def test_password_change_invalidates_all_old_sessions(self):
        with self.acting('viewer'):app.change_user_password('viewer',PASSWORD,PASSWORD+'!')
        self.assertIsNone(app.validate_session_token(self.tokens['viewer']))
        self.assertFalse(app.authenticate_user('viewer',PASSWORD))
        self.assertTrue(app.authenticate_user('viewer',PASSWORD+'!'))

    def test_cache_hits_still_check_membership_and_draft_boundary(self):
        import streamlit_app as ui
        ui.get_ui_dashboard_summary.clear()
        with self.acting('viewer'):ui.get_ui_dashboard_summary(str(app.DB_PATH))
        with self.acting('unassigned'):
            with self.assertRaises(PermissionError):ui.get_ui_dashboard_summary(str(app.DB_PATH))
        with self.acting('viewer'):pid=app.access.create_draft('Future')
        with self.acting('viewer',pid):
            with self.assertRaises(PermissionError):ui.get_ui_dashboard_summary(str(app.DB_PATH))

    def test_platform_secrets_are_not_available_to_operator(self):
        import streamlit_app as ui
        with self.acting('operator'):
            with self.assertRaises(PermissionError):app.get_storage_backup_status()
            with self.assertRaises(PermissionError):app.backup_database_now()
            with self.assertRaises(PermissionError):app.verify_database_backup()
            with self.assertRaises(PermissionError):ui.load_env_config()
            with self.assertRaises(PermissionError):ui.save_env_config({'ETORO_API_KEY':'never-written'})

    def test_viewer_cannot_revoke_another_users_session(self):
        with self.acting('viewer'):
            with self.assertRaises(PermissionError):app.revoke_session_token(self.tokens['admin'])
        self.assertEqual(app.validate_session_token(self.tokens['admin']),'admin')

    def test_populated_legacy_schema_upgrade_preserves_data_and_owners(self):
        import sqlite3,profile_access
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;c.execute('pragma foreign_keys=on')
        c.executescript("""CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,is_admin INTEGER,is_active INTEGER,password_hash TEXT);
        CREATE TABLE trades(id INTEGER PRIMARY KEY,status TEXT,policy_json TEXT,broker_position_id TEXT);
        INSERT INTO users VALUES (1,'admin',1,1,'unchanged'),(2,'clarsp@live.se',1,1,'also-unchanged'),(3,'friend',0,1,'preserved');
        INSERT INTO trades VALUES (7,'EXIT_PENDING','{"holding_hours":24}','broker-p');""")
        profile_access.migrate(c);profile_access.migrate(c)
        self.assertEqual(c.execute('SELECT status,policy_json,broker_position_id,trading_profile_id FROM trades').fetchone()[:],('EXIT_PENDING','{"holding_hours":24}','broker-p',1))
        self.assertEqual([tuple(r) for r in c.execute('SELECT user_id,role FROM profile_memberships ORDER BY user_id')],[(1,'owner'),(2,'owner'),(3,'viewer')])
        self.assertEqual(c.execute('SELECT password_hash FROM users WHERE id=2').fetchone()[0],'also-unchanged')
        self.assertEqual(c.execute('pragma foreign_key_check').fetchall(),[]);c.close()

    def test_concurrent_demotions_cannot_remove_all_active_admins(self):
        from concurrent.futures import ThreadPoolExecutor
        app.add_user('second-admin',PASSWORD,is_admin=True,profile_role='owner')
        def demote(name):
            try:app.set_user_admin(name,False);return 'ok'
            except ValueError:return 'protected'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(demote,['admin','second-admin']))
        c=app.get_connection();remaining=c.execute('SELECT count(*) FROM users WHERE is_admin=1 AND is_active=1').fetchone()[0];c.close()
        self.assertEqual(remaining,1);self.assertEqual(sorted(results),['ok','protected'])

    def ui(self,username):
        from streamlit.testing.v1 import AppTest
        ui=AppTest.from_file(str(Path(app.__file__).with_name('streamlit_app.py')),default_timeout=30)
        ui.session_state['auth_token']=self.tokens[username]
        ui.session_state['authenticated']=True
        ui.session_state['username']=username
        return ui

    def test_viewer_ui_has_read_only_profile_access_and_personal_settings(self):
        ui=self.ui('viewer')
        ui.session_state['viewer_navigation']='Position management'
        ui.run()
        self.assertFalse(ui.exception,[e.message for e in ui.exception])
        self.assertTrue(any(b.label=='Run configuration audit' and b.disabled for b in ui.button))
        self.assertFalse(any(e.label=='Broker settings' for e in ui.expander))
        ui.button(key='open_profile_from_header').click().run()
        self.assertFalse(ui.exception,[e.message for e in ui.exception])
        self.assertTrue(any(s.value=='Trading profiles and access' for s in ui.subheader))
        self.assertFalse(any(s.value=='User management' for s in ui.subheader))

    def test_operator_ui_hides_platform_credentials_but_keeps_profile_controls(self):
        ui=self.ui('operator');ui.run()
        self.assertFalse(ui.exception,[e.message for e in ui.exception])
        labels=[e.label for e in ui.expander]
        self.assertNotIn('Broker settings',labels)
        self.assertNotIn('Trade email notifications',labels)
        self.assertIn('Research Actions',labels)

    def test_suspended_session_cannot_reuse_authenticated_ui_state(self):
        app.set_user_suspended('viewer',True)
        ui=self.ui('viewer');ui.run()
        self.assertFalse(ui.exception,[e.message for e in ui.exception])
        self.assertTrue(any(b.label=='Login' for b in ui.button))
        self.assertFalse(any(s.label=='Active trading profile' for s in ui.selectbox))

    def test_draft_ui_never_renders_shared_trading_dashboard(self):
        with self.acting('viewer'):pid=app.access.create_draft('Not connected','ibkr')
        ui=self.ui('viewer');ui.session_state['active_profile_id']=pid;ui.run()
        self.assertFalse(ui.exception,[e.message for e in ui.exception])
        self.assertTrue(any('Draft profile:' in info.value for info in ui.info))
        self.assertFalse(any(b.label=='Run configuration audit' for b in ui.button))
        self.adapter.get_positions.assert_not_called()
