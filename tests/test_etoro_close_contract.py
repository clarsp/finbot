import types
import unittest
from unittest.mock import Mock
from integrations import EtoroBrokerAdapter

class CloseContractTests(unittest.TestCase):
    def adapter(self,demo=True):
        a=object.__new__(EtoroBrokerAdapter)
        a.settings=types.SimpleNamespace(etoro_demo_trading=demo)
        a._request_json=Mock(return_value={'orderForClose':{'orderID':123},'token':'secret'})
        a.get_positions=Mock(return_value={'available':True,'positions':[{'position_id':12,'instrument_id':1041}]})
        return a

    def test_required_instrument_and_nested_order_id(self):
        a=self.adapter();result=a.close_position('12.0')
        a._request_json.assert_called_once_with('POST','/api/v1/trading/execution/demo/market-close-orders/positions/12',payload={'InstrumentID':1041,'UnitsToDeduct':None})
        self.assertEqual(result['order_id'],123)
        self.assertEqual(result['response']['token'],'[REDACTED]')

    def test_verified_instrument_does_not_need_another_portfolio_read(self):
        a=self.adapter();a.close_position('12',1041)
        a.get_positions.assert_not_called()

    def test_missing_instrument_or_position_never_submits(self):
        for positions in ([],[{'position_id':12,'instrument_id':None}],
                          [{'position_id':12,'instrument_id':1041}]*2):
            a=self.adapter();a.get_positions.return_value={'available':True,'positions':positions}
            with self.assertRaises(ValueError):a.close_position('12')
            a._request_json.assert_not_called()

    def test_live_close_stays_blocked(self):
        a=self.adapter(False)
        with self.assertRaises(ValueError):a.close_position('12',1041)
        a._request_json.assert_not_called()

    def test_reported_fee_field_is_normalized_without_inventing_a_mark(self):
        a=self.adapter()
        a._request_json.return_value={'positions':[{'positionID':12,'instrumentID':1041,
            'units':4,'openRate':100,'amount':200,'leverage':2,'unrealizedPnL':8,'totalFees':.5}]}
        # Use the real normalizer rather than the close test's mocked getter.
        result=EtoroBrokerAdapter.get_positions(a)['positions'][0]
        self.assertEqual(result['fees'],.5)
        self.assertEqual(result['unrealized_pnl'],8)
        self.assertIsNone(result['current_rate'])
