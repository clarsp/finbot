"""Optional native TSL: directions, activation gates, continued verification, no resets."""
import asyncio
import json
import unittest
from unittest.mock import Mock,patch
import makemoney as app
import native_trailing
import position_policy
import test_broker_recovery as recovery_tests


class NativeTrailingTests(unittest.TestCase):
    setUp=recovery_tests.RecoveryTests.setUp
    trade=recovery_tests.RecoveryTests.trade
    row=recovery_tests.RecoveryTests.row
    update=recovery_tests.RecoveryTests.update

    def prime(self,side='LONG',price=103):
        self.t['side']=side;self.p['side']=side
        self.t['policy_json']=json.dumps({'atr14':4})
        self.update(self.tid,side=side,policy_json=self.t['policy_json'])
        self.p.update(stop_loss_price=95 if side=='LONG' else 105,take_profit_price=110 if side=='LONG' else 90)
        self.t.update(stop_loss_price=self.p['stop_loss_price'],take_profit_price=self.p['take_profit_price'])
        self.manager.rules={**self.manager.rules,'profit_trailing_enabled':True,'profit_trailing_arming_basis':'atr','profit_trailing_mode':'broker'}
        self.adapter.supports_native_trailing=True
        self.adapter.get_positions.return_value={'available':True,'positions':[self.p]}
        rate={'bid':price,'ask':price+.1} if side=='LONG' else {'bid':price-.1,'ask':price}
        self.adapter.get_rate.return_value=rate
        c=app.get_connection();e=position_policy.observe_trailing_profit(c,self.t,self.p,rate,self.now,self.manager.rules);c.close()
        return e

    def reconcile(self,e):
        return asyncio.run(native_trailing.reconcile(self.manager,self.t,self.p,self.adapter,e))

    def test_scheduled_mode_is_default_and_performs_no_native_write(self):
        self.assertEqual(app.get_agent_rules()['position_manager']['profit_trailing_mode'],'scheduled')
        e=self.prime();self.manager.rules['profit_trailing_mode']='scheduled'
        self.reconcile(e);self.adapter.modify_position.assert_not_called()
        self.assertIsNone(self.manager.get(self.tid,'protection'))

    def assert_native_payload(self,side,price,expected):
        e=self.prime(side,price)
        result=self.reconcile(e)
        self.assertEqual(result['status'],'update_pending');self.assertFalse(result['confirmed'])
        action=self.manager.get(self.tid,'protection')
        self.assertAlmostEqual(action['payload']['stop_loss_price'],expected)
        self.assertEqual(action['payload']['stop_loss_type'],'trailing')
        self.assertEqual(self.adapter.modify_position.call_args.kwargs['stop_type'],'trailing')
        self.assertEqual(self.adapter.modify_position.call_args.args[2],self.t['take_profit_price'])
        self.assertEqual(action['state'],'ACKNOWLEDGED')
        self.p.update(stop_loss_type='trailing',stop_loss_price=expected+(0.1 if side=='LONG' else -0.1))
        asyncio.run(self.manager.observe_protection(self.t,self.p,self.adapter))
        self.assertIsNone(self.manager.get(self.tid,'protection'))
        self.assertEqual(self.row(self.tid)['status'],'OPEN')

    def test_long_native_payload_and_ack_requires_verification(self):
        self.assert_native_payload('LONG',103,102.4)

    def test_short_native_payload_and_ack_requires_verification(self):
        self.assert_native_payload('SHORT',97,97.6)

    def test_unknown_native_submission_never_resets_trailing_distance(self):
        e=self.prime();self.adapter.modify_position.side_effect=TimeoutError('ambiguous')
        self.assertEqual(self.reconcile(e)['status'],'update_unconfirmed')
        for n in range(8):
            self.now+=3600
            asyncio.run(self.manager.observe_protection(self.t,self.p,self.adapter))
            self.reconcile(e)
        self.adapter.modify_position.assert_called_once()

    def test_broker_ratchet_confirms_without_exact_stop_equality(self):
        e=self.prime();self.reconcile(e)
        self.p.update(stop_loss_type='trailing',stop_loss_price=103.2)
        asyncio.run(self.manager.observe_protection(self.t,self.p,self.adapter))
        self.assertIsNone(self.manager.get(self.tid,'protection'))
        self.assertEqual(self.row(self.tid)['stop_loss_price'],103.2)
        self.adapter.modify_position.assert_called_once()

    def test_major_upgrade_retains_tighter_active_prime_stop(self):
        e=self.prime();self.reconcile(e)
        self.p.update(stop_loss_type='trailing',stop_loss_price=102.4)
        asyncio.run(self.manager.observe_protection(self.t,self.p,self.adapter))
        self.p['stop_loss_price']=104.4
        major=position_policy.trailing_evidence(self.t,self.p,{'bid':105,'ask':105.1},rules=self.manager.rules)
        result=self.reconcile(major)
        self.assertEqual(result['status'],'broker_active_keep_tighter')
        self.assertEqual(result['retained_distance_atr'],.15)
        self.adapter.modify_position.assert_called_once()

    def test_native_confirmation_is_checked_even_after_stop_is_placed(self):
        e=self.prime();self.reconcile(e)
        self.p.update(stop_loss_type='trailing',stop_loss_price=102.4)
        asyncio.run(self.manager.observe_protection(self.t,self.p,self.adapter))
        self.assertTrue(self.reconcile(e)['confirmed'])
        self.p['stop_loss_type']='fixed'
        self.adapter.get_positions.return_value={'available':False,'positions':[]}
        result=self.reconcile(e)
        self.assertFalse(result['confirmed'])
        self.assertEqual(self.adapter.modify_position.call_count,1)

    def test_refreshing_native_peaks_does_not_postpone_scheduled_reconfirmation(self):
        self.prime()
        c=app.get_connection()
        position_policy.observe_trailing_profit(c,self.t,self.p,{'bid':104,'ask':104.1},self.now+60,self.manager.rules,peak_only=True)
        self.assertIsNone(position_policy.observe_trailing_profit(c,self.t,self.p,{'bid':104,'ask':104.1},self.now+1799,self.manager.rules))
        e=position_policy.observe_trailing_profit(c,self.t,self.p,{'bid':104,'ask':104.1},self.now+1800,self.manager.rules)
        self.assertEqual(e['best_price'],104);c.close()

    def test_fresh_rebound_below_arming_withholds_native_order(self):
        e=self.prime();self.adapter.get_rate.return_value={'bid':100,'ask':100.1}
        self.reconcile(e);self.adapter.modify_position.assert_not_called()

    def test_short_ratchet_is_adopted_without_widening(self):
        self.prime('SHORT',97);self.p.update(stop_loss_type='trailing',stop_loss_price=96)
        self.assertTrue(native_trailing.adopt_verified_stop(app.get_connection,self.t,self.p))
        self.assertEqual(self.t['stop_loss_price'],96)
        self.p['stop_loss_price']=98
        self.assertFalse(native_trailing.adopt_verified_stop(app.get_connection,self.t,self.p))
        self.assertEqual(self.row(self.tid)['stop_loss_price'],96)

    def test_fixed_stop_does_not_confirm_native_and_native_never_loosens(self):
        e=self.prime();self.p['stop_loss_price']=102.8
        intent=position_policy.native_trailing_intent(e,self.p)
        self.assertEqual(intent['stop_loss_price'],102.8)
        self.assertFalse(self.manager.protection_matches(intent,self.p))
        self.assertTrue(self.manager.protection_matches(intent,{**self.p,'stop_loss_type':'trailing','stop_loss_price':103}))

    def test_native_initial_stop_retains_the_observed_peak_after_a_small_retreat(self):
        self.prime()
        e=position_policy.trailing_evidence(self.t,self.p,{'bid':102.8,'ask':102.9},103,self.manager.rules)
        intent=position_policy.native_trailing_intent(e,self.p)
        self.assertAlmostEqual(intent['stop_loss_price'],102.4)
        self.assertAlmostEqual(intent['profit_trailing']['initial_effective_distance_atr'],.1)

    def test_native_flag_with_wrong_stop_distance_does_not_suppress_fallback(self):
        e=self.prime();self.p.update(stop_loss_type='trailing',stop_loss_price=95)
        result=self.reconcile(e)
        self.assertFalse(result['confirmed']);self.assertEqual(result['status'],'broker_distance_unconfirmed')

    def test_adapter_without_native_capability_keeps_scheduled_fallback(self):
        e=self.prime();self.adapter.supports_native_trailing=False
        self.assertEqual(self.reconcile(e)['status'],'unsupported_adapter')
        self.adapter.modify_position.assert_not_called()

    def test_fresh_native_confirmation_withholds_a_stale_scheduled_close(self):
        e=self.prime();self.p.update(stop_loss_type='trailing',stop_loss_price=102.4)
        self.adapter.get_positions.return_value={'available':True,'positions':[self.p]}
        self.adapter.get_rate.return_value={'bid':102.3,'ask':102.4}
        result=asyncio.run(self.manager.request_close(self.t,self.p,self.adapter,'profit_trailing:drawdown'))
        self.assertTrue(result['withheld']);self.adapter.close_position.assert_not_called()

    def test_main_maintenance_keeps_monitoring_without_competing_with_native_stop(self):
        self.prime()
        self.p.update(stop_loss_type='trailing',stop_loss_price=102.4)
        self.update(self.tid,stop_loss_price=102.4,take_profit_price=110,strategy_version=app.WORKFLOW_VERSION,
            policy_json=json.dumps({'atr14':4,'fill_adjusted':True}))
        self.adapter.get_positions.return_value={'available':True,'positions':[self.p]}
        self.adapter.get_rate.return_value={'bid':102.3,'ask':102.4}
        for version in ['news_reaction_v2', app.WORKFLOW_VERSION]:
            self.update(self.tid, strategy_version=version)
            with patch.object(app.PositionManagerAgent,'recovery_manager',return_value=self.manager):
                asyncio.run(app.manage_etoro_demo_positions(self.settings))
        self.adapter.close_position.assert_not_called()
        c=app.get_connection()
        e=json.loads(c.execute("SELECT details FROM trade_events WHERE trade_id=? AND stage='profit_trailing' ORDER BY id DESC LIMIT 1",(self.tid,)).fetchone()[0]);c.close()
        self.assertTrue(e['broker_trailing']['confirmed'])
        self.assertTrue(e['close_due'])

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):app.update_agent_rules('position_manager',{'profit_trailing_mode':'unknown'})

    def test_amount_arming_preserves_atr_distance_and_fresh_cash_peak(self):
        self.prime()
        self.manager.rules['profit_trailing_arming_basis']='amount'
        self.p['unrealized_pnl']=11
        c=app.get_connection()
        e=position_policy.observe_trailing_profit(c,self.t,self.p,{'bid':101,'ask':101.1},self.now+1800,self.manager.rules)
        c.close()
        self.assertEqual(e['tier'],'prime');self.assertEqual(e['best_price'],101)
        self.assertAlmostEqual(e['trigger_price'],100.4)
        self.reconcile(e)
        self.adapter.modify_position.assert_called_once()
        self.assertEqual(self.manager.get(self.tid,'protection')['payload']['profit_trailing']['peak_profit_usd'],11)

    def test_amount_floor_does_not_remove_existing_native_stop(self):
        self.prime()
        self.manager.rules['profit_trailing_arming_basis']='amount'
        self.p.update(unrealized_pnl=5,stop_loss_type='trailing',stop_loss_price=102)
        e=position_policy.trailing_evidence(self.t,self.p,{'bid':103,'ask':103.1},rules=self.manager.rules)
        self.assertEqual(e['tier'],'tracking')
        self.reconcile(e)
        self.adapter.modify_position.assert_not_called()
        self.adapter.close_position.assert_not_called()
