import asyncio
from datetime import datetime,timezone
import unittest
import makemoney as app
import scheduler_policy as pacing
import test_two_direction_workflow as fixtures

class WeekendSchedulerTests(unittest.TestCase):
    trade=fixtures.DatabaseTests.trade
    row=fixtures.DatabaseTests.row
    update=fixtures.DatabaseTests.update
    def setUp(self):
        fixtures.DatabaseTests.setUp(self)
        app.configure_research_schedule('automatic',60,weekend_trading_enabled=False)
        self.saturday=datetime(2026,10,17,12,tzinfo=timezone.utc)
    def when(self,hour,day=17):return datetime(2026,10,day,hour,tzinfo=timezone.utc)

    def test_weekend_research_is_six_hourly_and_claimed_once(self):
        self.assertTrue(app.claim_due_research_cycle(self.saturday))
        self.assertFalse(app.claim_due_research_cycle(self.saturday))
        self.assertFalse(app.claim_due_research_cycle(self.when(13)))
        self.assertTrue(app.claim_due_research_cycle(self.when(18)))
        self.assertEqual(app.get_research_schedule(self.saturday)['effective_interval_minutes'],360)

    def test_weekday_cadence_resumes_without_weekend_delay(self):
        self.assertTrue(app.claim_due_research_cycle(self.when(20,18)))
        self.assertTrue(app.claim_due_research_cycle(self.when(0,19)))
        self.assertEqual(app.get_research_schedule(self.when(0,19))['effective_interval_minutes'],60)
        self.assertFalse(app.claim_due_research_cycle(self.when(0,19)))
        self.assertTrue(app.claim_due_research_cycle(self.when(1,19)))

    def test_news_claim_is_persisted_across_restart(self):
        self.assertTrue(app.claim_due_news_fetch(3600,self.saturday))
        self.assertFalse(app.claim_due_news_fetch(3600,self.when(13)))
        self.assertTrue(app.claim_due_news_fetch(3600,self.when(18)))

    def test_suspension_and_manual_mode_remain_respected(self):
        app.suspend_research_schedule('2026-10-20T00:00:00Z')
        self.assertFalse(app.claim_due_research_cycle(self.saturday))
        app.suspend_research_schedule(None)
        app.configure_research_schedule('manual',60)
        self.assertFalse(app.claim_due_research_cycle(self.saturday))

    def test_weekend_entry_switch_is_independent_of_pacing(self):
        self.assertIsNotNone(app.weekend_entry_block_reason(self.saturday))
        app.configure_research_schedule('automatic',60,weekend_pacing_enabled=False)
        self.assertIsNotNone(app.weekend_entry_block_reason(self.saturday))
        self.assertEqual(app.get_research_schedule(self.saturday)['effective_interval_minutes'],60)
        app.configure_research_schedule('automatic',60,weekend_trading_enabled=True)
        self.assertIsNone(app.weekend_entry_block_reason(self.saturday))

    def test_configured_timezone_controls_weekend_boundary(self):
        friday=datetime(2026,10,16,23,tzinfo=timezone.utc)
        self.assertTrue(app.get_research_schedule(friday)['is_weekend'])
        app.configure_research_schedule('automatic',60,timezone='America/New_York')
        self.assertFalse(app.get_research_schedule(friday)['is_weekend'])

    def test_configuration_validation_and_preservation(self):
        for key,value in [('timezone','bad/zone'),('weekend_news_minutes',0),('weekend_research_minutes',True),('weekend_trading_enabled','false')]:
            with self.assertRaises(ValueError):app.configure_research_schedule('automatic',60,**{key:value})
        app.configure_research_schedule('automatic',60,weekend_news_minutes=240)
        app.configure_research_schedule('manual',60)
        self.assertEqual(app.get_research_schedule(self.saturday)['weekend_news_minutes'],240)

    def test_urgent_protection_retains_fast_maintenance(self):
        self.assertEqual(app.position_maintenance_interval(self.saturday),900)
        tid=self.trade();self.update(tid,status='RECONCILIATION_REQUIRED',broker_position_id='p')
        self.assertEqual(app.position_maintenance_interval(self.saturday),60)
        self.update(tid,status='EXIT_PENDING')
        self.assertEqual(app.position_maintenance_interval(self.saturday),900)
        app.PositionManagerAgent().recovery_manager().create(self.row(tid),'close',{},state='UNKNOWN')
        self.assertEqual(app.position_maintenance_interval(self.saturday),60)

    def test_weekend_entry_is_blocked_before_broker_calls(self):
        from unittest.mock import patch
        with patch.object(app,'weekend_entry_block_reason',return_value='Weekend entries disabled'):
            with self.assertRaisesRegex(ValueError,'Weekend entries disabled'):
                asyncio.run(app.place_analysis_candidate_demo_order(self.trade()))
        self.adapter.get_positions.assert_not_called();self.submit.assert_not_awaited()

    def test_weekend_schedule_preview_reaches_monday_boundary(self):
        saved={'research_schedule_timezone':'Europe/Stockholm'}
        now=datetime(2026,10,18,21,tzinfo=timezone.utc)
        due=pacing.due_at(saved,'research',now,60,now)
        self.assertEqual(due,datetime(2026,10,18,22,tzinfo=timezone.utc))
