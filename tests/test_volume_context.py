"""Offline volume evidence and real strategy/risk boundary regressions."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import exchange_calendars as xcals
import pandas as pd
import makemoney as app
from volume_context import from_bars, confirmation, adjusted_rules, prioritize, decision_note
import test_two_direction_workflow as fixtures


def candidate(side='LONG', confidence=.67, trend=None):
    now = datetime.now(timezone.utc)
    sign = 1 if side == 'LONG' else -1
    price = 100 + sign
    return dict(ticker='TEST', signal='bullish' if sign == 1 else 'bearish',
        confidence=confidence, company_news_sentiment='bullish' if sign == 1 else 'bearish',
        price=price, fundamentals={}, ai_analysis={'reaction_assessment':'structural'},
        market_quote={'available': True, 'ask':price, 'bid':price},
        daily_trend_context={'available':True,'trend_state':trend or ('bullish' if sign == 1 else 'bearish'),'atr14':2},
        volume_context={'available':True,'rvol':2,'vwap':100,'last':price,
            'previous':price-sign*.2,'through':now.isoformat(),
            'session_open':(now-timedelta(hours=1)).isoformat(),
            'session_close':(now+timedelta(hours=1)).isoformat()})


class VolumeCalculationTests(unittest.TestCase):
    def setUp(self):
        self.calendar=xcals.get_calendar('XNYS')
        self.now=pd.Timestamp('2026-11-02 15:30Z')
        self.session=pd.Timestamp('2026-11-02')
        rows=[]; stamps=[]
        for day in self.calendar.sessions_window(self.session,-25):
            opened=self.calendar.session_open(day)
            for i in range(12):
                stamps.append(opened+pd.Timedelta(minutes=5*i))
                rows.append({'High':101,'Low':99,'Close':100,'Volume':200 if day==self.session else 100})
        self.bars=pd.DataFrame(rows,index=pd.DatetimeIndex(stamps))

    def test_same_elapsed_slots_across_dst_and_incomplete_current_bar(self):
        future=self.bars.iloc[[-1]].copy();future.index=[self.now];future['Volume']=999999
        result=from_bars(pd.concat([self.bars,future]),self.calendar,self.now)
        self.assertTrue(result['available'],result)
        self.assertEqual(result['rvol'],2)
        self.assertEqual(result['vwap'],100)
        self.assertEqual(result['completed_bars'],12)
        self.assertEqual(result['baseline_sessions'],20)
        self.assertEqual(result['time_of_day'],'midday')
        self.assertIn('intraday_structure_state',result)
        self.assertIn('opening_range_high',result)
        self.assertIn('gap_pct',result)

    def test_missing_current_slot_and_missing_baseline_are_unavailable(self):
        result=from_bars(self.bars.drop(self.bars.index[-2]),self.calendar,self.now)
        self.assertFalse(result['available'])
        self.assertFalse(from_bars(self.bars.iloc[-12:],self.calendar,self.now)['available'])

    def test_closed_session_invalid_volume_and_duplicate_bars_are_unavailable(self):
        self.assertFalse(from_bars(self.bars,self.calendar,'2026-11-02 22:00Z')['available'])
        self.bars.iloc[-1,self.bars.columns.get_loc('Volume')]=-1
        self.assertFalse(from_bars(self.bars,self.calendar,self.now)['available'])
        self.assertFalse(from_bars(pd.concat([self.bars,self.bars.iloc[[-1]]]),self.calendar,self.now)['available'])

    def test_prioritization_preserves_recency_capacity_and_missing_data_fallback(self):
        pool=[('A',[]),('B',[]),('C',[]),('D',[])]
        self.assertEqual(prioritize(pool,{'D':{'available':True,'rvol':4}},2),[pool[0],pool[3]])
        self.assertEqual(prioritize(pool,{},2),pool[:2])


class VolumeGateTests(unittest.TestCase):
    def test_completed_intraday_structure_is_directional_and_symmetric(self):
        for side,state,slope,ema9,ema20 in [('LONG','bullish','rising',101,100),('SHORT','bearish','falling',99,100)]:
            c=candidate(side)
            c['volume_context'].update(intraday_structure_state=state,vwap_slope=slope,ema9=ema9,ema20=ema20)
            self.assertTrue(confirmation(c,side,app.DEFAULT_AGENT_RULES['strategist'])['qualified'])
            c['volume_context']['intraday_structure_state']='bearish' if side=='LONG' else 'bullish'
            result=confirmation(c,side,app.DEFAULT_AGENT_RULES['strategist'])
            self.assertFalse(result['qualified']);self.assertTrue(result['intraday_confirmation_applied'])

    def test_opening_and_near_close_are_explicit_entry_deferrals(self):
        c=candidate();c['confidence']=.9
        c['volume_context'].update(session_elapsed_minutes=10,session_remaining_minutes=380)
        gate=app.evaluate_strategist_gate(c,'LONG',[],app.DEFAULT_AGENT_RULES['strategist'])
        self.assertFalse(gate['accepted']);self.assertIn('first 15', ' '.join(gate['rejection_reasons']))
        c['volume_context'].update(session_elapsed_minutes=370,session_remaining_minutes=20)
        gate=app.evaluate_strategist_gate(c,'LONG',[],app.DEFAULT_AGENT_RULES['strategist'])
        self.assertFalse(gate['accepted']);self.assertIn('20 session minutes', ' '.join(gate['rejection_reasons']))

    def test_both_directions_admit_borderline_candidate_and_record_baseline(self):
        for side in ['LONG','SHORT']:
            c=candidate(side)
            gate=app.evaluate_strategist_gate(c,side,[],app.DEFAULT_AGENT_RULES['strategist'])
            self.assertTrue(gate['accepted'],gate['rejection_reasons'])
            self.assertAlmostEqual(gate['required_confidence'],.65)
            self.assertTrue(gate['volume_confirmation']['relaxation_used'])
            self.assertFalse(gate['volume_confirmation']['baseline_accepted'])
            note=decision_note(gate['volume_confirmation'])
            self.assertIn(f'Volume confirmation applied for {side}',note)
            self.assertIn('70% to 65%',note)
            self.assertIn('85% to 82.5%',note)
            self.assertIn('enabled strategy approval',note)
            self.assertIn('Risk allocation and limits unchanged',note)

    def test_absent_disabled_stale_wrong_side_stretched_or_fading_cannot_relax(self):
        for side in ['LONG','SHORT']:
            for condition in ['absent','disabled','stale','future','wrong','stretch','fading','low']:
                c=candidate(side);rules=copy.deepcopy(app.DEFAULT_AGENT_RULES['strategist'])
                if condition=='absent':c.pop('volume_context')
                if condition=='disabled':rules['volume_confirmation_enabled']=False
                if condition=='stale':c['volume_context']['through']=(datetime.now(timezone.utc)-timedelta(minutes=16)).isoformat()
                if condition=='future':c['volume_context']['through']=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()
                if condition=='wrong':c['volume_context']['vwap']=110 if side=='LONG' else 90
                if condition=='stretch':c['market_quote'].update(ask=110 if side=='LONG' else 90,bid=110 if side=='LONG' else 90)
                if condition=='fading':c['volume_context']['previous']=110 if side=='LONG' else 90
                if condition=='low':c['volume_context']['rvol']=.9
                gate=app.evaluate_strategist_gate(c,side,[],rules)
                self.assertFalse(gate['accepted'],(side,condition))
                self.assertEqual(decision_note(gate['volume_confirmation']),'')

    def test_floor_and_countertrend_cap_and_reversal_threshold_preserved(self):
        rules,evidence=adjusted_rules(candidate(), 'LONG',app.DEFAULT_AGENT_RULES['strategist'])
        self.assertEqual(rules['fundamental_confirmation_min_confidence'],.6)
        self.assertAlmostEqual(rules['countertrend_long_min_confidence'],.825)
        self.assertEqual(rules['conditional_mean_reversion_min_confidence'],.7)
        for side in ['LONG','SHORT']:
            c=candidate(side,.83,'bearish' if side=='LONG' else 'bullish')
            self.assertTrue(app.evaluate_strategist_gate(c,side,[],app.DEFAULT_AGENT_RULES['strategist'])['accepted'])
            c['confidence']=.82
            self.assertFalse(app.evaluate_strategist_gate(c,side,[],app.DEFAULT_AGENT_RULES['strategist'])['accepted'])


class VolumeRiskTests(unittest.TestCase):
    setUp=fixtures.DatabaseTests.setUp
    def test_risk_agrees_on_countertrend_threshold_and_retains_half_allocation(self):
        for side in ['LONG','SHORT']:
            c=candidate(side,.83,'bearish' if side=='LONG' else 'bullish')
            with patch.object(app,'record_agent_event'):
                risk=app.RiskManagerAgent().review_news_demo_candidate(candidate=c,
                    strategy={'position':side,'leverage':2},account={'available':True,'equity':10000,'cash_available':10000},
                    broker_snapshot={'available':True,'positions':[]},
                    instrument={'resolved':True,'instrument_id':'test','currency':'USD'},atr14=2)
            self.assertEqual(risk['decision'],'accepted',risk['reasons'])
            self.assertEqual(risk['risk_budget_multiplier'],.5)
            self.assertLessEqual(risk.get('proposed_units',0),5)
            c['volume_context']['available']=False
            with patch.object(app,'record_agent_event'):
                risk=app.RiskManagerAgent().review_news_demo_candidate(candidate=c,
                    strategy={'position':side,'leverage':2},account={'available':True,'equity':10000,'cash_available':10000},
                    broker_snapshot={'available':True,'positions':[]},instrument={'resolved':True},atr14=2)
            self.assertEqual(risk['decision'],'rejected')

    def test_submission_refreshes_volume_and_cannot_reuse_saved_confirmation(self):
        import asyncio
        import json
        from unittest.mock import AsyncMock
        for side in ['LONG','SHORT']:
            c=candidate(side)
            c['ticker']='ACME'
            c['related_news']=[{'title':'Company earnings','published_at':None}]
            tid=fixtures.DatabaseTests.trade(self,'bullish' if side=='LONG' else 'bearish')
            conn=app.get_connection()
            details={'candidate_context':c}
            conn.execute("UPDATE trade_events SET details=? WHERE trade_id=? AND stage='strategy'",(json.dumps(details),tid))
            conn.commit();conn.close()
            daily=c['daily_trend_context']
            quote={**c['market_quote'],'fetched_at':datetime.now(timezone.utc).isoformat(),'provider':'eToro'}
            with patch.object(app,'evaluate_latest_signal',return_value=daily),patch.object(app,'fetch_market_quote',AsyncMock(return_value=quote)),patch.object(app,'load_volume_context',return_value={'available':False}) as loader:
                with self.assertRaises(ValueError):
                    asyncio.run(app.place_analysis_candidate_demo_order(tid))
                loader.assert_called_once_with('ACME')
            self.submit.assert_not_awaited()
            with patch.object(app,'evaluate_latest_signal',return_value=daily),patch.object(app,'fetch_market_quote',AsyncMock(return_value=quote)),patch.object(app,'load_volume_context',return_value=c['volume_context']):
                asyncio.run(app.place_analysis_candidate_demo_order(tid))
            self.assertEqual(self.submit.call_args.args[0]['side'],side)
            self.assertLessEqual(self.submit.call_args.args[0]['units'],5)
            fixtures.DatabaseTests.update(self,tid,status='CLOSED')
            self.submit.reset_mock()
