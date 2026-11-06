import unittest
import workflow_store


class EvidenceCompactionTests(unittest.TestCase):
    def test_strategy_keeps_one_canonical_candidate(self):
        trend={'price_structure':{'resistance':[{'low':i} for i in range(20)]}}
        candidate={'daily_trend_context':trend,'market_quote':{'instrument':{'leverageConfigs':[1,2]}},'fundamentals':{'beta':2}}
        detail={'candidate_context':candidate,'decision_gate':{**candidate},'analysis':{**candidate,'decision_gate':{**candidate}}}
        compact=workflow_store.compact_trade_event_detail('strategy',detail)
        self.assertEqual(compact['candidate_context'],candidate)
        self.assertEqual(compact['decision_gate']['daily_trend_context']['evidence_ref'],'candidate_context.daily_trend_context')
        self.assertEqual(compact['analysis']['decision_gate']['evidence_ref'],'decision_gate')

    def test_risk_references_duplicate_broker_and_structure(self):
        structure={'resistance':[{'low':100}]};instrument={'leverageConfigs':[1,2]}
        detail={'daily_trend_context':{'price_structure':structure},'broker_instrument':instrument,
                'broker_quote':{'instrument':instrument},'protection':{'target_selection':{'structure':structure}}}
        compact=workflow_store.compact_trade_event_detail('risk',detail)
        self.assertEqual(compact['broker_quote']['instrument']['evidence_ref'],'broker_instrument')
        self.assertEqual(compact['protection']['target_selection']['structure']['evidence_ref'],'daily_trend_context.price_structure')
