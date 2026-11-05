"""Recent direction, shared gates, and fresh-submission regressions; no broker calls."""
import asyncio
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, AsyncMock
import pandas as pd
import makemoney as app
import test_two_direction_workflow as fixtures
from strategy import evaluate_latest_signal
from recent_trend import analyze_recent_trend
from trading_workflow import select_entry_side, recent_entry_requirements, MANAGED_VERSIONS


def bars(up=False):
    closes=[100+i*.8 if up else 160-i*.8 for i in range(70)]
    f=pd.DataFrame({'Close':closes,'High':[v+1 for v in closes], 'Low':[v-1 for v in closes],
                    'atr14':2.,'sma50':130.,'sma200':100.},index=pd.bdate_range('2026-07-20',periods=70,tz='America/New_York'))
    return f


def candidate(up=False, signal=None, confidence=.75):
    f=bars(up);daily=evaluate_latest_signal('TEST',f);price=float(f.Close.iloc[-1])
    now=datetime.now(timezone.utc);sign=1 if up else -1
    return {'ticker':'TEST','signal':signal or ('bullish' if up else 'bearish'),'confidence':confidence,
            'company_news_sentiment':signal or ('bullish' if up else 'bearish'),
            'ai_analysis':{'reaction_assessment':'structural'}, 'fundamentals':{},
            'price':price,'market_quote':{'available':True,'bid':price,'ask':price},'daily_trend_context':daily,
            'volume_context':{'available':True,'rvol':2,'vwap':price-sign*.5,'last':price,'previous':price-sign*.2,
                'through':now.isoformat(),'session_open':(now-timedelta(hours=1)).isoformat(),
                'session_close':(now+timedelta(hours=1)).isoformat()}}


class RecentCalculationTests(unittest.TestCase):
    def test_direction_and_slope_are_symmetric_despite_sma_ordering(self):
        for up,state in [(True,'bullish'),(False,'bearish')]:
            result=analyze_recent_trend(bars(up))
            self.assertTrue(result['available']);self.assertEqual(result['state'],state)
            self.assertEqual(result['completed_weeks']['state'],state)
            self.assertEqual(result['price_structure']['state'],state)
            self.assertEqual(evaluate_latest_signal('TEST',bars(up))['recent_trend'],result)

    def test_flat_and_conflicting_history_does_not_force_a_direction(self):
        f=bars();f[['Close','High','Low']]=100
        self.assertEqual(analyze_recent_trend(f)['state'],'mixed')
        f=bars(True);f.iloc[-7:,f.columns.get_loc('Close')]=[150,149,148,147,146,145,144]
        result=analyze_recent_trend(f)
        self.assertEqual(result['state'],'mixed')

    def test_invalid_missing_unsorted_and_duplicate_inputs_are_unavailable(self):
        for f in [bars().iloc[:10],bars().iloc[::-1],pd.concat([bars(),bars().iloc[[-1]]])]:
            self.assertFalse(analyze_recent_trend(f)['available'])
        for column,value in [('Close',float('nan')),('Low',-1),('atr14',float('inf'))]:
            f=bars();f.iloc[-1,f.columns.get_loc(column)]=value
            self.assertFalse(analyze_recent_trend(f)['available'])

    def test_weekly_evidence_excludes_unfinished_week(self):
        f=bars().iloc[:-2] # Wednesday/Thursday depending on generated schedule
        result=analyze_recent_trend(f)
        self.assertTrue(result['available'])
        self.assertTrue(all(pd.Timestamp(w['week_ending']).date()<=f.index[-1].date() for w in result['completed_weeks']['closes']))
        # The result is unchanged by appending then excluding future bars upstream.
        cutoff=f.index[-1];extended=bars();self.assertEqual(result,analyze_recent_trend(extended.loc[extended.index<=cutoff]))

    def test_meta_entry_bearish_even_while_saved_sma_ordering_is_mixed(self):
        raw=json.loads((Path(__file__).parent/'fixtures/meta_pre_entry_20261023.json').read_text())
        f=pd.DataFrame(raw['rows']);f.index=pd.to_datetime(f.pop('timestamp'),utc=True).dt.tz_convert('America/New_York')
        f['atr14']=25.283564976283483;f['sma50']=663.5766625976562;f['sma200']=631.6591571044922
        daily=evaluate_latest_signal('META',f)
        self.assertEqual(daily['trend_state'],'mixed')
        self.assertEqual(daily['recent_trend']['state'],'bearish')
        self.assertAlmostEqual(daily['recent_trend']['return_7_sessions_pct'],-12.8698085,places=5)
        self.assertAlmostEqual(daily['recent_trend']['return_14_sessions_pct'],-13.3818609,places=5)
        c=candidate();c.update(signal='bullish',company_news_sentiment='bullish',daily_trend_context=daily,price=641.11,
            market_quote={'bid':641.,'ask':641.11},volume_context={'available':False})
        self.assertEqual(select_entry_side(c,app.DEFAULT_AGENT_RULES['strategist']),'SHORT')
        gate=app.evaluate_strategist_gate(c,'SHORT',[],app.DEFAULT_AGENT_RULES['strategist'])
        self.assertFalse(gate['accepted']);self.assertIn('RVOL/VWAP',' '.join(gate['rejection_reasons']))
        self.assertFalse(app.evaluate_strategist_gate(c,'LONG',[],app.DEFAULT_AGENT_RULES['strategist'])['accepted'])


class RecentGateTests(unittest.TestCase):
    def test_trend_leads_both_directions_despite_opposite_news_and_fundamentals(self):
        for up,signal,side in [(False,'bullish','SHORT'),(True,'bearish','LONG')]:
            c=candidate(up,signal)
            c['fundamentals']={'revenue_yoy_growth_pct':25 if not up else -10,'net_profit_margin_pct':35 if not up else -5,
                               'free_cash_flow_ttm':100 if not up else -100,'debt_to_equity_ratio':.2 if not up else 3,'trailing_eps':10 if not up else -10}
            self.assertEqual(select_entry_side(c,app.DEFAULT_AGENT_RULES['strategist']),side)
            gate=app.evaluate_strategist_gate(c,side,[],app.DEFAULT_AGENT_RULES['strategist'])
            self.assertTrue(gate['accepted'],gate['rejection_reasons'])
            self.assertAlmostEqual(gate['required_confidence'],.65)
            self.assertFalse(gate['volume_confirmation']['relaxation_used']) # entry volume is not confidence relief
            self.assertIn('Recent price trend:',gate['decision_message'])
            self.assertEqual(gate['fundamental_evidence_role'],'context_only')
            self.assertEqual(gate['strategy_framework'],'RECENT_TREND_CONTINUATION')
            self.assertEqual(set(gate['volume_confirmation']['thresholds']),{'min_confidence'})
            self.assertTrue(gate['recent_trend_decision']['entry_confirmation']['qualified'])
            c['volume_context']['available']=False
            self.assertFalse(app.evaluate_strategist_gate(c,side,[],app.DEFAULT_AGENT_RULES['strategist'])['accepted'])

    def test_company_quality_does_not_lower_threshold_in_recent_direction_path(self):
        c=candidate(False,confidence=.67);c['volume_context']['available']=False
        c['fundamentals']={'revenue_yoy_growth_pct':-5,'net_profit_margin_pct':-5,'free_cash_flow_ttm':-100,'debt_to_equity_ratio':3,'trailing_eps':-5}
        gate=app.evaluate_strategist_gate(c,'SHORT',[],app.DEFAULT_AGENT_RULES['strategist'])
        self.assertAlmostEqual(gate['required_confidence'],.7);self.assertFalse(gate['accepted'])

    def test_countertrend_high_confidence_alone_cannot_bypass_measured_reversal(self):
        for up,side in [(False,'LONG'),(True,'SHORT')]:
            c=candidate(up,confidence=.99)
            self.assertFalse(app.evaluate_strategist_gate(c,side,[],app.DEFAULT_AGENT_RULES['strategist'])['accepted'])
            c['ai_analysis']['reaction_assessment']='temporary'
            sign=1 if side=='LONG' else -1;price=c['price']
            c['news_reaction']={'available':True,'bars':3,'anchor':price+sign*5,'low':price-2,'high':price+2,'last':price,'previous':price-sign*.5}
            self.assertEqual(select_entry_side(c,app.DEFAULT_AGENT_RULES['strategist']),side)
            self.assertTrue(app.evaluate_strategist_gate(c,side,[],app.DEFAULT_AGENT_RULES['strategist'])['accepted'])
            c['confidence']=.84
            self.assertFalse(app.evaluate_strategist_gate(c,side,[],app.DEFAULT_AGENT_RULES['strategist'])['accepted'])

    def test_disabled_missing_mixed_and_neutral_paths_are_explicit(self):
        c=candidate(False,'bullish');rules={**app.DEFAULT_AGENT_RULES['strategist'],'recent_trend_enabled':False}
        self.assertEqual(select_entry_side(c,rules),'LONG')
        for evidence in [{'available':False},{'available':True,'state':'mixed'}]:
            c['daily_trend_context']['recent_trend']=evidence
            self.assertEqual(select_entry_side(c,app.DEFAULT_AGENT_RULES['strategist']),'LONG')
        c=candidate();c['signal']='neutral'
        self.assertEqual(select_entry_side(c,app.DEFAULT_AGENT_RULES['strategist']),'HOLD')


class RecentWorkflowTests(unittest.TestCase):
    setUp=fixtures.DatabaseTests.setUp
    trade=fixtures.DatabaseTests.trade
    row=fixtures.DatabaseTests.row
    update=fixtures.DatabaseTests.update

    def test_shared_risk_guard_preserves_half_allocation_and_rejects_timing_failure(self):
        for up,side in [(False,'SHORT'),(True,'LONG')]:
            c=candidate(up,'bullish' if not up else 'bearish')
            c['daily_trend_context']['trend_state']='mixed'
            args=dict(candidate=c,strategy={'position':side,'leverage':2},account={'available':True,'equity':10000,'cash_available':10000},broker_snapshot={'available':True,'positions':[]},instrument=self.adapter.resolve_instrument.return_value,atr14=2)
            with patch.object(app,'record_agent_event'):
                risk=app.RiskManagerAgent().review_news_demo_candidate(**args)
                self.assertEqual(risk['decision'],'accepted',risk['reasons']);self.assertEqual(risk['risk_budget_multiplier'],.5)
                c['volume_context']['available']=False
                self.assertEqual(app.RiskManagerAgent().review_news_demo_candidate(**args)['decision'],'rejected')

    def test_final_submission_reloads_recent_direction_and_does_not_reuse_approval(self):
        for up,side in [(False,'SHORT'),(True,'LONG')]:
            c=candidate(up,'bullish' if not up else 'bearish');c['ticker']='ACME'
            tid=self.trade(c['signal']);conn=app.get_connection()
            # Saved research still has the opposite direction; current daily bars own the entry bias.
            saved={**c,'related_news':[{'title':'Company earnings','published_at':None}]}
            conn.execute("UPDATE trade_events SET details=? WHERE trade_id=? AND stage='strategy'",(json.dumps({'candidate_context':saved}),tid));conn.commit();conn.close()
            quote={**c['market_quote'],'provider':'eToro','fetched_at':datetime.now(timezone.utc).isoformat()}
            with patch.object(app,'load_daily_bars',return_value=bars(up)),patch.object(app,'fetch_market_quote',AsyncMock(return_value=quote)),patch.object(app,'load_volume_context',return_value={'available':False}):
                with self.assertRaisesRegex(ValueError,'Fresh Strategist'):
                    asyncio.run(app.place_analysis_candidate_demo_order(tid))
            self.submit.assert_not_awaited()
            with patch.object(app,'load_daily_bars',return_value=bars(up)),patch.object(app,'fetch_market_quote',AsyncMock(return_value=quote)),patch.object(app,'load_volume_context',return_value=c['volume_context']):
                asyncio.run(app.place_analysis_candidate_demo_order(tid))
            self.assertEqual(self.submit.call_args.args[0]['side'],side)
            self.assertLessEqual(self.submit.call_args.args[0]['units'],5)
            self.assertEqual(json.loads(self.row(tid)['policy_json'])['framework'],'RECENT_TREND_CONTINUATION')
            self.update(tid,status='CLOSED');self.submit.reset_mock()
        self.assertTrue({'news_reaction_v2','news_reaction_v3','news_reaction_v4'}.issubset(MANAGED_VERSIONS))

class AutoConfirmationTests(unittest.TestCase):
    def test_auto_confirm_both_directions_requires_measurements_and_can_be_disabled(self):
        for up,signal,side in [(False,'bullish','SHORT'),(True,'bearish','LONG')]:
            c=candidate(up,signal)
            rules=copy.deepcopy(app.DEFAULT_AGENT_RULES['strategist'])
            gate=app.evaluate_strategist_gate(c,side,[],rules)
            self.assertTrue(gate['accepted'])
            self.assertEqual(gate['recent_trend_decision']['confirmation_status'],'confirmed')
            self.assertIn('auto-confirm ON',gate['decision_message'])
            self.assertIn('passed automatically',gate['decision_message'])
            rules['trend_auto_confirm_enabled']=False
            gate=app.evaluate_strategist_gate(c,side,[],rules)
            self.assertFalse(gate['accepted'])
            self.assertEqual(gate['recent_trend_decision']['confirmation_status'],'automatic_confirmation_disabled')
            self.assertIn('auto-confirm OFF',gate['decision_message'])
            rules['trend_auto_confirm_enabled']=True;c['volume_context']['available']=False
            gate=app.evaluate_strategist_gate(c,side,[],rules)
            self.assertFalse(gate['accepted'])
            self.assertEqual(gate['recent_trend_decision']['confirmation_status'],'awaiting_measurement')
            self.assertIn('awaiting measured',gate['decision_message'])

    def test_off_does_not_disable_ordinary_trend_aligned_news_entries(self):
        for up,side in [(False,'SHORT'),(True,'LONG')]:
            c=candidate(up)
            gate=app.evaluate_strategist_gate(c,side,[],{**app.DEFAULT_AGENT_RULES['strategist'],'trend_auto_confirm_enabled':False})
            self.assertTrue(gate['accepted'],gate['rejection_reasons'])
            self.assertEqual(gate['recent_trend_decision']['confirmation_status'],'not_required')

    def test_off_also_holds_otherwise_confirmed_countertrend_reversal(self):
        c=candidate(False,confidence=.99);price=c['price'];c['ai_analysis']['reaction_assessment']='temporary'
        c['news_reaction']={'available':True,'bars':3,'anchor':price+5,'low':price-2,'high':price+2,'last':price,'previous':price-.5}
        rules={**app.DEFAULT_AGENT_RULES['strategist'],'trend_auto_confirm_enabled':False}
        gate=app.evaluate_strategist_gate(c,'LONG',[],rules)
        self.assertTrue(gate['entry_setup']['approved']);self.assertFalse(gate['accepted'])
        self.assertIn('auto-confirm is OFF',' '.join(gate['rejection_reasons']))


class AutoConfirmationWorkflowTests(unittest.TestCase):
    setUp=fixtures.DatabaseTests.setUp
    trade=fixtures.DatabaseTests.trade
    row=fixtures.DatabaseTests.row
    update=fixtures.DatabaseTests.update

    def test_final_submission_and_risk_respect_off_and_persist_setting(self):
        c=candidate(False,'bullish');c['ticker']='ACME';tid=self.trade('bullish')
        conn=app.get_connection()
        saved={**c,'related_news':[{'title':'Company earnings','published_at':None}]}
        conn.execute("UPDATE trade_events SET details=? WHERE trade_id=? AND stage='strategy'",(json.dumps({'candidate_context':saved}),tid));conn.commit();conn.close()
        app.update_agent_rules('strategist',{'trend_auto_confirm_enabled':False})
        conn=app.get_connection();persisted=json.loads(conn.execute("SELECT rules_json FROM agent_rules WHERE agent_name='strategist'").fetchone()[0]);conn.close()
        self.assertFalse(persisted['trend_auto_confirm_enabled'])
        self.rules['strategist']['trend_auto_confirm_enabled']=False
        with self.assertRaisesRegex(ValueError,'true or false'):
            app.update_agent_rules('strategist',{'trend_auto_confirm_enabled':'yes'})
        quote={**c['market_quote'],'provider':'eToro','fetched_at':datetime.now(timezone.utc).isoformat()}
        with patch.object(app,'load_daily_bars',return_value=bars(False)),patch.object(app,'fetch_market_quote',AsyncMock(return_value=quote)),patch.object(app,'load_volume_context',return_value=c['volume_context']):
            with self.assertRaisesRegex(ValueError,'Fresh Strategist'):
                asyncio.run(app.place_analysis_candidate_demo_order(tid))
        self.submit.assert_not_awaited()
        with patch.object(app,'record_agent_event'):
            risk=app.RiskManagerAgent().review_news_demo_candidate(candidate=c,strategy={'position':'SHORT','leverage':2},account={'available':True,'equity':10000,'cash_available':10000},broker_snapshot={'available':True,'positions':[]},instrument=self.adapter.resolve_instrument.return_value,atr14=2)
        self.assertEqual(risk['decision'],'rejected');self.assertIn('auto-confirm OFF',risk['decision_message'])
