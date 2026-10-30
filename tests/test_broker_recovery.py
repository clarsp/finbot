"""Recovery invariants: temporary DBs, fake clock and mocked broker only."""
import asyncio
from datetime import datetime,timezone
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import io
import broker_recovery as recovery
import makemoney as app
from integrations import EtoroAPIError,EtoroBrokerAdapter
import test_two_direction_workflow as fixtures

class RecoveryTests(unittest.TestCase):
    trade=fixtures.DatabaseTests.trade
    row=fixtures.DatabaseTests.row
    update=fixtures.DatabaseTests.update

    def setUp(self):
        fixtures.DatabaseTests.setUp(self)
        self.now=datetime(2026,10,20,12,tzinfo=timezone.utc).timestamp()
        self.manager=recovery.RecoveryManager(app.get_connection,app.record_trade_event,self.rules['position_manager'],clock=lambda:self.now)
        self.tid=self.trade()
        self.update(self.tid,status='OPEN',broker_position_id='p',broker_order_id='open',entry_price=100,quantity=1,
            broker_fill_price=100,broker_fill_units=1,stop_loss_price=96,take_profit_price=106,
            policy_json=json.dumps({'fill_adjusted':True,'holding_hours':48}),opened_at=datetime.now(timezone.utc).isoformat())
        self.t=self.row(self.tid)
        self.p=dict(position_id='p',instrument_id='1',side='LONG',units=1,amount=50,open_rate=100,current_rate=100,stop_loss_price=95,take_profit_price=106)
        self.adapter.get_rate.return_value={'bid':100,'ask':100.1}
        self.adapter.lookup_close_order.return_value={'orderID':'close-1','statusID':1,'positions':[],'errorCode':0}

    def advance(self,seconds=2000):self.now+=seconds
    def close(self):return asyncio.run(self.manager.request_close(self.t,self.p,self.adapter,'holding_deadline'))
    def observe_close(self,present=True):return asyncio.run(self.manager.observe_close(self.t,self.p if present else None,self.adapter))
    def protect(self,**kw):return asyncio.run(self.manager.request_protection(self.t,self.p,self.adapter,96,106,**kw))
    def observe_protect(self):return asyncio.run(self.manager.observe_protection(self.t,self.p,self.adapter))
    def action(self,kind):return self.manager.get(self.tid,kind)
    def attention_count(self):
        c=app.get_connection();n=c.execute("SELECT COUNT(*) FROM trade_events WHERE trade_id=? AND stage='recovery' AND decision='attention'",(self.tid,)).fetchone()[0];c.close();return n

    def test_missing_position_id_repairs_before_one_bounded_correction(self):
        action=self.manager.create({**self.t,'broker_position_id':None},'protection',{'stop_loss_price':96,'take_profit_price':106},state='RETRY_READY')
        self.manager.update(action,mismatches=2000,checks=2,next_check_at=self.now-1,created_at=self.now-1000)
        self.observe_protect()
        current=self.action('protection')
        self.assertEqual(current['position_id'],'p')
        self.assertEqual(current['attempts'],1)
        self.adapter.modify_position.assert_called_once()
        self.assertGreater(current['next_check_at'],self.now)
        for _ in range(5):self.observe_protect()
        self.adapter.modify_position.assert_called_once()

    def test_conflicting_identity_blocks_writes_and_backs_off(self):
        a=self.manager.create(self.t,'protection',{'stop_loss_price':96,'take_profit_price':106},state='RETRY_READY')
        self.manager.update(a,position_id='other')
        self.observe_protect()
        self.assertEqual(self.action('protection')['state'],'ESCALATED')
        self.assertGreater(self.action('protection')['next_check_at'],self.now)
        self.adapter.modify_position.assert_not_called()
        self.adapter.get_rate.assert_not_called()

    def test_pretransport_failure_advances_retry_ready_backoff(self):
        a=self.manager.create(self.t,'protection',{'stop_loss_price':96,'take_profit_price':106},state='RETRY_READY')
        self.manager.update(a,created_at=self.now-1000,mismatches=2)
        with patch.object(self.manager,'send',side_effect=ValueError('pretransport failure')):
            self.observe_protect()
        self.assertGreater(self.action('protection')['next_check_at'],self.now)
        self.assertEqual(self.action('protection')['failures'],1)
        self.adapter.get_rate.reset_mock()
        self.observe_protect();self.adapter.get_rate.assert_not_called()

    def test_tighter_broker_stop_is_adopted_without_widening(self):
        a=self.manager.create({**self.t,'broker_position_id':None},'protection',{'stop_loss_price':96,'take_profit_price':106},state='ESCALATED')
        self.p['stop_loss_price']=97
        self.observe_protect()
        self.assertIsNone(self.action('protection'))
        self.assertEqual(self.row(self.tid)['stop_loss_price'],97)
        self.adapter.modify_position.assert_not_called()

    def test_short_tighter_stop_adoption_preserves_broker_protection(self):
        self.update(self.tid,side='SHORT',stop_loss_price=104,take_profit_price=94)
        self.t=self.row(self.tid);self.p.update(side='SHORT',stop_loss_price=103,take_profit_price=94)
        self.manager.create(self.t,'protection',{'stop_loss_price':104,'take_profit_price':94},state='ESCALATED')
        self.observe_protect()
        self.assertEqual(self.row(self.tid)['stop_loss_price'],103)
        self.assertIsNone(self.action('protection'))
        self.adapter.modify_position.assert_not_called()

    def test_configuration_audit_includes_active_recovery_discrepancy(self):
        self.manager.create({**self.t,'broker_position_id':None},'protection',{'stop_loss_price':96,'take_profit_price':106},state='RETRY_READY')
        result=app.TradeConfigurationAuditor().validate_trade_configuration(self.tid)
        self.assertTrue(any('Unresolved broker recovery' in reason for reason in result['issues']))

    def test_close_is_deduplicated_and_correlation_survives_restart(self):
        self.close();a=self.action('close')
        self.assertEqual(a['state'],'ACKNOWLEDGED');self.assertEqual(a['order_id'],'close-1')
        self.assertEqual(a['attempts'],1);self.assertTrue(a['request_id'])
        self.manager=recovery.RecoveryManager(app.get_connection,app.record_trade_event,self.rules['position_manager'],clock=lambda:self.now)
        result=self.close()
        self.assertTrue(result['deduplicated']);self.adapter.close_position.assert_called_once()
        self.assertEqual(self.row(self.tid)['status'],'EXIT_PENDING')

    def test_two_lifecycles_for_same_position_cannot_send_duplicate_close(self):
        self.close();other=self.trade();self.update(other,status='OPEN',broker_position_id='p')
        result=asyncio.run(self.manager.request_close(self.row(other),self.p,self.adapter,'same_position'))
        self.assertTrue(result['deduplicated'])
        self.assertEqual(result['owner_trade_id'],self.tid)
        self.adapter.close_position.assert_called_once()

    def test_intent_committed_before_transport(self):
        def check(*args,**kw):
            a=self.action('close')
            self.assertEqual(a['state'],'SUBMITTING');self.assertEqual(a['request_id'],kw['request_id'])
            self.assertEqual(self.row(self.tid)['status'],'EXIT_PENDING')
            return {'order_id':'close-1'}
        self.adapter.close_position.side_effect=check;self.close()

    def test_pending_reads_back_off_without_extra_closes(self):
        self.close();self.observe_close();self.adapter.lookup_close_order.assert_not_called()
        self.advance();self.observe_close();after=self.action('close')
        for _ in range(10):self.observe_close()
        self.adapter.lookup_close_order.assert_called_once_with('close-1')
        self.adapter.close_position.assert_called_once()
        self.assertGreater(after['next_check_at'],self.now)

    def test_ambiguous_close_timeout_never_blindly_resubmits(self):
        self.adapter.close_position.side_effect=TimeoutError('Response lost')
        with self.assertRaises(TimeoutError):self.close()
        a=self.action('close');self.assertEqual(a['state'],'UNKNOWN')
        self.adapter.lookup_order.side_effect=EtoroAPIError('HTTP 404',status_code=404)
        for _ in range(4):
            self.advance();self.observe_close()
        self.adapter.close_position.assert_called_once()
        self.assertEqual(self.action('close')['attempts'],1)
        self.assertGreater(self.attention_count(),0)

    def test_new_attempt_clears_previous_order_id_if_response_is_lost(self):
        self.close();self.adapter.lookup_close_order.return_value={'orderID':'close-1','status':{'name':'Rejected'},'positions':[]}
        self.advance();self.observe_close();self.assertEqual(self.action('close')['state'],'RETRY_READY')
        self.adapter.close_position.side_effect=TimeoutError('Second response lost')
        self.advance();self.observe_close()
        self.assertIsNone(self.action('close')['order_id'])
        self.adapter.lookup_order.return_value={'action':'close','status':{'name':'Pending'},'orderId':'close-2'}
        self.advance();self.observe_close()
        self.assertEqual(self.action('close')['order_id'],'close-2')
        self.assertEqual(self.adapter.close_position.call_count,2)

    def test_terminal_rejection_allows_bounded_replacement_only_after_wait(self):
        self.close()
        for attempt in range(3):
            a=self.action('close');self.adapter.lookup_close_order.return_value={'orderID':a['order_id'],'status':{'name':'Cancelled'},'positions':[]}
            self.advance();self.observe_close()
            if attempt<2:
                self.assertEqual(self.action('close')['state'],'RETRY_READY')
                self.observe_close();self.assertEqual(self.adapter.close_position.call_count,attempt+1)
                self.advance();self.observe_close()
        self.assertEqual(self.adapter.close_position.call_count,3)
        self.assertEqual(self.action('close')['state'],'ESCALATED')
        self.assertEqual(self.attention_count(),1)

    def test_partial_execution_is_not_assumed_closed_or_retried(self):
        self.close();self.adapter.lookup_close_order.return_value={'orderID':'close-1','statusID':3,'positions':[{'positionID':'p','units':.5,'occurred':'2026-10-20T12:00Z','rate':102}]}
        self.advance();self.observe_close()
        self.assertEqual(self.action('close')['state'],'EXECUTION_REPORTED')
        self.assertEqual(self.row(self.tid)['status'],'EXIT_PENDING')
        self.adapter.close_position.assert_called_once();self.assertEqual(self.attention_count(),1)

    def test_unknown_numeric_status_is_preserved_not_guessed_as_cancelled(self):
        self.close();self.adapter.lookup_close_order.return_value={'orderID':'close-1','statusID':987,'positions':[]}
        self.advance();self.observe_close()
        self.assertEqual(self.action('close')['state'],'ACKNOWLEDGED')
        self.assertEqual(self.action('close')['evidence']['statusID'],987)
        self.adapter.close_position.assert_called_once()

    def test_wrong_close_order_response_cannot_authorize_replacement(self):
        self.close();self.adapter.lookup_close_order.return_value={'orderID':'another-order','status':{'name':'Rejected'},'positions':[]}
        self.advance();self.observe_close()
        self.assertNotEqual(self.action('close')['state'],'RETRY_READY')
        self.adapter.close_position.assert_called_once()

    def test_absence_resolves_close_without_inventing_realized_pnl(self):
        self.close();self.observe_close(False)
        self.assertIsNone(self.action('close'))
        self.assertEqual(app.get_broker_actions(self.tid)[0]['state'],'CLOSED')
        self.assertEqual(self.row(self.tid)['realized_pnl'],0)
        self.adapter.lookup_close_order.assert_not_called()

    def test_protection_converges_after_spaced_identical_bounded_retries(self):
        self.protect()
        self.observe_protect();self.assertEqual(self.action('protection')['mismatches'],0)
        for _ in range(5):self.advance();self.observe_protect()
        self.assertEqual(self.adapter.modify_position.call_count,3)
        for call in self.adapter.modify_position.call_args_list:self.assertEqual(call.args,('p',96.0,106.0))
        self.assertEqual(self.action('protection')['state'],'ESCALATED')
        self.assertEqual(self.attention_count(),1)

    def test_matching_protection_resolves_without_mutation_or_lookup(self):
        self.protect();self.p['stop_loss_price']=96
        self.observe_protect()
        self.assertIsNone(self.action('protection'))
        self.assertEqual(app.get_broker_actions(self.tid)[0]['state'],'APPLIED')
        self.adapter.modify_position.assert_called_once()
        self.adapter.lookup_close_order.assert_not_called()

    def test_initial_automatic_correction_never_loosens_tighter_stop(self):
        self.p['stop_loss_price']=97
        with self.assertRaises(ValueError):self.protect()
        self.adapter.modify_position.assert_not_called()
        self.assertEqual(self.attention_count(),1)
        self.protect(explicit=True)
        self.adapter.modify_position.assert_called_once()

    def test_protection_already_at_requested_levels_does_not_patch_again(self):
        self.p['stop_loss_price']=96
        result=self.protect()
        self.assertTrue(result['already_applied'])
        self.adapter.modify_position.assert_not_called()

    def test_recovery_stops_if_price_clearance_is_lost(self):
        self.protect();self.adapter.get_rate.return_value={'bid':95,'ask':95.1}
        for _ in range(2):self.advance();self.observe_protect()
        self.assertEqual(self.action('protection')['state'],'ESCALATED')
        self.adapter.modify_position.assert_called_once()

    def test_retry_after_survives_restart_and_never_retries_early(self):
        self.adapter.close_position.side_effect=EtoroAPIError('HTTP 429',status_code=429,retry_after_seconds=3600)
        with self.assertRaises(EtoroAPIError):self.close()
        a=self.action('close');self.assertGreaterEqual(a['next_check_at'],self.now+3600)
        self.advance(3599);self.observe_close();self.adapter.close_position.assert_called_once()
        self.adapter.close_position.side_effect=None
        self.advance(2);self.observe_close();self.assertEqual(self.adapter.close_position.call_count,2)

    def test_read_429_delays_reads_without_creating_write(self):
        self.close();self.adapter.lookup_close_order.side_effect=EtoroAPIError('HTTP 429',status_code=429,retry_after_seconds=7200)
        self.advance();self.observe_close();self.assertGreaterEqual(self.action('close')['next_check_at'],self.now+7200)
        self.advance(100);self.observe_close()
        self.adapter.lookup_close_order.assert_called_once();self.adapter.close_position.assert_called_once()

    def test_permanent_write_rejection_alerts_once_and_does_not_resend(self):
        self.adapter.close_position.side_effect=EtoroAPIError('HTTP 400',status_code=400)
        with self.assertRaises(EtoroAPIError):self.close()
        for _ in range(3):self.advance();self.observe_close()
        self.adapter.close_position.assert_called_once();self.adapter.lookup_close_order.assert_not_called()
        self.assertEqual(self.attention_count(),1)

    def test_legacy_close_imports_nested_id_without_posting(self):
        self.t['status']='EXIT_PENDING'
        app.record_trade_event(self.tid,'exit','execution','submitted','pending','expiry',{'broker_close':{'response':{'orderForClose':{'orderID':99}}}})
        self.manager.import_legacy(self.t,self.p)
        self.assertEqual(self.action('close')['order_id'],'99')
        self.assertEqual(self.action('close')['attempts'],1)
        self.adapter.close_position.assert_not_called()

    def test_absent_position_does_not_reimport_completed_protection_forever(self):
        self.t['status']='CLOSED_PNL_UNAVAILABLE'
        self.t['policy_json']=json.dumps({'last_protection_request_signature':json.dumps({'stop_loss_price':96,'take_profit_price':106}),'last_protection_request':self.now})
        for _ in range(3):self.manager.import_legacy(self.t,None)
        self.assertEqual(app.get_broker_actions(self.tid),[])

    def test_entry_execution_waiting_for_portfolio_keeps_backoff(self):
        self.t.update(status='SUBMITTING',order_reference='ref')
        self.adapter.lookup_order.return_value={'status':{'name':'Filled'},'positionExecutions':[{'positionId':'p'}]}
        asyncio.run(self.manager.lookup_entry(self.t,self.adapter))
        for _ in range(5):asyncio.run(self.manager.lookup_entry(self.t,self.adapter))
        self.adapter.lookup_order.assert_called_once()
        self.assertEqual(self.action('entry')['state'],'EXECUTION_REPORTED')

    def test_legacy_close_without_id_escalates_instead_of_resubmitting(self):
        self.t['status']='EXIT_PENDING';self.manager.import_legacy(self.t,self.p)
        self.advance();self.observe_close()
        self.assertEqual(self.action('close')['state'],'ESCALATED')
        self.adapter.close_position.assert_not_called()

    def test_weekends_slow_queued_reads_and_do_not_create_age_only_alerts(self):
        self.now=datetime(2026,10,17,10,tzinfo=timezone.utc).timestamp()
        self.close();a=self.action('close');self.assertGreaterEqual(a['next_check_at'],self.now+900)
        self.advance(10*3600);self.observe_close()
        self.assertIsNone(self.action('close')['alert_at'])
        self.assertEqual(recovery.business_age(self.now-10*3600,self.now),0)

    def test_main_maintenance_checks_pending_close_and_preserves_status(self):
        self.close();self.advance()
        self.adapter.get_positions.return_value={'available':True,'positions':[self.p]}
        with patch.object(app.PositionManagerAgent,'recovery_manager',return_value=self.manager):
            asyncio.run(app.manage_etoro_demo_positions())
        self.adapter.lookup_close_order.assert_called_once()
        self.adapter.close_position.assert_called_once()
        self.assertEqual(self.row(self.tid)['status'],'EXIT_PENDING')

    def test_actions_and_alerts_are_persisted_and_redacted(self):
        self.adapter.close_position.return_value={'order_id':'close-1','token':'private','nested':{'authorization':'secret'}}
        self.close();a=self.action('close')
        self.assertEqual(a['evidence']['token'],'[REDACTED]')
        self.assertEqual(a['evidence']['nested']['authorization'],'[REDACTED]')
        self.assertEqual(len(app.get_broker_actions(self.tid,True)),1)

    def test_recovery_ui_renders_real_tracked_state(self):
        from streamlit.testing.v1 import AppTest
        self.close()
        page=AppTest.from_string('import streamlit_app as ui\nui.render_broker_recovery()').run(timeout=20)
        self.assertEqual(list(page.exception),[])

    def test_legacy_fixture_close_adopts_existing_position_without_new_entry(self):
        self.update(self.tid,status='CLOSED',broker_position_id=None)
        self.adapter.get_positions.return_value={'available':True,'positions':[self.p]}
        c=app.get_connection();cur=c.execute("INSERT INTO test_workflows(symbol,amount,status,broker_position_id,details_json) VALUES ('ACME',10,'OPEN','p','{}')");wid=cur.lastrowid;c.commit();c.close()
        result=asyncio.run(app.close_demo_test_workflow(wid))
        self.assertEqual(result['status'],'EXIT_PENDING')
        self.adapter.close_position.assert_called_once();self.submit.assert_not_awaited()
        linked=app._load_test_workflow(wid)['details']['trade_id']
        self.assertEqual(app.get_broker_actions(linked)[0]['state'],'ACKNOWLEDGED')

    def test_invalid_recovery_rules_are_rejected(self):
        for key,value in [('recovery_max_attempts',0),('recovery_max_attempts',1.2),('recovery_initial_check_seconds',1),('recovery_escalate_hours',float('nan'))]:
            original=dict(self.rules['execution'])
            with self.subTest(key=key),self.assertRaises(ValueError):app.update_agent_rules('execution',{key:value})
            self.rules['execution']=original

    def test_tighter_stop_and_different_target_are_reconciled_without_widening(self):
        self.p.update(stop_loss_price=97,take_profit_price=105)
        a=self.manager.create(self.t,'protection',{'stop_loss_price':96,'take_profit_price':106},state='ESCALATED')
        self.manager.update(a,created_at=self.now-1000,mismatches=20)
        self.observe_protect()
        self.adapter.modify_position.assert_called_once()
        args=self.adapter.modify_position.call_args.args
        self.assertEqual(args,('p',97,106))
        self.assertEqual(self.action('protection')['payload']['stop_loss_price'],97)

    def test_pending_expiry_cancellation_is_durable_and_fill_wins_race(self):
        self.t.update(created_at=datetime.fromtimestamp(self.now-7200,timezone.utc).isoformat(),broker_order_id='123')
        data={'orderId':123,'status':{'id':11},'positionExecutions':[]}
        self.adapter.cancel_pending_order.return_value={'accepted':True}
        asyncio.run(self.manager.expire_pending_entry(self.t,data,self.adapter))
        asyncio.run(self.manager.expire_pending_entry(self.t,data,self.adapter))
        self.adapter.cancel_pending_order.assert_called_once()
        self.assertEqual(self.action('entry_cancel')['state'],'ACKNOWLEDGED')
        asyncio.run(self.manager.expire_pending_entry(self.t,{**data,'status':{'id':9},'positionExecutions':[{'positionId':'p'}]},self.adapter))
        self.assertIsNone(self.action('entry_cancel'))
        self.adapter.close_position.assert_not_called()

    def test_cancel_unknown_status_or_wrong_order_never_sends(self):
        self.t.update(created_at=datetime.fromtimestamp(self.now-7200,timezone.utc).isoformat(),broker_order_id='123')
        for d in [{'orderId':124,'status':{'id':11}},{'orderId':123,'status':{'id':99}},{'orderId':123,'status':{'id':3}}]:
            asyncio.run(self.manager.expire_pending_entry(self.t,d,self.adapter))
        self.adapter.cancel_pending_order.assert_not_called()

    def test_profit_close_rechecks_each_attempt_and_withholds_after_price_reversal(self):
        from unittest.mock import AsyncMock
        self.now=datetime(2026,10,20,17,tzinfo=timezone.utc).timestamp()
        self.update(self.tid,opened_at='2026-10-20T14:00:00Z',config_json=json.dumps(self.rules))
        self.t=self.row(self.tid)
        self.p.update(opened_at='2026-10-20T14:00:00Z',units=6,amount=300,fees=0,unrealized_pnl=120)
        self.adapter.get_positions.return_value={'available':True,'positions':[self.p]}
        self.adapter.get_rate.return_value={'bid':120,'ask':120.1}
        fundamentals={'exchange':'NMS','exchange_timezone':'America/New_York','exchange_session_start_epoch':datetime(2026,10,20,13,30,tzinfo=timezone.utc).timestamp(),'exchange_session_end_epoch':datetime(2026,10,20,20,tzinfo=timezone.utc).timestamp()}
        with patch('integrations.fetch_company_fundamentals',AsyncMock(return_value=fundamentals)):
            asyncio.run(self.manager.request_close(self.t,self.p,self.adapter,'profit_review:actual_fill_exceeded_configured_units_or_spend_cap'))
            self.adapter.close_position.assert_called_once()
            a=self.action('close');self.manager.update(a,state='RETRY_READY',next_check_at=0)
            self.p['unrealized_pnl']=-1;self.adapter.get_rate.return_value={'bid':99,'ask':99.1}
            asyncio.run(self.manager.send(a,self.row(self.tid),self.p,self.adapter))
        self.adapter.close_position.assert_called_once()
        self.assertIsNone(self.action('close'))
        self.assertEqual(self.row(self.tid)['status'],'OPEN')

    def test_profitable_short_news_exit_requires_direct_research_and_regular_session(self):
        from unittest.mock import AsyncMock
        self.now=datetime(2026,10,20,17,tzinfo=timezone.utc).timestamp()
        self.update(self.tid,side='SHORT',opened_at='2026-10-20T14:00:00Z',config_json=json.dumps(self.rules))
        self.t=self.row(self.tid)
        self.p.update(side='SHORT',opened_at='2026-10-20T14:00:00Z',units=2,amount=100,fees=0,unrealized_pnl=20)
        self.adapter.get_positions.return_value={'available':True,'positions':[self.p]}
        self.adapter.get_rate.return_value={'bid':89.9,'ask':90}
        app.record_trade_event(self.tid,'research','researcher','completed','accepted','New company evidence',
            {'signal':'bullish','confidence':.9,'openai':{'status':'available','company_relevance':'direct','reaction_assessment':'structural'}})
        c=app.get_connection();c.execute("UPDATE trade_events SET created_at=? WHERE trade_id=? AND stage='research'",('2026-10-20 16:59:00',self.tid));c.commit();c.close()
        fundamentals={'exchange':'NMS','exchange_timezone':'America/New_York','exchange_session_start_epoch':datetime(2026,10,20,13,30,tzinfo=timezone.utc).timestamp(),'exchange_session_end_epoch':datetime(2026,10,20,20,tzinfo=timezone.utc).timestamp()}
        with patch('integrations.fetch_company_fundamentals',AsyncMock(return_value=fundamentals)):
            asyncio.run(self.manager.request_close(self.t,self.p,self.adapter,'profit_review:structural_news_invalidated_thesis'))
            self.adapter.close_position.assert_called_once()
            self.assertTrue(self.action('close')['payload']['profit_guard']['allowed'])
            a=self.action('close');self.manager.update(a,state='RETRY_READY',next_check_at=0)
            self.now=datetime(2026,10,20,21,tzinfo=timezone.utc).timestamp()
            asyncio.run(self.manager.send(a,self.row(self.tid),self.p,self.adapter))
        self.adapter.close_position.assert_called_once()
        self.assertIsNone(self.action('close'))

    def test_pending_validity_starts_at_submission_not_old_analysis(self):
        self.t.update(created_at=datetime.fromtimestamp(self.now-86400,timezone.utc).isoformat(),broker_order_id='123',
            policy_json=json.dumps({'entry_submitted_at':datetime.fromtimestamp(self.now-60,timezone.utc).isoformat()}))
        asyncio.run(self.manager.expire_pending_entry(self.t,{'orderId':123,'status':{'id':11}},self.adapter))
        self.adapter.cancel_pending_order.assert_not_called()


class AdapterRecoveryTests(unittest.TestCase):
    def test_close_lookup_uses_dedicated_read_endpoint(self):
        adapter=EtoroBrokerAdapter.__new__(EtoroBrokerAdapter)
        from types import SimpleNamespace
        adapter.settings=SimpleNamespace(etoro_demo_trading=True)
        with patch.object(adapter,'_request_json',return_value={'orderID':99}) as request:
            self.assertEqual(adapter.lookup_close_order('99.0'),{'orderID':99})
        request.assert_called_once_with('GET','/api/v1/trading/info/demo/close-orders/99')

    def test_retry_after_http_metadata_is_retained(self):
        import integrations
        from types import SimpleNamespace
        adapter=EtoroBrokerAdapter.__new__(EtoroBrokerAdapter)
        adapter.settings=SimpleNamespace(etoro_api_base_url='https://broker.invalid',etoro_api_debug_logging=False)
        adapter.api_logs=[]
        error=HTTPError('https://broker.invalid',429,'Too Many Requests',{'Retry-After':'120'},io.BytesIO(b'{}'))
        with patch.object(adapter,'_headers',return_value={'x-request-id':'ref'}),patch('brokers.etoro.urlopen',side_effect=error):
            with self.assertRaises(EtoroAPIError) as captured:adapter._request_json('GET','/status')
        self.assertEqual(captured.exception.status_code,429)
        self.assertEqual(captured.exception.retry_after_seconds,120)

    def test_pending_cancel_is_demo_only_and_uses_documented_endpoint(self):
        from types import SimpleNamespace
        adapter=EtoroBrokerAdapter.__new__(EtoroBrokerAdapter)
        adapter.settings=SimpleNamespace(etoro_demo_trading=True)
        with patch.object(adapter,'_request_json',return_value={}) as request:
            adapter.cancel_pending_order('123.0',request_id='cancel-ref')
        request.assert_called_once_with('DELETE','/api/v2/trading/execution/demo/orders/123',request_id='cancel-ref')
        adapter.settings.etoro_demo_trading=False
        with self.assertRaises(ValueError):adapter.cancel_pending_order('123')

class TrailingCloseTests(unittest.TestCase):
    setUp=RecoveryTests.setUp
    trade=RecoveryTests.trade
    row=RecoveryTests.row
    update=RecoveryTests.update

    def prime(self):
        import position_policy
        self.manager.rules={**self.manager.rules,'profit_trailing_enabled':True,'profit_trailing_arming_basis':'atr'}
        self.t['policy_json']=json.dumps({'atr14':4})
        self.update(self.tid,policy_json=self.t['policy_json'])
        c=app.get_connection()
        position_policy.observe_trailing_profit(c,self.t,self.p,{'bid':108,'ask':108.1},self.now,self.manager.rules)
        self.adapter.get_rate.return_value={'bid':107,'ask':107.1}
        c.close()

    def test_fresh_drawdown_submits_once_and_unknown_does_not_duplicate(self):
        self.prime()
        self.adapter.get_positions.return_value={'available':True,'positions':[{**self.p,'unrealized_pnl':32}]}
        asyncio.run(self.manager.request_close(self.t,self.p,self.adapter,'profit_trailing:drawdown'))
        asyncio.run(self.manager.request_close(self.t,self.p,self.adapter,'profit_trailing:drawdown'))
        self.adapter.close_position.assert_called_once()
        self.adapter.modify_position.assert_not_called()

    def test_short_close_uses_fresh_ask_and_upward_retreat(self):
        self.t['side']='SHORT';self.p['side']='SHORT';self.t['policy_json']=json.dumps({'atr14':4})
        self.update(self.tid,side='SHORT',policy_json=self.t['policy_json'])
        self.manager.rules={**self.manager.rules,'profit_trailing_enabled':True,'profit_trailing_arming_basis':'atr'}
        import position_policy
        c=app.get_connection()
        position_policy.observe_trailing_profit(c,self.t,self.p,{'bid':91.9,'ask':92},self.now,self.manager.rules);c.close()
        self.adapter.get_positions.return_value={'available':True,'positions':[self.p]}
        self.adapter.get_rate.return_value={'bid':92.9,'ask':93}
        asyncio.run(self.manager.request_close(self.t,self.p,self.adapter,'profit_trailing:drawdown'))
        self.adapter.close_position.assert_called_once()

    def test_rebound_or_missing_snapshot_withholds_close(self):
        self.prime()
        for snapshot,rate in [({'available':True,'positions':[self.p]},{'bid':107.5,'ask':107.6}),
                              ({'available':False,'positions':[]},{'bid':107,'ask':107.1}),
                              ({'available':True,'positions':[self.p]},{'available':False})]:
            self.adapter.get_positions.return_value=snapshot
            self.adapter.get_rate.return_value=rate
            result=asyncio.run(self.manager.request_close(self.t,self.p,self.adapter,'profit_trailing:drawdown'))
            self.assertTrue(result['withheld'])
        self.adapter.close_position.assert_not_called()

    def test_retry_revalidates_after_confirmed_failure(self):
        self.prime()
        self.adapter.get_positions.return_value={'available':True,'positions':[{**self.p,'unrealized_pnl':32}]}
        asyncio.run(self.manager.request_close(self.t,self.p,self.adapter,'profit_trailing:drawdown'))
        action=self.manager.get(self.tid,'close')
        self.manager.update(action,state='RETRY_READY',next_check_at=self.now)
        self.adapter.get_positions.return_value={'available':True,'positions':[self.p]}
        self.adapter.get_rate.return_value={'bid':109,'ask':109.1}
        result=asyncio.run(self.manager.send(action,self.t,self.p,self.adapter))
        self.assertTrue(result['withheld'])
        self.adapter.close_position.assert_called_once()
        self.assertEqual(self.row(self.tid)['status'],'OPEN')
        c=app.get_connection()
        self.assertEqual(c.execute('SELECT best_price FROM price_trailing WHERE trade_id=?',(self.tid,)).fetchone()[0],109)
        c.close()
