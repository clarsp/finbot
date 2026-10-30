import json
import unittest
from datetime import datetime,timedelta,timezone
from position_timing import timing_fields,migrate_open_policy
from trading_workflow import exit_reason

class TimingTests(unittest.TestCase):
    def test_age_never_closes_but_price_protection_still_applies_both_directions(self):
        opened=datetime(2026,10,19,20,tzinfo=timezone.utc)
        for side,stop,target in [('LONG',90,120),('SHORT',110,80)]:
            trade={'side':side,'stop_loss_price':stop,'take_profit_price':target,'opened_at':opened.isoformat(),
                   'policy':timing_fields(48,24)}
            self.assertIsNone(exit_reason(trade,100,opened+timedelta(hours=25)))
            self.assertIsNone(exit_reason(trade,100,opened+timedelta(hours=480)))
            self.assertEqual(exit_reason(trade,stop,opened+timedelta(hours=1)),'stop_level_observed')
            self.assertEqual(exit_reason(trade,target,opened+timedelta(hours=1)),'take_profit_level_observed')

    def test_existing_trade_uses_saved_cap_without_resetting_opening_time(self):
        trade={'status':'OPEN','opened_at':'2026-10-19T20:00:00Z','policy_json':json.dumps({'framework':'DIRECTIONAL_NEWS','holding_hours':24,'target_offset':12}),
               'config_json':json.dumps({'risk_manager':{'news_max_hold_hours':48}})}
        policy=migrate_open_policy(trade)
        self.assertIsNone(policy['max_hold_hours'])
        self.assertFalse(policy['time_exit_enabled'])
        self.assertEqual(policy['review_after_hours'],24)
        self.assertEqual(policy['target_offset'],12)
        self.assertEqual(trade['opened_at'],'2026-10-19T20:00:00Z')
        self.assertIsNone(migrate_open_policy({**trade,'policy_json':json.dumps(policy)}))
        for status in ['CLOSED','EXIT_PENDING','SUBMITTING']:
            self.assertIsNone(migrate_open_policy({**trade,'status':status}))
        self.assertFalse(migrate_open_policy({**trade,'config_json':'{}'})['time_exit_enabled'])

    def test_mean_reversion_retains_separate_cap_and_long_forecast_cannot_extend_it(self):
        fields=timing_fields(24,72)
        self.assertEqual(fields['review_after_hours'],24)
        self.assertIsNone(fields['max_hold_hours'])
        t={'status':'OPEN','policy_json':json.dumps({'framework':'CONDITIONAL_MEAN_REVERSION','holding_hours':12}),
           'config_json':json.dumps({'risk_manager':{'news_max_hold_hours':48,'mean_reversion_max_hold_hours':24}})}
        self.assertEqual(migrate_open_policy(t)['review_after_hours'],12)


import asyncio
import makemoney as app
import test_two_direction_workflow as fixtures

class TimingWorkflowTests(unittest.TestCase):
    setUp=fixtures.DatabaseTests.setUp
    trade=fixtures.DatabaseTests.trade
    row=fixtures.DatabaseTests.row
    update=fixtures.DatabaseTests.update

    def test_new_entry_keeps_forecast_and_configured_cap_separate(self):
        tid=self.trade()
        c=app.get_connection()
        row=c.execute("SELECT id,details FROM trade_events WHERE trade_id=? AND stage='strategy'",(tid,)).fetchone()
        details=json.loads(row['details']);details['candidate_context']['ai_analysis']['forecast_horizon_hours']=24
        c.execute('UPDATE trade_events SET details=? WHERE id=?',(json.dumps(details),row['id']));c.commit();c.close()
        asyncio.run(app.place_analysis_candidate_demo_order(tid))
        policy=json.loads(self.row(tid)['policy_json'])
        self.assertEqual(policy['holding_hours'],48)
        self.assertIsNone(policy['max_hold_hours'])
        self.assertFalse(policy['time_exit_enabled'])
        self.assertEqual(policy['review_after_hours'],24)
        self.assertEqual(policy['cost_assumptions']['holding_hours'],48)

    def test_existing_position_migrates_before_expiry_check_without_resetting_clock(self):
        tid=self.trade();opened=(datetime.now(timezone.utc)-timedelta(hours=250)).isoformat()
        self.update(tid,status='OPEN',broker_position_id='p',broker_order_id='o',opened_at=opened,
            strategy_version=app.WORKFLOW_VERSION,stop_loss_price=96,take_profit_price=106,
            config_json=json.dumps(self.rules),policy_json=json.dumps({'framework':'DIRECTIONAL_NEWS','holding_hours':24,'fill_adjusted':True}))
        self.adapter.get_positions.return_value={'available':True,'positions':[dict(position_id='p',instrument_id='1',side='LONG',units=1,amount=50,open_rate=100,stop_loss_price=96,take_profit_price=106)]}
        self.adapter.get_rate.return_value={'bid':100,'ask':100.1}
        asyncio.run(app.PositionManagerAgent().manage(self.settings))
        result=self.row(tid)
        self.assertIsNone(json.loads(result['policy_json'])['max_hold_hours'])
        self.assertEqual(result['opened_at'],opened)
        self.adapter.close_position.assert_not_called()

        c=app.get_connection()
        reviews=c.execute("SELECT details FROM trade_events WHERE trade_id=? AND stage='reassessment'",(tid,)).fetchall()
        c.close()
        self.assertEqual(len(reviews),1)
        evidence=json.loads(reviews[0]['details'])
        self.assertFalse(evidence['time_exit_enabled'])
        self.assertIn('daily_trend',evidence)
        self.assertIn('broker_stop',evidence)
        asyncio.run(app.PositionManagerAgent().manage(self.settings))
        c=app.get_connection()
        self.assertEqual(c.execute("SELECT count(*) FROM trade_events WHERE trade_id=? AND stage='reassessment'",(tid,)).fetchone()[0],1)
        c.close()

    def test_fresh_opposing_structural_news_can_invalidate_but_stale_news_cannot(self):
        from position_timing import invalidation_reason
        now=datetime.now(timezone.utc)
        for side,opposing in [('LONG','bearish'),('SHORT','bullish')]:
            evidence={'signal':opposing,'confidence':.9,'openai':{'reaction_assessment':'structural'}}
            self.assertEqual(invalidation_reason({'side':side},evidence,now.isoformat(),now),'structural_news_invalidated_thesis')
            self.assertIsNone(invalidation_reason({'side':side},evidence,(now-timedelta(hours=25)).isoformat(),now))
            self.assertIsNone(invalidation_reason({'side':side},evidence,(now+timedelta(hours=1)).isoformat(),now))

    def test_missing_quote_does_not_authorize_an_age_exit_and_retry_is_bounded(self):
        tid=self.trade();opened=(datetime.now(timezone.utc)-timedelta(days=20)).isoformat()
        self.update(tid,status='OPEN',broker_position_id='p',broker_order_id='o',opened_at=opened,
            strategy_version=app.WORKFLOW_VERSION,stop_loss_price=96,take_profit_price=106,
            config_json=json.dumps(self.rules),policy_json=json.dumps({'framework':'DIRECTIONAL_NEWS','holding_hours':24,'fill_adjusted':True}))
        self.adapter.get_positions.return_value={'available':True,'positions':[dict(position_id='p',instrument_id='1',side='LONG',units=1,amount=50,open_rate=100,stop_loss_price=96,take_profit_price=106)]}
        self.adapter.get_rate.side_effect=TimeoutError('temporary')
        asyncio.run(app.PositionManagerAgent().manage(self.settings))
        self.adapter.close_position.assert_not_called()
        policy=json.loads(self.row(tid)['policy_json'])
        self.assertLess(datetime.fromisoformat(policy['next_review_at'])-datetime.now(timezone.utc),timedelta(minutes=16))

    def test_losing_positions_are_not_closed_by_news_or_size_reviews(self):
        for side,opposing,mark,stop,target,units,amount in [('LONG','bearish',95,90,120,1,50),('SHORT','bullish',105,110,80,1,50),('LONG','bearish',95,90,120,6,300),('SHORT','bullish',105,110,80,6,300)]:
            tid=self.trade()
            self.update(tid,status='OPEN',side=side,broker_position_id='p',broker_order_id='o',
                opened_at=datetime.now(timezone.utc).isoformat(),entry_price=100,
                strategy_version=app.WORKFLOW_VERSION,stop_loss_price=stop,take_profit_price=target,
                config_json=json.dumps(self.rules),policy_json=json.dumps({'framework':'DIRECTIONAL_NEWS','holding_hours':24,'fill_adjusted':True}))
            app.record_trade_event(tid,'research','researcher','completed','accepted','Opposing news',
                {'signal':opposing,'confidence':.9,'openai':{'reaction_assessment':'structural'}})
            self.adapter.get_positions.return_value={'available':True,'positions':[dict(position_id='p',instrument_id='1',side=side,units=units,amount=amount,open_rate=100,stop_loss_price=stop,take_profit_price=target)]}
            self.adapter.get_rate.return_value={'bid':mark,'ask':mark+.01}
            asyncio.run(app.PositionManagerAgent().manage(self.settings))
            self.adapter.close_position.assert_not_called()
            self.assertNotEqual(self.row(tid)['status'],'EXIT_PENDING')
            policy=json.loads(self.row(tid)['policy_json'])
            self.assertEqual(policy['last_advisory_reason'],'actual_fill_exceeded_configured_units_or_spend_cap' if units>5 else 'structural_news_invalidated_thesis')
            self.update(tid,status='CLOSED')

    def test_stale_dependency_records_one_review_then_waits_without_reloading_bars(self):
        tid=self.trade();self.update(tid,status='OPEN',broker_position_id='p')
        t=self.row(tid);now=datetime.now(timezone.utc)
        policy={'review_after_hours':24,'review_interval_hours':24}
        p={'stop_loss_price':90,'take_profit_price':120}
        from unittest.mock import patch
        with patch.object(app,'load_daily_bars',side_effect=ValueError('unavailable')) as bars:
            asyncio.run(app.PositionManagerAgent().reassess_position(t,p,policy,100,{},None))
            asyncio.run(app.PositionManagerAgent().reassess_position(t,p,policy,100,{},None))
        self.assertEqual(bars.call_count,1)
        c=app.get_connection()
        self.assertEqual(c.execute("SELECT count(*) FROM trade_events WHERE trade_id=? AND stage='reassessment'",(tid,)).fetchone()[0],1)
        self.assertEqual(c.execute('SELECT count(*) FROM held_research_requests').fetchone()[0],1);c.close()
