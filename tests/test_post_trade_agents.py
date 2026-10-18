"""Ownership migration and advisory reviews use isolated DBs and saved evidence."""
import asyncio
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch
import makemoney as app
import performance_analysis as analysis
import test_two_direction_workflow as fixtures


class PostTradeTests(unittest.TestCase):
    trade=fixtures.DatabaseTests.trade
    row=fixtures.DatabaseTests.row
    update=fixtures.DatabaseTests.update
    def setUp(self):fixtures.DatabaseTests.setUp(self)
    def tearDown(self):self.stack.close()

    def connection(self):return app.get_connection()
    def outcome(self,pid,net,closed='2026-10-17T12:00:00+00:00',fees=2):
        c=self.connection();c.execute('INSERT INTO broker_closed_positions VALUES (?,?,?,?,?)',(pid,closed,net,fees,'{}'));c.commit();c.close()
    def evidence(self):
        c=self.connection()
        try:return analysis.build_evidence(c,datetime(2026,10,17,tzinfo=timezone.utc),datetime(2026,10,18,tzinfo=timezone.utc))
        finally:c.close()

    def test_adoption_preserves_pending_trades_and_recovery(self):
        tid=self.trade();self.update(tid,status='EXIT_PENDING',broker_position_id='p',take_profit_price=106,policy_json='{"holding_hours":24}')
        manager=app.PositionManagerAgent().recovery_manager()
        action=manager.create(self.row(tid),'close',{'reason':'old'},state='ACKNOWLEDGED',order_id='o',attempts=1,stagger=True)
        before=self.row(tid);c=self.connection()
        self.assertEqual(analysis.adopt(c),1);self.assertEqual(analysis.adopt(c),0);c.close()
        self.assertEqual(self.row(tid),before)
        self.assertEqual(manager.get(tid,'close'),action)
        self.adapter.close_position.assert_not_called()

    def test_direct_position_manager_uses_existing_account_lease(self):
        import workflow_store
        async def attempt():
            async with workflow_store.lease(self.connection,'etoro_demo_account'):
                with self.assertRaises(workflow_store.AccountBusy):
                    await app.PositionManagerAgent().manage(self.settings)
        asyncio.run(attempt())
        self.adapter.get_positions.assert_not_called()

    def test_execution_has_only_entry_responsibilities(self):
        self.assertFalse(hasattr(app.ExecutionAgent,'recovery_manager'))
        self.assertFalse(hasattr(app.ExecutionAgent,'observe_shadow_exits'))
        self.assertTrue(hasattr(app.PositionManagerAgent,'manage'))

    def test_new_management_events_owned_by_position_manager(self):
        tid=self.trade()
        app.record_trade_event(tid,'reconciliation','execution','completed','accepted','Matched',{})
        c=self.connection();row=c.execute('select agent_name from trade_events where trade_id=? order by id desc limit 1',(tid,)).fetchone();c.close()
        self.assertEqual(row[0],'position_manager')

    def test_reviews_are_idempotent_and_cannot_change_orders_or_rules(self):
        tid=self.trade();self.update(tid,status='CLOSED',broker_position_id='p');self.outcome('p',12)
        before=self.row(tid);rules=app.get_agent_rules()
        analyst=analysis.PerformanceAnalystAgent(self.connection)
        now=datetime(2026,10,18,9,tzinfo=timezone.utc)
        self.assertEqual(len(analyst.run_due(now)),2);self.assertEqual(analyst.run_due(now),[])
        self.assertEqual(self.row(tid),before);self.assertEqual(app.get_agent_rules(),rules)
        self.adapter.close_position.assert_not_called();self.adapter.modify_position.assert_not_called()
        self.submit.assert_not_awaited()

    def test_unique_positions_net_costs_and_unknown_profit(self):
        for _ in range(2):
            tid=self.trade();self.update(tid,status='CLOSED',broker_position_id='p',realized_pnl=10)
        self.outcome('p',10,fees=2);self.outcome('unknown',None);self.outcome('loss',-4)
        self.outcome('outside',500,closed='2026-10-18T12:00:00+00:00')
        result=self.evidence()
        self.assertEqual(result['summary']['closed_positions'],2)
        self.assertEqual(result['summary']['net_profit'],6)
        self.assertEqual(result['summary']['average_net_profit'],3)
        self.assertEqual(result['summary']['win_rate_pct'],50)
        self.assertEqual(result['missing_net_outcomes'],1)
        self.assertIn('fewer than',result['recommendations'][0])

    def test_calendar_day_and_week_boundaries_use_stockholm(self):
        analyst=analysis.PerformanceAnalystAgent(self.connection)
        analyst.run_due(datetime(2026,10,18,9,tzinfo=timezone.utc))
        c=self.connection();reports=analysis.list_reviews(c);c.close()
        daily=next(r for r in reports if r['cadence']=='daily');weekly=next(r for r in reports if r['cadence']=='weekly')
        self.assertEqual(daily['window_start'],'2026-10-17T00:00:00+02:00')
        self.assertEqual(weekly['window_start'],'2026-10-05T00:00:00+02:00')
        self.assertEqual(weekly['window_end'],'2026-10-12T00:00:00+02:00')

    def test_cumulative_history_includes_prior_period_without_future_outcomes(self):
        self.outcome('prior',4,closed='2026-10-01T12:00:00Z')
        self.outcome('current',3)
        self.outcome('future',100,closed='2026-10-18T12:00:00Z')
        report=self.evidence()
        self.assertEqual(report['summary']['net_profit'],3)
        self.assertEqual(report['cumulative_summary']['net_profit'],7)

    def test_policy_groups_are_separate(self):
        for pid,mode in [('p1','nearest'),('p2','range_boundary')]:
            tid=self.trade();self.update(tid,status='CLOSED',broker_position_id=pid,policy_json=json.dumps({'target_selection':{'mode':mode}}));self.outcome(pid,1)
        self.assertEqual(len(self.evidence()['groups']['policy_cohort']),2)

    def test_paired_shadow_requires_same_trade_horizon_and_completed_period(self):
        tid=self.trade();other=self.trade();c=self.connection()
        for trade,variant,hours,net in [(tid,'production',48,5),(tid,'1S/1T',48,8),(tid,'2S/1T',168,100),(other,'1S/1T',48,20)]:
            c.execute('INSERT INTO shadow_exits VALUES (?,?,?,?,?,?,?)',(trade,variant,hours,'TARGET_OBSERVED','1','LONG',json.dumps({'estimated_net_pnl':net,'exit_at':'2026-10-17T13:00:00Z','gap_count':1})))
        c.commit();c.close();pairs=self.evidence()['shadow_pairs']
        self.assertEqual(len(pairs),1);self.assertEqual(pairs[0]['estimated_difference'],3)
        self.assertEqual(pairs[0]['observation_gaps'],2)

    def test_persisted_recovery_settings_move_once(self):
        # Simulate upgrade from the four-agent schema without changing history.
        c=self.connection();c.execute("DELETE FROM agent_rules WHERE agent_name='position_manager'")
        saved=dict(app.DEFAULT_AGENT_RULES['execution'],recovery_max_attempts=2)
        c.execute("UPDATE agent_rules SET rules_json=? WHERE agent_name='execution'",(json.dumps(saved),));c.commit();c.close()
        app.ensure_db();c=self.connection()
        rules={r['agent_name']:json.loads(r['rules_json']) for r in c.execute('SELECT * FROM agent_rules')};c.close()
        self.assertEqual(rules['position_manager']['recovery_max_attempts'],2)
        self.assertNotIn('recovery_max_attempts',rules['execution'])
        app.ensure_db();c=self.connection()
        saved=json.loads(c.execute("SELECT rules_json FROM agent_rules WHERE agent_name='position_manager'").fetchone()[0]);c.close()
        self.assertEqual(saved['recovery_max_attempts'],2)

    def test_analysis_threshold_and_required_position_management(self):
        for bad in (0,True,2.5,float('nan')):
            with self.assertRaises(ValueError):app.update_agent_rules('performance_analyst',{'minimum_closed_positions':bad})
        with self.assertRaises(ValueError):app.update_agent_rules('position_manager',{'enabled':False})
