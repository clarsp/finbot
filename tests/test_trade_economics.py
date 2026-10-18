import copy
import json
import unittest
from trade_economics import position_economics

class EconomicsTests(unittest.TestCase):
    def trade(self):
        return dict(side='LONG',entry_price=100,quantity=4,leverage=2,allocated_capital=200,
                    take_profit_price=103,stop_loss_price=96,status='OPEN',broker_fees=None,
                    policy_json=json.dumps({'estimated_costs':2,'sizing':{'units':4},
                      'broker_observation':{'units':4,'entry_price':100,'current_price':102,
                        'margin':200,'leverage':2,'reported_running_pnl':8,'reported_fees':1}}))

    def test_leverage_is_not_multiplied_twice_and_stop_includes_costs(self):
        result=position_economics(self.trade())
        self.assertEqual(result['opening_exposure'],400)
        self.assertEqual(result['current_exposure'],408)
        self.assertEqual(result['margin'],200)
        target,stop=result['scenarios']
        self.assertEqual(target['profit_per_unit'],3)
        self.assertEqual(target['gross_position_pnl'],12)
        self.assertEqual(target['estimated_net_position_pnl'],10)
        self.assertEqual(target['estimated_return_on_margin_pct'],5)
        self.assertEqual(stop['estimated_net_position_pnl'],-18)
        self.assertEqual(result['broker_running_pnl'],8)  # Do not subtract fees again.
        self.assertIsNone(result['realized_pnl'])

    def test_short_price_direction_and_confirmed_realized_result(self):
        trade=self.trade();trade.update(side='SHORT',take_profit_price=97,stop_loss_price=104,status='CLOSED',realized_pnl=9)
        result=position_economics(trade)
        self.assertEqual(result['scenarios'][0]['estimated_net_position_pnl'],10)
        self.assertEqual(result['scenarios'][1]['estimated_net_position_pnl'],-18)
        self.assertEqual(result['realized_pnl'],9)
        self.assertEqual(result['current_exposure'],0)
        self.assertIsNone(result['broker_running_pnl'])

    def test_missing_costs_or_quote_never_become_zero_or_entry_mark(self):
        trade=self.trade();trade['policy_json']='{}'
        result=position_economics(trade)
        self.assertIsNone(result['current_exposure'])
        self.assertIsNone(result['broker_running_pnl'])
        self.assertIsNone(result['scenarios'][0]['estimated_net_position_pnl'])
        self.assertEqual(result['scenarios'][0]['gross_position_pnl'],12)

    def test_partial_position_scales_cost_forecast_to_actual_fill_units(self):
        trade=self.trade();trade['broker_fill_units']=2
        result=position_economics(trade)
        self.assertEqual(result['scenarios'][0]['estimated_costs'],1)
        self.assertEqual(result['scenarios'][0]['estimated_net_position_pnl'],5)
