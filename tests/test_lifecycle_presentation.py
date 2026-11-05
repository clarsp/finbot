import copy
import unittest
from lifecycle_presentation import group_shadow_events


class ShadowPresentationTests(unittest.TestCase):
    def test_preserves_all_evidence_and_other_lifecycle_steps(self):
        events = [{'id': 1, 'stage': 'execution'},
                  {'id': 2, 'stage': 'shadow', 'details': {'variant': '1T'}},
                  {'id': 3, 'stage': 'protection', 'decision': 'rejected'},
                  {'id': 4, 'stage': 'shadow', 'details': {'variant': '2T'}},
                  {'id': 5, 'stage': 'closure'}]
        before = copy.deepcopy(events)
        result = group_shadow_events(events)
        self.assertEqual([e['stage'] for e in result], ['execution', 'shadow', 'protection', 'closure'])
        self.assertEqual(result[1]['id'], 4)
        self.assertEqual([e['id'] for e in result[1]['shadow_observations']], [2, 4])
        self.assertEqual(events, before)
        self.assertEqual(result[2]['decision'], 'rejected')

    def test_no_shadow_or_single_shadow(self):
        self.assertEqual(group_shadow_events([]), [])
        self.assertEqual(group_shadow_events([{'stage': 'risk'}]), [{'stage': 'risk'}])
        result = group_shadow_events([{'id': 1, 'stage': 'shadow'}])
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]['shadow_observations']), 1)

    def test_repeated_reassessments_share_one_evidence_trail(self):
        events=[{'id':1,'stage':'execution'},{'id':2,'stage':'reassessment','details':{'decision':'hold'}},
                {'id':3,'stage':'shadow'},{'id':4,'stage':'reassessment','details':{'decision':'review_required'}}]
        result=group_shadow_events(events)
        reviews=[e for e in result if e['stage']=='reassessment']
        self.assertEqual(len(reviews),1)
        self.assertEqual([e['id'] for e in reviews[0]['reassessments']],[2,4])
        self.assertEqual(len(events),4)

    def test_repeated_position_management_stages_share_one_visual_step(self):
        events=[
            {'id':1,'stage':'execution','decision':'submitted'},
            {'id':2,'stage':'profit_trailing','decision':'tracking','message':'First observation'},
            {'id':3,'stage':'recovery','decision':'attention','message':'Retry required'},
            {'id':4,'stage':'profit_trailing','decision':'tracking','message':'Second observation'},
            {'id':5,'stage':'recovery','decision':'accepted','message':'Recovered'},
            {'id':6,'stage':'reconciliation','decision':'accepted'},
        ]
        before=copy.deepcopy(events)
        result=group_shadow_events(events)
        self.assertEqual([e['stage'] for e in result],['execution','profit_trailing','recovery','reconciliation'])
        trailing=next(e for e in result if e['stage']=='profit_trailing')
        recovery=next(e for e in result if e['stage']=='recovery')
        self.assertEqual([e['id'] for e in trailing['management_observations']],[2,4])
        self.assertEqual([e['decision'] for e in recovery['management_observations']],['attention','accepted'])
        self.assertEqual(recovery['decision'],'accepted')
        self.assertEqual(events,before)


class BrokerStatusPresentationTests(unittest.TestCase):
    def test_preflight_failure_is_not_broker_rejection_or_pending(self):
        from lifecycle_presentation import lifecycle_status_label
        self.assertEqual(lifecycle_status_label({'status':'ANALYSIS_ONLY'}), 'Not submitted · execution blocked')
        self.assertEqual(lifecycle_status_label({'status':'DEFERRED_CAPACITY'}), 'Not submitted · capacity blocked')
        self.assertEqual(lifecycle_status_label({'status':'REJECTED_EXECUTION'}), 'Execution rejected')
        for evidence in ('broker_rejected', 'broker_confirmed_rejection'):
            self.assertEqual(lifecycle_status_label({'status':'REJECTED_EXECUTION','reconciliation_status':evidence}), 'Broker rejected')

    def test_accepted_pending_and_uncertain_submission_are_distinct(self):
        from lifecycle_presentation import lifecycle_status_label
        self.assertEqual(lifecycle_status_label({'status':'SUBMITTED_PENDING_RECONCILIATION'}), 'Broker pending · awaiting fill / confirmation')
        self.assertIn('uncertain',lifecycle_status_label({'status':'RECONCILIATION_REQUIRED'}))
        self.assertEqual(lifecycle_status_label({'status':'REJECTED_RISK'}), 'Rejected risk')

    def test_friday_research_expires_before_monday_and_bad_timestamps_fail_closed(self):
        from datetime import datetime,timezone
        from lifecycle_presentation import research_is_expired
        trade={'created_at':'2026-10-23 21:31:42'}
        self.assertFalse(research_is_expired(trade,24,datetime(2026,10,23,22,tzinfo=timezone.utc)))
        self.assertTrue(research_is_expired(trade,24,datetime(2026,10,26,14,tzinfo=timezone.utc)))
        self.assertTrue(research_is_expired({},24,datetime.now(timezone.utc)))
