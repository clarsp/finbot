"""Offline regressions: real gates/storage, isolated DB, mocked provider boundaries."""
import asyncio
import copy
import json
import os
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Mocked broker submissions must never contact a real notification service.
os.environ["FINBOT_DISABLE_EXTERNAL_NOTIFICATIONS"] = "true"

import pandas as pd
import makemoney as app
import trading_workflow as workflow
import workflow_store as store
from integrations import EtoroBrokerAdapter


class SizingTests(unittest.TestCase):
    def size(self, **overrides):
        args = dict(side='LONG', entry=100, atr=2, equity=10000, cash=10000,
                    leverage=2, positions=[], risk_rules=app.DEFAULT_AGENT_RULES['risk_manager'],
                    execution_rules=app.DEFAULT_AGENT_RULES['execution'])
        args.update(overrides)
        return workflow.size_order(**args)

    def test_both_sides_have_realistic_opposite_protection(self):
        for side, stop, target in [('LONG',96,106),('SHORT',104,94)]:
            result = self.size(side=side)
            self.assertEqual(result['proposed_stop'],stop)
            self.assertEqual(result['proposed_take_profit'],target)
            self.assertFalse(result['execution_reasons'])
            self.assertLessEqual(result['proposed_units'],5)
            self.assertLessEqual(result['estimated_total_spend'],200)

    def test_unit_and_spend_caps_both_apply(self):
        self.assertEqual(self.size(entry=10,atr=.1)['proposed_units'],5)
        self.assertLess(self.size(entry=611,atr=20)['proposed_units'],400/611)
        self.assertLessEqual(self.size(cash=20)['estimated_total_spend'],20)

    def test_portfolio_risk_uses_current_marks_and_direction(self):
        position=dict(position_id='p', units=10,current_rate=110,open_rate=100,stop_loss_price=100,side='LONG')
        self.assertEqual(self.size(positions=[position])['open_stop_risk'],100)
        position.update(current_rate=90,stop_loss_price=100,side='SHORT')
        self.assertEqual(self.size(positions=[position])['open_stop_risk'],100)
        position['stop_loss_price']=80
        self.assertEqual(self.size(positions=[position])['open_stop_risk'],0)

    def test_unknown_risk_blocks_even_with_positive_units(self):
        result=self.size(positions=[dict(position_id='p',units=1,open_rate=100)])
        self.assertGreater(result['proposed_units'],0)
        self.assertTrue(result['execution_reasons'])

    def test_aggregate_exposure_and_risk_exhaustion(self):
        p=dict(units=100,current_rate=100,stop_loss_price=99,side='LONG')
        self.assertEqual(self.size(positions=[p])['proposed_units'],0)
        p.update(units=10,stop_loss_price=50)
        self.assertEqual(self.size(positions=[p])['risk_budget'],0)

    def test_concentration_and_gap_scenario_constrain_size(self):
        rules={**app.DEFAULT_AGENT_RULES['risk_manager'],'max_single_position_exposure_pct':.001}
        result=self.size(risk_rules=rules)
        self.assertLessEqual(result['notional'],10)
        rules.update(max_single_position_exposure_pct=.1,max_stressed_loss_pct=.0001)
        result=self.size(risk_rules=rules)
        self.assertLessEqual(result['stressed_loss'],1)

    def test_sector_gate_is_optional_but_never_guesses_missing_sectors(self):
        rules={**app.DEFAULT_AGENT_RULES['risk_manager'],'sector_exposure_enabled':True,'max_sector_exposure_pct':.001}
        self.assertTrue(self.size(risk_rules=rules)['execution_reasons'])
        self.assertLessEqual(self.size(risk_rules=rules,sector='Technology')['notional'],10)

    def test_invalid_numbers_do_not_authorize_orders(self):
        for field,value in [('entry',float('nan')),('atr',0),('equity',float('inf')),('leverage',2.5),('cash',float('inf'))]:
            with self.subTest(field=field),self.assertRaises((ValueError,TypeError)):
                self.size(**{field:value})

    def test_exit_rules_for_both_sides_and_time(self):
        now=datetime.now(timezone.utc)
        for side,stop,target in [('LONG',96,106),('SHORT',104,94)]:
            trade=dict(side=side,stop_loss_price=stop,take_profit_price=target,created_at=now.isoformat(),policy={'holding_hours':2})
            self.assertEqual(workflow.exit_reason(trade,stop,now),'stop_level_observed')
            self.assertEqual(workflow.exit_reason(trade,target,now),'take_profit_level_observed')
            self.assertIsNone(workflow.exit_reason(trade,100,now))
            self.assertEqual(workflow.exit_reason(trade,100,now+timedelta(hours=3)),'thesis_time_expired')

    def test_mean_reversion_needs_temporary_confident_turning_research(self):
        candidate={'price':95,'confidence':.9,'ai_analysis':{'reaction_assessment':'temporary'},
                   'daily_trend_context':{'atr14':10},'news_reaction':{'available':True,'anchor':110,'low':90,'high':110,'last':95,'previous':94}}
        rules=app.DEFAULT_AGENT_RULES['strategist']
        self.assertTrue(workflow.mean_reversion_setup(candidate,'LONG',rules)['approved'])
        for changed in [{'confidence':.2},{'ai_analysis':{'reaction_assessment':'structural'}},{'news_reaction':{**candidate['news_reaction'],'previous':96}}]:
            self.assertFalse(workflow.mean_reversion_setup({**candidate,**changed},'LONG',rules)['approved'])

    def test_daily_bars_observe_actual_session_end_including_early_close(self):
        import strategy
        index=pd.date_range('2025-12-01',periods=318,freq='D',tz='America/New_York')
        bars=pd.DataFrame({'Open':100,'High':102,'Low':98,'Close':100},index=index)
        # A metadata-defined early close must be honored without assuming 16:00.
        end=pd.Timestamp('2026-10-14T17:00Z')
        ticker=MagicMock();ticker.history.return_value=bars
        ticker.get_history_metadata.return_value={'currentTradingPeriod':{'regular':{'end':int(end.timestamp())}}}
        with patch.object(strategy.yf,'Ticker',return_value=ticker):
            before=strategy.load_daily_bars('ACME',now='2026-10-14T16:59Z')
            after=strategy.load_daily_bars('ACME',now='2026-10-14T17:01Z')
        self.assertEqual(before.index[-1].date().isoformat(),'2026-10-13')
        self.assertEqual(after.index[-1].date().isoformat(),'2026-10-14')

    def test_daily_bars_conservatively_exclude_today_without_session_metadata(self):
        import strategy
        ticker=MagicMock()
        ticker.history.return_value=pd.DataFrame({'Open':100,'High':102,'Low':98,'Close':100},index=pd.date_range('2025-12-01',periods=318,freq='D',tz='America/New_York'))
        ticker.get_history_metadata.return_value={}
        with patch.object(strategy.yf,'Ticker',return_value=ticker):
            result=strategy.load_daily_bars('ACME',now='2026-10-14T22:00Z')
        self.assertEqual(result.index[-1].date().isoformat(),'2026-10-13')

    def test_reaction_uses_only_completed_bars(self):
        bars=pd.DataFrame({'Close':[100,99,91,93,999],'Low':[99,98,90,92,0], 'High':[101,100,92,94,999]},
                          index=pd.date_range('2026-10-14T12:00Z',periods=5,freq='5min'))
        result=workflow.reaction_from_bars(bars,'2026-10-14T12:10Z',datetime(2026,10,14,12,21,tzinfo=timezone.utc))
        self.assertEqual(result['anchor'],99)
        self.assertEqual(result['last'],93)
        self.assertEqual(result['bars'],2)
        self.assertEqual(result['low'],90)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(self.temp.cleanup)
        self.stack.enter_context(patch.object(app,'DB_PATH',Path(self.temp.name)/'finbot.db'))
        self.stack.enter_context(patch.dict(os.environ,{'FINBOT_PASSWORD':'test-only-password','FINBOT_USERNAME':'admin'}))
        app.ensure_db()
        # Entry-path fixtures explicitly allow weekends; scheduler tests cover the default restriction.
        app.configure_research_schedule('manual',60,weekend_trading_enabled=True)
        self.rules=copy.deepcopy(app.DEFAULT_AGENT_RULES)
        self.stack.enter_context(patch.object(app,'get_agent_rules',return_value=self.rules))
        self.settings=types.SimpleNamespace(broker_provider='etoro',etoro_demo_trading=True,openai_api_key='')
        self.stack.enter_context(patch.object(app.IntegrationSettings,'from_environment',return_value=self.settings))
        self.adapter=MagicMock()
        self.adapter.close_position.return_value={"order_id":"close-1"}
        self.adapter.modify_position.return_value={"operationId":"patch-1"}
        self.adapter.get_account_snapshot.return_value={'available':True,'equity':10000,'cash_available':10000}
        self.adapter.get_positions.return_value={'available':True,'positions':[]}
        self.adapter.resolve_instrument.return_value={'instrument_id':'1','currency':'USD','tradeable':True,'supported_asset':True,'buy_enabled':True,'short_enabled':True}
        self.adapter.estimate_order_costs.return_value={}
        self.adapter.get_closed_positions.return_value={'available':True,'positions':[]}
        self.stack.enter_context(patch.object(app,'EtoroBrokerAdapter',return_value=self.adapter))
        self.stack.enter_context(patch.object(app,'load_daily_bars',return_value=pd.DataFrame([{'Close':100,'sma50':95,'sma200':90,'atr14':2}],index=pd.to_datetime(['2026-10-13'],utc=True))))
        self.stack.enter_context(patch.object(app,'load_news_reaction',return_value={'available':False}))
        self.stack.enter_context(patch.object(app,'fetch_company_fundamentals',AsyncMock(return_value={'company_name':'Acme','data_status':'available'})))
        self.stack.enter_context(patch.object(app,'fetch_market_quote',AsyncMock(side_effect=lambda _:dict(available=True,provider='eToro',bid=99.9,ask=100,fetched_at=datetime.now(timezone.utc).isoformat()))))
        self.submit=self.stack.enter_context(patch.object(app,'submit_broker_order',AsyncMock(return_value={'order_id':'o','position_id':'p','status':'submitted'})))

    def trade(self,signal='bullish',status='ANALYSIS_ONLY'):
        candidate=dict(ticker='ACME',company_name='Acme',signal=signal,company_news_sentiment=signal,confidence=.95,
                       fundamentals={},ai_analysis={'reaction_assessment':'structural'},related_news=[{'title':'Company earnings','published_at':None}])
        tid=app.save_trade('ACME','LONG' if signal=='bullish' else 'SHORT',2,1,status)
        app.record_trade_event(tid,'research','researcher','completed','accepted','Research passed',{'signal':signal,'confidence':.95})
        app.record_trade_event(tid,'strategy','strategist','completed','accepted','Strategy passed',{'candidate_context':candidate})
        return tid

    def row(self,tid):
        c=app.get_connection();r=dict(c.execute('SELECT * FROM trades WHERE id=?',(tid,)).fetchone());c.close();return r

    def update(self,tid,**fields):
        c=app.get_connection();c.execute('UPDATE trades SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),tid));c.commit();c.close()

    def test_actual_final_submission_handles_long_and_short(self):
        for sentiment,side in [('bullish','LONG'),('bearish','SHORT')]:
            with self.subTest(side=side):
                tid=self.trade(sentiment)
                asyncio.run(app.place_analysis_candidate_demo_order(tid))
                payload=self.submit.call_args.args[0]
                self.assertEqual(payload['side'],side)
                self.assertIsInstance(payload['leverage'],int)
                self.assertLessEqual(payload['units'],5)
                price=100 if side=='LONG' else 99.9
                self.assertLessEqual(payload['units']*price/payload['leverage'],200)
                self.assertEqual(self.row(tid)['order_reference'],payload['reference_id'])
                self.assertEqual(self.row(tid)['status'],'SUBMITTED_PENDING_RECONCILIATION')
                self.update(tid,status='CLOSED')

    def test_mean_reversion_both_directions_reach_shared_order_boundary(self):
        for sentiment,side,price,anchor,previous in [('bearish','LONG',95,110,94),('bullish','SHORT',105,90,106)]:
            tid=self.trade(sentiment)
            c=app.get_connection();event=c.execute("SELECT details FROM trade_events WHERE trade_id=? AND stage='strategy'",(tid,)).fetchone()
            details=json.loads(event['details']);details['candidate_context']['ai_analysis']={'reaction_assessment':'temporary'}
            c.execute("UPDATE trade_events SET details=? WHERE trade_id=? AND stage='strategy'",(json.dumps(details),tid));c.commit();c.close()
            bullish=side=='LONG'
            bars=pd.DataFrame([dict(Close=100,sma50=90 if bullish else 110,sma200=80 if bullish else 120,atr14=10)],index=pd.to_datetime(['2026-10-13'],utc=True))
            reaction=dict(available=True,anchor=anchor,low=90,high=110,last=price,previous=previous)
            quote=dict(available=True,provider='eToro',ask=price if bullish else price+.1,bid=price-.1 if bullish else price,fetched_at=datetime.now(timezone.utc).isoformat())
            with patch.object(app,'load_daily_bars',return_value=bars),patch.object(app,'load_news_reaction',return_value=reaction),patch.object(app,'fetch_market_quote',AsyncMock(return_value=quote)):
                asyncio.run(app.place_analysis_candidate_demo_order(tid))
            payload=self.submit.call_args.args[0]
            self.assertEqual(payload['side'],side);self.assertEqual(payload['take_profit_price'],anchor)
            self.assertEqual(json.loads(self.row(tid)['policy_json'])['framework'],'CONDITIONAL_MEAN_REVERSION')
            self.update(tid,status='CLOSED')

    def test_fresh_execution_rejection_cannot_be_bypassed(self):
        tid=self.trade()
        bad={'decision':'accepted','execution_ready':False,'execution_reasons':['Unverifiable existing position risk'], 'proposed_units':1,'proposed_stop':96}
        with patch.object(app.RiskManagerAgent,'review_news_demo_candidate',return_value=bad):
            with self.assertRaisesRegex(ValueError,'Fresh Risk Manager'):
                asyncio.run(app.place_analysis_candidate_demo_order(tid))
        self.submit.assert_not_awaited()

    def test_full_portfolio_and_duplicate_symbol_block(self):
        for positions in [[{'symbol':'ACME'}],[dict(position_id=str(i),symbol=f'P{i}',units=1,current_rate=10,stop_loss_price=9,side='LONG') for i in range(30)]]:
            self.adapter.get_positions.return_value={'available':True,'positions':positions}
            with self.assertRaises(ValueError):
                asyncio.run(app.place_analysis_candidate_demo_order(self.trade()))
        self.submit.assert_not_awaited()

    def test_rejected_research_or_expired_candidate_never_submits(self):
        tid=self.trade()
        c=app.get_connection();c.execute("UPDATE trade_events SET decision='rejected' WHERE trade_id=? AND stage='research'",(tid,));c.commit();c.close()
        with self.assertRaises(ValueError):asyncio.run(app.place_analysis_candidate_demo_order(tid))
        tid=self.trade();self.update(tid,created_at='2020-01-01 00:00:00')
        with self.assertRaisesRegex(ValueError,'expired'):asyncio.run(app.place_analysis_candidate_demo_order(tid))
        self.submit.assert_not_awaited()

    def test_timeout_keeps_reference_and_blocks_blind_resubmission(self):
        tid=self.trade();self.submit.side_effect=ValueError('HTTP 408 request timed out')
        with self.assertRaises(ValueError):asyncio.run(app.place_analysis_candidate_demo_order(tid))
        row=self.row(tid)
        self.assertEqual(row['status'],'RECONCILIATION_REQUIRED');self.assertTrue(row['order_reference'])
        with self.assertRaises(ValueError):asyncio.run(app.place_analysis_candidate_demo_order(tid))
        self.assertEqual(self.submit.await_count,1)

    def test_stale_quote_never_submits(self):
        with patch.object(app,'fetch_market_quote',AsyncMock(return_value=dict(available=True,provider='eToro',ask=100,bid=99,fetched_at='2020-01-01T00:00Z'))):
            with self.assertRaisesRegex(ValueError,'fresh eToro'):asyncio.run(app.place_analysis_candidate_demo_order(self.trade()))
        self.submit.assert_not_awaited()

    def test_definite_rejection_releases_reserved_capital(self):
        tid=self.trade();self.submit.side_effect=ValueError('HTTP 400 invalid order')
        with self.assertRaises(ValueError):asyncio.run(app.place_analysis_candidate_demo_order(tid))
        self.assertEqual(self.row(tid)['status'],'REJECTED_EXECUTION')
        self.assertEqual(self.row(tid)['allocated_capital'],0)

    def test_broker_costs_fit_inside_cap_and_match_lifecycle_units(self):
        tid=self.trade()
        self.adapter.estimate_order_costs.return_value={'costs':[{'currency':'USD','costType':'transactionFee','value':2}]}
        asyncio.run(app.place_analysis_candidate_demo_order(tid))
        row=self.row(tid);policy=json.loads(row['policy_json'])
        self.assertLessEqual(row['allocated_capital']+2,200)
        self.assertEqual(policy['sizing']['units'],row['quantity'])
        self.assertEqual(policy['sizing']['binding_limit'],'broker_opening_costs')

    def test_archive_preserves_submitted_uncertain_and_fresh_and_restores(self):
        old=self.trade();fresh=self.trade();broker=self.trade();uncertain=self.trade()
        for tid in [old,broker,uncertain]:self.update(tid,created_at='2020-01-01 00:00:00')
        self.update(broker,broker_order_id='order')
        app.record_trade_event(uncertain,'execution','execution','completed','uncertain','Timeout',{})
        c=app.get_connection();before=[dict(r) for r in c.execute('SELECT * FROM trade_events WHERE trade_id=?',(old,))]
        archived=store.archive_analysis(c,3)
        self.assertEqual(archived,[old]);self.assertEqual(c.execute('SELECT COUNT(*) FROM trades').fetchone()[0],3)
        store.restore_analysis(c,old)
        self.assertEqual([dict(r) for r in c.execute('SELECT * FROM trade_events WHERE trade_id=?',(old,))],before)
        c.close();self.assertEqual(self.row(old)['created_at'],'2020-01-01 00:00:00')

    def test_account_lease_excludes_concurrent_orders_and_releases_on_failure(self):
        async def check():
            with self.assertRaisesRegex(RuntimeError,'test error'):
                async with store.lease(app.get_connection,'test'):
                    with self.assertRaises(store.AccountBusy):
                        async with store.lease(app.get_connection,'test'):pass
                    raise RuntimeError('test error')
            async with store.lease(app.get_connection,'test'):pass
        asyncio.run(check())

    def test_broker_history_records_net_outcome_and_fees(self):
        tid=self.trade();self.update(tid,status='OPEN',broker_position_id='p',broker_order_id='o')
        self.adapter.get_closed_positions.return_value={'available':True,'positions':[dict(position_id='p',closed_at='2026-10-14T10:00Z',close_rate=105,net_profit=7.3,fees=.7)]}
        asyncio.run(app.manage_etoro_demo_positions(self.settings))
        self.assertEqual(self.row(tid)['status'],'CLOSED')
        self.assertEqual(self.row(tid)['realized_pnl'],7.3)
        c=app.get_connection();p=store.broker_performance(c);c.close()
        self.assertEqual(p['reported_closed_net_profit'],7.3);self.assertTrue(p['fees_complete'])
        self.assertEqual(p['status'],'inconclusive')

    def test_pending_close_is_not_reopened_or_repeated(self):
        tid=self.trade();self.update(tid,status='EXIT_PENDING',broker_position_id='p',broker_order_id='o',stop_loss_price=96,take_profit_price=106)
        self.adapter.get_positions.return_value={'available':True,'positions':[dict(position_id='p',instrument_id='1',units=1,open_rate=100,current_rate=100,stop_loss_price=96,take_profit_price=106)]}
        asyncio.run(app.manage_etoro_demo_positions(self.settings))
        self.assertEqual(self.row(tid)['status'],'EXIT_PENDING');self.adapter.close_position.assert_not_called()

    def test_position_manager_closes_both_sides_only_after_broker_confirmation(self):
        for side,mark,stop,target in [('LONG',107,96,106),('SHORT',93,104,94)]:
            tid=self.trade('bullish' if side=='LONG' else 'bearish')
            policy={'fill_adjusted':True,'holding_hours':48}
            position_id='p-'+side.lower()
            self.update(tid,status='OPEN',side=side,broker_position_id=position_id,broker_order_id='o',strategy_version=workflow.VERSION,
                        policy_json=json.dumps(policy),config_json=json.dumps(self.rules),stop_loss_price=stop,take_profit_price=target)
            self.adapter.get_positions.return_value={'available':True,'positions':[dict(position_id=position_id,instrument_id='1',side=side,
                units=1,amount=50,open_rate=100,current_rate=mark,stop_loss_price=stop,take_profit_price=target)]}
            self.adapter._get_rate.return_value={'bid':mark,'ask':mark}
            asyncio.run(app.manage_etoro_demo_positions(self.settings))
            self.assertEqual(self.row(tid)['status'],'EXIT_PENDING')
            self.assertIsNone(self.row(tid)['closed_at'])
            self.update(tid,status='CLOSED')
        self.assertEqual(self.adapter.close_position.call_count,2)

    def test_protection_patch_ack_does_not_claim_confirmed(self):
        tid=self.trade();self.update(tid,status='OPEN',broker_position_id='p',stop_loss_price=96,take_profit_price=106)
        self.adapter.get_positions.return_value={'available':True,'positions':[dict(position_id='p',instrument_id='1',units=1,
            amount=50,open_rate=100,current_rate=100,stop_loss_price=96,take_profit_price=600)]}
        self.adapter.modify_position.return_value={'accepted':True}
        asyncio.run(app.manage_etoro_demo_positions(self.settings))
        self.adapter.modify_position.assert_called_once()
        self.assertEqual(self.adapter.modify_position.call_args.args,('p',96.0,106.0))
        self.assertTrue(self.adapter.modify_position.call_args.kwargs['request_id'])
        self.assertEqual(self.row(tid)['status'],'RECONCILIATION_REQUIRED')

    def test_unknown_order_resolves_by_persisted_reference_without_resubmission(self):
        tid=self.trade();self.update(tid,status='SUBMITTING',order_reference='ref')
        self.adapter.lookup_order.return_value={'status':{'name':'Rejected'}}
        asyncio.run(app.manage_etoro_demo_positions(self.settings))
        self.adapter.lookup_order.assert_called_once_with(order_id=None,reference_id='ref')
        self.assertEqual(self.row(tid)['status'],'REJECTED_EXECUTION');self.submit.assert_not_awaited()

    def test_reconciliation_keeps_broker_observations_distinct_from_forecasts(self):
        tid=self.trade()
        self.update(tid,status='OPEN',broker_position_id='p',broker_order_id='o',
                    stop_loss_price=96,take_profit_price=106,
                    policy_json=json.dumps({'estimated_costs':2,'fill_adjusted':True}))
        position=dict(position_id='p',instrument_id='1',side='LONG',units=1,
                      amount=50,open_rate=100,current_rate=102,stop_loss_price=96,
                      take_profit_price=106,unrealized_pnl=1.8,leverage=2,fees=.2)
        app.reconcile_news_demo_positions([position])
        policy=json.loads(self.row(tid)['policy_json'])
        self.assertEqual(policy['estimated_costs'],2)
        self.assertEqual(policy['broker_observation']['reported_running_pnl'],1.8)
        self.assertEqual(policy['broker_observation']['margin'],50)
        position['current_rate']=None
        app.reconcile_news_demo_positions([position])
        self.assertIsNone(json.loads(self.row(tid)['policy_json'])['broker_observation']['current_price'])
        self.assertEqual(self.row(tid)['current_price'],102)

    def test_sample_replay_uses_shared_gates_lifecycle_and_smaller_stake(self):
        fixture=dict(symbol='ACME',signal='bullish',confidence=.95,notes='Positive earnings',timestamp='2026-10-10T12:00Z',source='fixture')
        with patch.object(app,'get_sample_research_inputs',return_value=[fixture]):
            result=asyncio.run(app.run_demo_test_workflow('ACME',10))
        self.assertEqual(result['status'],'SUBMITTED_PENDING_RECONCILIATION')
        row=self.row(result['trade_id'])
        self.assertLessEqual(row['allocated_capital'],10)
        self.assertTrue(row['order_reference'])
        self.assertEqual(json.loads(row['policy_json'])['test_workflow_id'],result['workflow_id'])
        self.submit.assert_awaited_once()
        self.update(result['trade_id'],status='CLOSED',realized_pnl=1)
        app.sync_linked_test_workflows()
        self.assertEqual(app._load_test_workflow(result['workflow_id'])['status'],'CLOSED')

    def test_known_absent_position_is_not_rebound_to_another_same_symbol_fill(self):
        tid=self.trade();self.update(tid,status='OPEN',broker_position_id='old',broker_fill_price=100,broker_fill_units=1)
        positions=[dict(position_id='new',symbol='ACME',instrument_id='1',open_rate=101,units=2,stop_loss_price=97)]
        app.reconcile_news_demo_positions(positions)
        self.assertEqual(self.row(tid)['broker_position_id'],'old')
        self.assertEqual(self.row(tid)['status'],'CLOSED_PNL_UNAVAILABLE')

    def test_maintenance_archives_hourly_without_requiring_new_news(self):
        tid=self.trade();self.update(tid,created_at='2020-01-01 00:00:00')
        sleeps=[]
        async def stop_after_three(_):
            sleeps.append(1)
            if len(sleeps)==3:raise asyncio.CancelledError()
        with patch.object(app,'position_maintenance_interval',return_value=60),patch.object(app,'manage_etoro_demo_positions',AsyncMock()) as manager,patch.object(app.asyncio,'sleep',side_effect=stop_after_three):
            with self.assertRaises(asyncio.CancelledError):asyncio.run(app.maintenance_loop())
        self.assertEqual(manager.await_count,3)
        c=app.get_connection();self.assertEqual(c.execute('SELECT COUNT(*) FROM archived_analysis').fetchone()[0],1);c.close()

    def test_restore_has_retention_grace_period(self):
        tid=self.trade();self.update(tid,created_at='2020-01-01 00:00:00')
        c=app.get_connection();store.archive_analysis(c);store.restore_analysis(c,tid)
        self.assertEqual(store.archive_analysis(c),[]);c.close()

    def test_multi_company_article_associations_survive_storage(self):
        article=dict(article_key='both',ticker='ACME',related_tickers=['ACME','OTHER'],title='Joint venture',fetched_at=datetime.now(timezone.utc).isoformat())
        app.save_news_articles([article])
        self.assertEqual(set(app.group_news_candidates([article])),{'ACME','OTHER'})
        c=app.get_connection();tickers={r[0] for r in c.execute('SELECT ticker FROM article_companies')};c.close()
        self.assertEqual(tickers,{'ACME','OTHER'})

    def test_admin_dashboard_renders_without_provider_requests(self):
        from streamlit.testing.v1 import AppTest
        # The dashboard performs broker/UI billing reads, all mocked here.
        with patch('integrations.get_portfolio_account_snapshot',return_value={'available':False}),patch('integrations.fetch_openai_month_to_date_costs',AsyncMock(return_value={'available':False})),patch('integrations.EtoroBrokerAdapter',return_value=self.adapter):
            ui=AppTest.from_file(str(Path(app.__file__).with_name('streamlit_app.py')),default_timeout=20)
            ui.session_state['auth_token']=app.create_session_token('admin')
            ui.session_state['authenticated']=True
            ui.session_state['username']='admin'
            ui.run()
            self.assertFalse(ui.exception, [x.message for x in ui.exception])

    def test_automatic_news_cycle_runs_all_agents_and_submits(self):
        researcher=MagicMock();researcher.analyze_news_candidate=AsyncMock(return_value=dict(status='available',signal='bearish',confidence=.95,company_relevance='direct',reaction_assessment='structural',summary='Earnings deteriorated'))
        provider=MagicMock();provider.fetch_news=AsyncMock(return_value=[dict(article_key='fixture',fetched_at=datetime.now(timezone.utc).isoformat(),ticker='ACME',title='Acme earnings fall',published_at=None)])
        with patch.object(app,'YahooFinanceNewsProvider',return_value=provider),patch.object(app,'ResearcherAgent',return_value=researcher),patch.object(app,'get_demo_automated_trading_enabled',return_value=True),patch.object(app,'get_ai_research_enabled',return_value=False),patch.object(app,'fetch_market_trends',AsyncMock(return_value={})),patch.object(app,'get_strategy_evaluation',return_value={'status':'inconclusive'}):
            result=asyncio.run(app.run_etoro_news_research_cycle(self.settings))
        self.assertEqual(len(result['trades']),1)
        self.submit.assert_awaited_once()
        self.assertEqual(self.submit.call_args.args[0]['side'],'SHORT')
        c=app.get_connection();stages={r['stage'] for r in c.execute('SELECT * FROM trade_events')};c.close()
        self.assertTrue({'research','strategy','risk','execution'} <= stages)


class AdapterTests(unittest.TestCase):
    def test_short_payload_and_reference_are_exact(self):
        adapter=object.__new__(EtoroBrokerAdapter)
        adapter.settings=types.SimpleNamespace(etoro_demo_trading=True,etoro_api_debug_logging=False)
        adapter.api_logs=[];adapter._find_instrument=MagicMock(return_value={'instrumentId':123,'currency':'USD'})
        adapter._request_json=MagicMock(return_value={'orderId':123456789012345678})
        result=adapter.submit_order(dict(symbol='ACME',side='SHORT',units=2,leverage=2,stop_loss_price=104,take_profit_price=94,currency='usd',reference_id='stable-reference'))
        payload=adapter._request_json.call_args.kwargs['payload']
        self.assertEqual(payload['transaction'],'sellShort')
        self.assertEqual(payload['stopLossRate'],104)
        self.assertEqual(payload['takeProfitRate'],94)
        self.assertEqual(adapter._request_json.call_args.kwargs['request_id'],'stable-reference')
        self.assertEqual(result['order_id'],'123456789012345678')

    def test_all_request_methods_keep_user_agent_and_redact_keys(self):
        adapter=object.__new__(EtoroBrokerAdapter)
        adapter.settings=types.SimpleNamespace(etoro_api_key='fake-api-secret',etoro_user_key='fake-user-secret',etoro_user_agent='Finbot-Test/1.0',etoro_api_base_url='https://example.invalid',etoro_api_debug_logging=True,request_timeout=5)
        adapter.api_logs=[]
        response=MagicMock();response.status=200;response.read.return_value=b'{}';response.__enter__.return_value=response
        import integrations
        with patch.object(integrations,'urlopen',return_value=response) as request:
            for method in ['GET','POST','PATCH']:
                adapter._request_json(method,'/fake',payload={'symbol':'ACME'} if method!='GET' else None)
                self.assertEqual(request.call_args.args[0].get_header('User-agent'),'Finbot-Test/1.0')
        logs=json.dumps(adapter.api_logs)
        self.assertNotIn('fake-api-secret',logs);self.assertNotIn('fake-user-secret',logs)

    def test_history_pagination_and_optional_fees(self):
        adapter=object.__new__(EtoroBrokerAdapter);adapter.settings=types.SimpleNamespace(etoro_demo_trading=True)
        first=[{'positionId':str(i),'netProfit':1,'closeRate':100} for i in range(100)]
        adapter._request_json=MagicMock(side_effect=[first,[{'positionId':'last','netProfit':2,'fees':.5}]])
        result=adapter.get_closed_positions('2026-10-01')
        self.assertTrue(result['complete']);self.assertEqual(len(result['positions']),101)
        self.assertIsNone(result['positions'][0]['fees'])
        self.assertEqual(adapter._request_json.call_args.kwargs['params']['page'],2)

    def test_modify_position_omits_undocumented_target_clear(self):
        adapter=object.__new__(EtoroBrokerAdapter);adapter.settings=types.SimpleNamespace(etoro_demo_trading=True)
        adapter._request_json=MagicMock(return_value={'accepted':True})
        adapter.modify_position('123',96,None)
        args=adapter._request_json.call_args
        self.assertEqual(args.args[:2],('PATCH','/api/v2/trading/demo/positions/123'))
        self.assertNotIn('clearTakeProfit',args.kwargs['payload'])
        self.assertNotIn('takeProfitRate',args.kwargs['payload'])

    def test_zero_unrealized_pnl_is_valid_account_data(self):
        adapter=object.__new__(EtoroBrokerAdapter);adapter.settings=types.SimpleNamespace(etoro_demo_trading=True)
        adapter._request_json=MagicMock(return_value={'credit':1000,'positions':[{'amount':50,'unrealizedPnL':0}],'ordersForOpen':[],'orders':[],'mirrors':[]})
        snapshot=adapter.get_account_snapshot()
        self.assertTrue(snapshot['available']);self.assertEqual(snapshot['equity'],1050)
        self.assertEqual(snapshot['unrealized_pnl'],0)

    def test_broker_timestamps_accept_iso_seconds_and_milliseconds(self):
        from integrations import _broker_time
        stamp='2026-10-14T12:00:00+00:00'
        seconds=datetime.fromisoformat(stamp).timestamp()
        self.assertEqual(_broker_time(seconds),stamp)
        self.assertEqual(_broker_time(seconds*1000),stamp)
        self.assertEqual(_broker_time(stamp),stamp)
        self.assertIsNone(_broker_time('invalid'))

    def test_live_orders_are_blocked(self):
        adapter=object.__new__(EtoroBrokerAdapter);adapter.settings=types.SimpleNamespace(etoro_demo_trading=False)
        with self.assertRaises(ValueError):adapter.submit_order(dict(symbol='ACME',side='LONG',units=1,leverage=2))




class DashboardAndHealthTests(unittest.TestCase):
    def chart_helpers(self):
        import ast
        from typing import Any, Dict, Optional
        source=Path(app.__file__).with_name('streamlit_app.py').read_text()
        tree=ast.parse(source)
        functions=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in {'_lifecycle_event_decision','_trade_chart_events','_trade_management_events'}]
        namespace={'Any':Any,'Dict':Dict,'Optional':Optional}
        exec(compile(ast.Module(body=functions,type_ignores=[]),'chart_helpers','exec'),namespace)
        return namespace['_trade_chart_events']

    def test_explicit_capacity_deferral_remains_on_agent_chart(self):
        event={'stage':'execution','agent_name':'execution','status':'waiting','decision':'deferred','details':{}}
        latest,_=self.chart_helpers()({'events':[event]})
        self.assertEqual(latest['execution']['chart_decision'],'deferred')

    def test_fill_is_executed_despite_later_reconciliation_warning(self):
        events=[dict(stage='execution',agent_name='execution',status='completed',decision='submitted',details={}),
                dict(stage='reconciliation',agent_name='execution',status='completed',decision='rejected',message='TP mismatch')]
        latest,_=self.chart_helpers()({'events':events,'broker_order_id':'o','broker_fill_price':100})
        self.assertEqual(latest['execution']['chart_decision'],'executed')
        self.assertEqual(latest['execution']['chart_reconciliation'],'TP mismatch')

    def test_pending_execution_is_visible_without_capacity_reason(self):
        event=dict(stage='execution',agent_name='execution',status='completed',decision='pending',
                   message='A fresh verified quote is required.',details={})
        latest,_=self.chart_helpers()({'events':[event]})
        self.assertEqual(latest['execution']['decision'],'pending')

    def test_skipped_execution_is_visible_without_implying_submission(self):
        event=dict(stage='execution',agent_name='execution',status='skipped',decision='skipped',details={})
        latest,_=self.chart_helpers()({'events':[event]})
        self.assertEqual(latest['execution']['chart_decision'],'skipped')

    def test_management_checks_collapse_separately_from_entry(self):
        import ast
        from typing import Any, Dict
        tree=ast.parse(Path(app.__file__).with_name('streamlit_app.py').read_text())
        functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_trade_management_events']
        namespace={'Any':Any,'Dict':Dict}
        exec(compile(ast.Module(body=functions,type_ignores=[]),'management_helpers','exec'),namespace)
        events=[dict(stage='execution',decision='submitted',status='completed'),
                dict(stage='recovery',decision='attention',status='attention'),
                dict(stage='recovery',decision='accepted',status='completed'),
                dict(stage='exit',decision='pending',status='submitted')]
        rows=namespace['_trade_management_events']({'events':events,'status':'EXIT_PENDING'})
        self.assertNotIn('execution',rows)
        self.assertEqual(rows['recovery']['chart_decision'],'verified')
        self.assertEqual(rows['recovery']['chart_count'],2)
        self.assertEqual(rows['exit']['chart_decision'],'pending')
        rows=namespace['_trade_management_events']({'events':events,'status':'CLOSED_PNL_UNAVAILABLE'})
        self.assertEqual(rows['exit']['chart_decision'],'closed')

    def test_healthcheck_requires_fresh_worker_and_frontend(self):
        import healthcheck
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'health.db'
            conn=sqlite3.connect(path);conn.execute('CREATE TABLE app_settings(key TEXT,value TEXT)')
            conn.execute('INSERT INTO app_settings VALUES (?,?)',('worker_heartbeat',datetime.now(timezone.utc).isoformat()));conn.commit()
            response=MagicMock();response.status=200;response.__enter__.return_value=response
            with patch.dict(os.environ,{'FINBOT_DB_PATH':str(path)}),patch.object(healthcheck,'urlopen',return_value=response):
                healthcheck.main()
                conn.execute("UPDATE app_settings SET value='2020-01-01T00:00:00+00:00'");conn.commit()
                with self.assertRaisesRegex(RuntimeError,'heartbeat'):healthcheck.main()
            conn.close()


if __name__ == '__main__':
    unittest.main()
