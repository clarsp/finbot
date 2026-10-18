import json
import unittest
from datetime import datetime,timezone
import pandas as pd
import makemoney as app
import test_two_direction_workflow as fixtures
import test_price_structure as structure_fixtures
from trading_workflow import size_order
from unittest.mock import patch

class RangeWorkflowTests(unittest.TestCase):
    setUp=fixtures.DatabaseTests.setUp
    trade=fixtures.DatabaseTests.trade
    row=fixtures.DatabaseTests.row
    update=fixtures.DatabaseTests.update

    def test_production_and_one_atr_shadow_use_different_saved_rules(self):
        structure=structure_fixtures.RangeBoundaryTests().structure()
        result=size_order(side='LONG',entry=37,atr=10,equity=10000,cash=10000,leverage=2,
            positions=[],risk_rules={**self.rules['risk_manager'],'minimum_reward_risk':.1},
            execution_rules=self.rules['execution'],structure=structure)
        self.assertEqual(result['proposed_take_profit'],48)
        self.assertEqual(result['protection']['target_selection']['mode'],'range_boundary')
        self.assertEqual(result['proposed_stop'],17)
        import shadow_tracking as shadow
        policy=dict(framework='DIRECTIONAL_NEWS',atr14=10,requested_entry=37,holding_hours=48,
            stop_offset=-20,target_offset=11,target_selection=result['protection']['target_selection'],
            cost_assumptions=result['cost_assumptions'])
        plan=shadow.build_plan(self.rules['risk_manager'],{**result,'decision':'accepted','execution_ready':True},policy,'1')
        tid=self.trade();t=self.row(tid);t['policy_json']=json.dumps({'shadow_plan':plan})
        c=app.get_connection()
        shadow.initialize(c,t,dict(open_rate=37,units=result['proposed_units'],opened_at=datetime.now(timezone.utc).isoformat()))
        c.commit();c.close()
        rows=app.get_shadow_exits(tid)
        one=next(r for r in rows if r['variant']=='2S/1T')
        baseline=next(r for r in rows if r['variant']=='production')
        self.assertEqual(one['target'],39)
        self.assertEqual(one['target_selection_mode'],'nearest')
        self.assertEqual(baseline['target'],48)
        self.assertEqual(baseline['target_selection_mode'],'range_boundary')

    def test_open_position_target_can_extend_but_stop_and_quantity_are_unchanged(self):
        tid=self.trade();t=self.row(tid)
        t.update(side='LONG',policy_json=json.dumps({'framework':'DIRECTIONAL_NEWS','atr14':10.3333333333333}))
        pos=dict(side='LONG',open_rate=37,units=4,stop_loss_price=30,take_profit_price=39.8)
        original=pos.copy()
        quote=dict(available=True,provider='eToro',bid=38,ask=38.1,fetched_at=datetime.now(timezone.utc).isoformat())
        bars=pd.DataFrame([{'atr14':2}])
        with patch('price_structure.multi_timeframe_structure',return_value=structure_fixtures.RangeBoundaryTests().structure()):
            review=app.RiskManagerAgent().review_open_position_target(t,pos,quote,bars)
        self.assertEqual(review['status'],'eligible')
        self.assertAlmostEqual(review['new_target'],48.8)
        self.assertEqual(review['stop'],30)
        self.assertEqual(pos,original)
        self.submit.assert_not_awaited()

    def test_open_position_review_preserves_mean_reversion_and_rejects_stale_quote(self):
        tid=self.trade();t=self.row(tid)
        r=app.RiskManagerAgent().review_open_position_target(t,{}, {},None)
        self.assertEqual(r['status'],'skipped')
        t['policy_json']=json.dumps({'framework':'CONDITIONAL_MEAN_REVERSION'})
        quote=dict(available=True,provider='eToro',bid=38,ask=38.1,fetched_at=datetime.now(timezone.utc).isoformat())
        r=app.RiskManagerAgent().review_open_position_target(t,{},quote,None)
        self.assertIn('anchor',r['reason'])

    def test_rule_validation_rejects_invalid_range_windows(self):
        for key,value in [('structure_target_mode','unknown'),('structure_range_months',0),('structure_range_months',1.5),('structure_range_weeks',True)]:
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                app.update_agent_rules('risk_manager',{key:value})
            self.rules['risk_manager']=dict(app.DEFAULT_AGENT_RULES['risk_manager'])

    def test_existing_position_quote_uses_verified_id_and_rejects_crossed_market(self):
        import asyncio
        self.adapter._get_rate.return_value={'bid':37.9,'ask':38}
        quote=asyncio.run(app.PositionManagerAgent().get_position_quote(self.adapter,{'instrument_id':'42'}))
        self.assertTrue(app.is_fresh_etoro_quote(quote))
        self.adapter._get_rate.assert_called_once_with(42)
        self.adapter.resolve_instrument.assert_not_called()
        self.adapter._get_rate.return_value={'bid':38,'ask':37}
        quote=asyncio.run(app.PositionManagerAgent().get_position_quote(self.adapter,{'instrument_id':'42'}))
        self.assertFalse(quote['available'])
