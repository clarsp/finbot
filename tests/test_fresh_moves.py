import asyncio
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch, AsyncMock

import pandas as pd
import makemoney as app
import test_two_direction_workflow as fixtures
from trading_workflow import select_entry_side, continuation_setup, size_order

real_load_news_reaction = app.load_news_reaction


def structure():
    stamp=datetime.now(timezone.utc).isoformat()
    return {'available':True, 'as_of':stamp,
        'support':[{'low':100,'high':100,'timeframe':'monthly','source_through':stamp}],
        'resistance':[{'low':100,'high':100,'timeframe':'monthly','source_through':stamp}]}


class FreshMoveTests(unittest.TestCase):
    def candidate(self, side):
        long = side == 'LONG'; price = 104 if long else 96
        return dict(signal='bullish' if long else 'bearish',confidence=.9,price=price,
            ai_analysis={'reaction_assessment':'temporary'},
            daily_trend_context={'available':True,'trend_state':'mixed','atr14':2,'price_structure':structure()},
            news_reaction={'available':True,'bars':2,'anchor':100,'low':95,'high':105,
                          'last':103.9 if long else 96.1,'previous':103.5 if long else 96.5})

    def test_confirmed_up_and_down_breakouts_choose_continuation_and_valid_targets(self):
        for side in ['LONG','SHORT']:
            c=self.candidate(side); setup=continuation_setup(c,side,{})
            self.assertTrue(setup['approved'],setup['reasons'])
            self.assertEqual(select_entry_side(c,{}),side)
            r=size_order(side=side,entry=c['price'],atr=2,equity=10000,cash=10000,
                leverage=2,positions=[],risk_rules={},execution_rules={},setup=setup,structure=structure())
            self.assertEqual(r['protection']['framework'],'NEWS_BREAKOUT_CONTINUATION')
            self.assertFalse(r['execution_reasons'],r['execution_reasons'])
            self.assertEqual(r['proposed_take_profit'],110 if side=='LONG' else 90)
            self.assertEqual(abs(r['proposed_stop']-c['price']),4)

    def test_unconfirmed_breakouts_stay_blocked_without_wrong_side_targets(self):
        for side in ['LONG','SHORT']:
            c=self.candidate(side);c['news_reaction']['available']=False
            setup=continuation_setup(c,side,{})
            r=size_order(side=side,entry=c['price'],atr=2,equity=10000,cash=10000,
                leverage=2,positions=[],risk_rules={},execution_rules={},setup=setup,structure=structure())
            self.assertFalse(setup['approved']);self.assertTrue(r['execution_reasons'])
            self.assertGreater((r['proposed_take_profit']-c['price'])*(1 if side=='LONG' else -1),0)

    def test_two_bars_hold_followthrough_and_current_price_are_all_required(self):
        for update in [{'bars':1},{'previous':99.9},{'last':103.4}]:
            c=self.candidate('LONG');c['news_reaction'].update(update)
            self.assertFalse(continuation_setup(c,'LONG',{})['approved'])
        c=self.candidate('LONG');c['price']=100.1
        self.assertFalse(continuation_setup(c,'LONG',{})['approved'])

    def test_unavailable_or_absent_boundaries_do_not_claim_a_confirmed_breakout(self):
        for s in [{'available':False},{'available':True,'resistance':[],'support':[]}]:
            c=self.candidate('LONG');c['daily_trend_context']['price_structure']=s
            self.assertFalse(continuation_setup(c,'LONG',{})['approved'])

    def test_breakout_keeps_real_opposing_zone_ahead_and_positive_cost_gate(self):
        c=self.candidate('LONG');s=structure();s['resistance'].append({'low':106,'high':106.2})
        c['daily_trend_context']['price_structure']=s;setup=continuation_setup(c,'LONG',{})
        r=size_order(side='LONG',entry=104,atr=2,equity=10000,cash=10000,leverage=2,
            positions=[],risk_rules={},execution_rules={},setup=setup,structure=s)
        self.assertAlmostEqual(r['proposed_take_profit'],105.8)
        self.assertTrue(r['economic_gate']['passed'])

    def test_toggles_disable_new_paths(self):
        c=self.candidate('LONG')
        self.assertFalse(continuation_setup(c,'LONG',{'fresh_boundary_entries_enabled':False})['approved'])
        self.assertEqual(select_entry_side(c,{'fresh_boundary_entries_enabled':False,'allow_mixed_trend_reversals':False}),'LONG')


class FreshMoveWorkflowTests(unittest.TestCase):
    setUp=fixtures.DatabaseTests.setUp
    trade=fixtures.DatabaseTests.trade
    row=fixtures.DatabaseTests.row
    update=fixtures.DatabaseTests.update

    def test_amzn_style_mixed_trend_dip_qualifies_long_only_after_recovery(self):
        c=dict(ticker='AMZN',company_name='Amazon',signal='bearish',company_news_sentiment='bearish',
            confidence=.7,price=225.63,fundamentals={},ai_analysis={'reaction_assessment':'temporary'},
            daily_trend_context={'available':True,'trend_state':'mixed','atr14':7.000710623604911},
            news_reaction={'available':True,'bars':3,'anchor':243.79,'low':223.85,'high':243.79,
                           'last':225.63,'previous':225.2})
        s=app.StrategistAgent().analyze_and_decide({'tech_data':c})
        self.assertEqual(s['position'],'LONG');self.assertTrue(s['decision_gate']['accepted'])
        r=app.RiskManagerAgent().review_news_demo_candidate(candidate=c,strategy=s,
            account={'available':True,'equity':10000,'cash_available':10000},
            broker_snapshot={'available':True,'positions':[]},instrument=self.adapter.resolve_instrument.return_value,
            atr14=c['daily_trend_context']['atr14'])
        self.assertEqual(r['risk_budget_multiplier'],.5)
        c['news_reaction']={'available':False}
        rejected=app.StrategistAgent().analyze_and_decide({'tech_data':c})
        self.assertEqual(rejected['position'],'LONG');self.assertFalse(rejected['decision_gate']['accepted'])
        self.submit.assert_not_awaited()

    def test_mixed_rebounds_both_directions_reach_one_shared_submission(self):
        for signal,side,price,anchor,previous in [('bearish','LONG',95,110,94),('bullish','SHORT',105,90,106)]:
            tid=self.trade(signal)
            c=app.get_connection();event=c.execute("select details from trade_events where trade_id=? and stage='strategy'",(tid,)).fetchone()
            d=json.loads(event[0]);d['candidate_context']['ai_analysis']={'reaction_assessment':'temporary'}
            c.execute("update trade_events set details=? where trade_id=? and stage='strategy'",(json.dumps(d),tid));c.commit();c.close()
            bars=pd.DataFrame([dict(Close=100,sma50=100,sma200=100,atr14=10)],index=pd.to_datetime(['2026-10-29'],utc=True))
            reaction=dict(available=True,bars=3,anchor=anchor,low=90,high=110,last=price,previous=previous)
            quote=dict(available=True,provider='eToro',ask=price if side=='LONG' else price+.1,
                       bid=price-.1 if side=='LONG' else price,fetched_at=datetime.now(timezone.utc).isoformat())
            before=self.submit.await_count
            with patch.object(app,'load_daily_bars',return_value=bars),patch.object(app,'load_news_reaction',return_value=reaction),patch.object(app,'fetch_market_quote',AsyncMock(return_value=quote)):
                asyncio.run(app.place_analysis_candidate_demo_order(tid))
            self.assertEqual(self.submit.await_count,before+1)
            self.assertEqual(self.submit.call_args.args[0]['side'],side)
            policy=json.loads(self.row(tid)['policy_json'])
            self.assertEqual(policy['framework'],'CONDITIONAL_MEAN_REVERSION')
            self.assertEqual(self.submit.call_args.args[0]['take_profit_price'],anchor)
            self.update(tid,status='CLOSED')

    def test_later_commentary_does_not_reset_persisted_catalyst_anchor(self):
        now=datetime.now(timezone.utc)
        start=pd.Timestamp(now).floor('5min')-pd.Timedelta(minutes=45)
        bars=pd.DataFrame({'Close':[100]*9,'Low':[98]*9,'High':[101]*9},index=pd.date_range(start,periods=9,freq='5min'))
        first={'title':'Amazon earnings results','published_at':(now-timedelta(minutes=30)).isoformat(),'article_key':'first'}
        later={'title':'Amazon earnings update','published_at':(now-timedelta(minutes=5)).isoformat(),'article_key':'later'}
        with patch('yfinance.Ticker',return_value=Mock(history=Mock(return_value=bars))):
            one=real_load_news_reaction('AMZN',[first])
            two=real_load_news_reaction('AMZN',[later])
        self.assertEqual(one['catalyst_anchor']['event_at'],two['catalyst_anchor']['event_at'])
        self.assertEqual(two['catalyst_anchor']['article_key'],'first')
        self.assertTrue(two['available'],two)
        c=app.get_connection();c.execute('update reaction_anchors set event_at=? where symbol=?',((now-timedelta(hours=25)).timestamp(),'AMZN'));c.commit();c.close()
        with patch('yfinance.Ticker',return_value=Mock(history=Mock(return_value=bars))):
            expired=real_load_news_reaction('AMZN',[later])
        self.assertFalse(expired['available']);self.assertIn('expired',expired['reason'])

    def test_existing_position_policy_and_trailing_settings_are_not_changed(self):
        tid=self.trade();self.update(tid,status='OPEN',policy_json=json.dumps({'framework':'DIRECTIONAL_NEWS','atr14':5}))
        before=self.row(tid)['policy_json'];settings=copy.deepcopy(self.rules['position_manager'])
        new_trade=self.trade()
        asyncio.run(app.place_analysis_candidate_demo_order(new_trade))
        self.assertEqual(self.row(tid)['policy_json'],before)
        self.assertEqual(self.rules['position_manager'],settings)

    def test_breakouts_both_directions_reach_submission_with_atr_protection(self):
        for signal,side,price in [('bullish','LONG',104),('bearish','SHORT',96)]:
            tid=self.trade(signal)
            candidate=FreshMoveTests().candidate(side)
            candidate['ai_analysis']['reaction_assessment']='structural'
            quote=dict(available=True,provider='eToro',ask=price if side=='LONG' else price+.1,
                       bid=price-.1 if side=='LONG' else price,fetched_at=datetime.now(timezone.utc).isoformat())
            daily=candidate['daily_trend_context']
            with patch.object(app,'evaluate_latest_signal',return_value=daily),patch.object(app,'load_news_reaction',return_value=candidate['news_reaction']),patch.object(app,'fetch_market_quote',AsyncMock(return_value=quote)):
                asyncio.run(app.place_analysis_candidate_demo_order(tid))
            payload=self.submit.call_args.args[0]
            self.assertEqual(payload['side'],side)
            self.assertEqual(payload['take_profit_price'],110 if side=='LONG' else 90)
            self.assertEqual(json.loads(self.row(tid)['policy_json'])['framework'],'NEWS_BREAKOUT_CONTINUATION')
            self.update(tid,status='CLOSED')

    def test_expiry_during_cost_review_recalculates_price_sizing_and_stops_once(self):
        tid=self.trade()
        first=dict(available=True,provider='eToro',bid=99.9,ask=100,fetched_at=datetime.now(timezone.utc).isoformat())
        second=dict(available=True,provider='eToro',bid=103.9,ask=104,fetched_at=datetime.now(timezone.utc).isoformat())
        calls=[]
        def costs(*args):
            calls.append(args)
            if len(calls)==1:first['fetched_at']='2020-01-01T00:00:00+00:00'
            return {}
        self.adapter.estimate_order_costs.side_effect=costs
        with patch.object(app,'fetch_market_quote',AsyncMock(side_effect=[first,second])):
            asyncio.run(app.place_analysis_candidate_demo_order(tid))
        self.assertEqual(self.submit.await_count,1)
        order=self.submit.call_args.args[0]
        self.assertEqual(order['stop_loss_price'],100)
        self.assertEqual(order['take_profit_price'],110)
        self.assertLessEqual(order['units']*104/order['leverage'],200)
        self.assertEqual(len(calls),2)
