import copy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import unittest
from unittest.mock import patch
import shadow_tracking as shadow
import makemoney as app
import test_two_direction_workflow as fixtures


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:'); self.conn.row_factory=sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE trades (id INTEGER PRIMARY KEY)')
        self.conn.execute('INSERT INTO trades VALUES (1)')
        shadow.ensure_schema(self.conn)
        self.now=datetime(2026,10,17,10,tzinfo=timezone.utc)
        self.rules=copy.deepcopy(app.DEFAULT_AGENT_RULES['risk_manager'])
        self.policy=dict(framework='DIRECTIONAL_NEWS',atr14=2,stop_offset=-4,target_offset=6,
            target_selection={},holding_hours=48,requested_entry=100,
            cost_assumptions=dict(spread_per_unit=.1,roundtrip_bps=10,financing_annual_pct=8,holding_hours=48))
        self.risk=dict(execution_ready=True,decision='accepted',proposed_units=4,estimated_costs=1)
        self.plan=shadow.build_plan(self.rules,self.risk,self.policy,'10')
        self.trade=dict(id=1,symbol='ACME',side='LONG',leverage=2,policy_json=json.dumps({'shadow_plan':self.plan}))
        self.fill=dict(open_rate=100,units=4,opened_at=self.now.isoformat(),leverage=2)

    def rows(self): return shadow.rows(self.conn)

    def test_no_plan_for_rejected_disabled_or_mean_reversion(self):
        self.assertIsNone(shadow.build_plan(self.rules,{**self.risk,'execution_ready':False},self.policy,'10'))
        self.assertIsNone(shadow.build_plan({**self.rules,'shadow_tracking_enabled':False},self.risk,self.policy,'10'))
        self.assertIsNone(shadow.build_plan(self.rules,self.risk,{**self.policy,'framework':'CONDITIONAL_MEAN_REVERSION'},'10'))

    def test_frozen_plan_and_idempotent_verified_fill(self):
        self.policy['cost_assumptions']['roundtrip_bps']=900
        self.assertEqual(self.plan['costs']['roundtrip_bps'],10)
        self.assertEqual(shadow.initialize(self.conn,{**self.trade,'policy_json':'{}'},self.fill),0)
        self.assertEqual(shadow.initialize(self.conn,self.trade,{**self.fill,'opened_at':None}),0)
        self.assertEqual(shadow.initialize(self.conn,self.trade,self.fill,self.now),15)
        self.assertEqual(shadow.initialize(self.conn,self.trade,{**self.fill,'open_rate':110},self.now),0)
        wider=next(r for r in self.rows() if r['variant']=='3S/1T')
        self.assertAlmostEqual(wider['units']*6,16)
        self.assertEqual(wider['entry'],100)
        self.assertFalse(wider['would_pass_net_reward_risk'])

    def test_structure_caps_raw_atr_and_keeps_baseline_absolute(self):
        self.plan['target_selection']={'adjusted':True,'selected_target':103,'structure':{
            'available':True,'resistance':[{'low':103.2,'high':104}], 'support':[]}}
        self.trade['policy_json']=json.dumps({'shadow_plan':self.plan})
        shadow.initialize(self.conn,self.trade,self.fill,self.now)
        row=next(r for r in self.rows() if r['variant']=='2S/2T')
        self.assertEqual(row['atr_target'],104)
        self.assertEqual(row['target'],103)
        self.assertEqual(next(r for r in self.rows() if r['variant']=='production')['target'],103)

    def test_side_prices_and_no_double_leverage_or_spread(self):
        shadow.initialize(self.conn,self.trade,self.fill,self.now)
        shadow.observe(self.conn,'10',{'bid':102,'ask':103},self.now+timedelta(seconds=60))
        row=next(r for r in self.rows() if r['variant']=='2S/1T')
        self.assertEqual(row['status'],'TARGET_OBSERVED')
        self.assertEqual(row['gross_pnl'],8)
        expected=400*(.001+.08/8760/60)
        self.assertAlmostEqual(row['estimated_costs'],expected)
        self.assertEqual(row['gap_count'],0)
        count=row['observation_count']
        shadow.observe(self.conn,'10',{'bid':120,'ask':121},self.now+timedelta(seconds=120))
        row=next(r for r in self.rows() if r['variant']=='2S/1T')
        self.assertEqual(row['observation_count'],count)
        self.assertEqual(row['exit_price'],102)

    def test_short_uses_ask_and_missing_quotes_never_exit(self):
        self.plan.update(stop_offset=4,target_offset=-6)
        self.trade.update(side='SHORT',policy_json=json.dumps({'shadow_plan':self.plan}))
        shadow.initialize(self.conn,self.trade,self.fill,self.now)
        shadow.observe(self.conn,'10',{'bid':98,'ask':99},self.now+timedelta(seconds=60))
        self.assertTrue(all(r['status']=='ACTIVE' for r in self.rows()))
        shadow.observe(self.conn,'10',{'bid':97,'ask':98},self.now+timedelta(seconds=120))
        row=next(r for r in self.rows() if r['variant']=='2S/1T')
        self.assertEqual(row['gross_pnl'],8)
        shadow.observe(self.conn,'10',{},self.now+timedelta(days=50))
        self.assertTrue(any(r['status']=='ACTIVE' for r in self.rows()))

    def test_late_observation_is_time_exit_and_flags_gap(self):
        shadow.initialize(self.conn,self.trade,self.fill,self.now)
        shadow.observe(self.conn,'10',{'bid':110},self.now+timedelta(hours=49))
        row=next(r for r in self.rows() if r['variant']=='production' and r['horizon_hours']==48)
        self.assertEqual(row['status'],'TIME_EXIT_OBSERVED')
        self.assertEqual(row['gap_count'],1)
        self.assertEqual(row['deadline_lateness_seconds'],3600)
        self.assertEqual(row['exit_price'],110)

    def test_invalid_lists_rejected(self):
        for invalid in [[],[0],[float('nan')],[True],['1'],[1,2,3,4,5]]:
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                shadow.validate_rules({**self.rules,'shadow_target_atr_multiples':invalid})


class WorkflowTests(unittest.TestCase):
    setUp=fixtures.DatabaseTests.setUp
    trade=fixtures.DatabaseTests.trade
    row=fixtures.DatabaseTests.row
    update=fixtures.DatabaseTests.update

    def test_final_submission_plan_then_fill_and_maintenance(self):
        import asyncio
        tid=self.trade()
        asyncio.run(app.place_analysis_candidate_demo_order(tid))
        self.assertEqual(app.get_shadow_exits(),[])
        row=self.row(tid)
        policy=json.loads(row['policy_json'])
        self.assertIn('shadow_plan',policy)
        p=dict(position_id='p',instrument_id='1',symbol='ACME',open_rate=100,
            units=row['quantity'],leverage=2,opened_at=datetime.now(timezone.utc).isoformat(),
            stop_loss_price=row['stop_loss_price'],take_profit_price=row['take_profit_price'])
        app.reconcile_news_demo_positions([p]);app.reconcile_news_demo_positions([p])
        self.assertEqual(len(app.get_shadow_exits(tid)),15)
        self.update(tid,status='CLOSED')
        self.adapter._get_rate.return_value={'bid':102,'ask':102.1}
        asyncio.run(app.PositionManagerAgent().observe_shadow_exits(self.adapter))
        self.assertTrue(any(r['status']=='TARGET_OBSERVED' for r in app.get_shadow_exits(tid)))
        self.assertEqual(self.submit.await_count,1)
        self.adapter.close_position.assert_not_called()
        self.adapter.update_position_protection.assert_not_called()

    def test_companies_count_current_research_separately_from_orders(self):
        first=self.trade(); second=self.trade()
        self.update(second,status='OPEN',broker_order_id='o',broker_position_id='p')
        company=app.get_company_analytics()[0]
        self.assertEqual(company['times_analyzed'],2)
        self.assertEqual(company['trade_count'],1)
        self.assertEqual(company['not_executed'],1)
        self.assertEqual(company['open_positions'],1)
        self.assertEqual(len(app.get_company_analysis_history('ACME')['trades']),2)

    def test_duplicate_requires_submitted_reference_and_current_submission(self):
        first=self.trade(); second=self.trade()
        self.update(second,status='OPEN',broker_order_id='o')
        row=app.get_trade_history(trade_ids=[second])[0]
        self.assertFalse(row['is_duplicate'])
        self.assertEqual(row['previous_analysis_trade_id'],first)
        third=self.trade()
        row=app.get_trade_history(trade_ids=[third])[0]
        self.assertEqual(row['duplicate_of_trade_id'],second)
        self.assertFalse(row['is_duplicate'])
        self.update(third,status='OPEN',broker_order_id='o2')
        self.assertTrue(app.get_trade_history(trade_ids=[third])[0]['is_duplicate'])

    def test_closed_prior_position_is_reentry_not_duplicate(self):
        first=self.trade()
        self.update(first,status='CLOSED',broker_order_id='old',closed_at='2026-01-01T00:00:00Z')
        current=self.trade()
        self.update(current,status='OPEN',broker_order_id='new')
        row=app.get_trade_history(trade_ids=[current])[0]
        self.assertFalse(row['is_duplicate'])
        self.assertIsNone(row['duplicate_of_trade_id'])
        self.assertEqual(row['previous_analysis_trade_id'],first)

    def test_max_target_atr_caps_directional_target(self):
        import trading_workflow as w
        rules={**self.rules['risk_manager'],'target_reward_risk':3,'max_target_atr_multiple':2.5}
        r=w.size_order(side='LONG',entry=100,atr=2,equity=10000,cash=10000,leverage=2,
            positions=[],risk_rules=rules,execution_rules=self.rules['execution'])
        self.assertEqual(r['proposed_take_profit'],105)
        self.assertEqual(r['protection']['target_atr_multiple'],2.5)
        self.assertEqual(r['proposed_stop'],96)

class ViewTests(unittest.TestCase):
    setUp=fixtures.DatabaseTests.setUp
    trade=fixtures.DatabaseTests.trade
    def test_companies_view_renders_current_lifecycle(self):
        from streamlit.testing.v1 import AppTest
        self.trade()
        page=AppTest.from_string('import streamlit_app as ui\nui.render_companies_dashboard()').run(timeout=20)
        self.assertEqual(list(page.exception),[])

    def test_shadow_view_renders_waiting_state(self):
        from streamlit.testing.v1 import AppTest
        page=AppTest.from_string('import streamlit_app as ui\nui.render_shadow_dashboard()').run(timeout=20)
        self.assertEqual(list(page.exception),[])


if __name__ == "__main__":
    unittest.main()
