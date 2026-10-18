import asyncio
from datetime import datetime,timedelta,timezone
import json
import unittest
from unittest.mock import patch,AsyncMock
import makemoney as app
import performance_analysis as analysis
import test_two_direction_workflow as fixtures

class ConsolidationTests(unittest.TestCase):
    trade=fixtures.DatabaseTests.trade
    row=fixtures.DatabaseTests.row
    update=fixtures.DatabaseTests.update
    def setUp(self):fixtures.DatabaseTests.setUp(self)

    def test_position_manager_owns_durable_daily_audit_without_broker_requests(self):
        tid=self.trade();self.update(tid,status='OPEN',entry_price=100,quantity=1,stop_loss_price=96,take_profit_price=106)
        before=self.row(tid);manager=app.PositionManagerAgent()
        report=manager.audit_configuration_if_due()
        self.assertEqual(report['total_trades_audited'],1)
        self.assertEqual(manager.audit_configuration_if_due(),None)
        self.assertEqual(len(app.get_configuration_audits()),1)
        self.assertEqual(self.row(tid),before)
        self.adapter.get_positions.assert_not_called();self.adapter.modify_position.assert_not_called();self.submit.assert_not_awaited()

    def test_audit_scope_is_explicit_and_bounded(self):
        for _ in range(52):
            tid=self.trade();self.update(tid,status='CLOSED',entry_price=100,stop_loss_price=96,take_profit_price=106)
        result=app.PositionManagerAgent().audit_configuration()
        self.assertEqual(result['total_trades_audited'],50)
        self.assertEqual(result['scope'],'latest_50_eligible_trades')
        self.assertEqual(len(result['results']),50)

    def test_old_auditor_name_is_compatibility_helper_not_seventh_agent(self):
        self.assertIs(app.OverwatcherAgent,app.TradeConfigurationAuditor)
        self.assertEqual(len(app.DEFAULT_AGENT_RULES),6)

    def test_historical_analysis_is_analyst_owned_and_does_not_change_position(self):
        tid=self.trade();self.update(tid,status='EXIT_PENDING',broker_position_id='p',entry_price=100,stop_loss_price=96,take_profit_price=106)
        app.record_trade_event(tid,'execution','execution','completed','submitted','Submitted',{})
        before=self.row(tid)
        data=dict(available=True,window_days=14,bars_analyzed=2,first_exit='take_profit',window_complete=False)
        with patch.object(app,'validate_trade_price_path',AsyncMock(return_value=data)) as fetch:
            result=asyncio.run(app.validate_trade_outcome(tid))
        self.assertEqual(result['analysis_owner'],'performance_analyst')
        self.assertEqual(result['validation_scope'],'broker_position_reference')
        self.assertEqual(result['reference_prices']['take_profit_price'],106)
        saved=app.get_historical_path_analyses()[0]
        self.assertEqual(saved['agent_name'],'performance_analyst')
        self.assertEqual(self.row(tid),before);fetch.assert_awaited_once();self.submit.assert_not_awaited()

    def test_new_reviews_include_saved_audit_and_path_evidence_without_reclassifying_returns(self):
        tid=self.trade();self.update(tid,status='OPEN',entry_price=100,stop_loss_price=96,take_profit_price=106)
        app.PositionManagerAgent().audit_configuration()
        app.record_trade_event(tid,'validation','performance_analyst','completed','observed','Retrospective scenario',
            dict(validation_type='historical_trade_outcome',available=True,first_exit='take_profit',validation_scope='counterfactual_opportunity'))
        c=app.get_connection();now=datetime.now(timezone.utc)
        evidence=analysis.build_evidence(c,now-timedelta(days=1),now+timedelta(seconds=1));c.close()
        self.assertEqual(evidence['configuration_audit']['trades_checked'],1)
        self.assertEqual(len(evidence['historical_path_analyses']),1)
        self.assertEqual(evidence['summary']['closed_positions'],0)
        self.assertEqual(evidence['summary']['net_profit'],0)

    def test_existing_report_payloads_are_not_rewritten(self):
        agent=analysis.PerformanceAnalystAgent(app.get_connection)
        agent.run_due(datetime(2026,10,18,9,tzinfo=timezone.utc))
        before=app.get_performance_reviews()
        app.PositionManagerAgent().audit_configuration()
        self.assertEqual(app.get_performance_reviews(),before)
