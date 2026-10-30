"""Broker identity translation never changes researched symbols or asset identity."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from brokers.etoro import EtoroBrokerAdapter
from brokers.etoro_symbols import select_instrument,search_symbol
from brokers.errors import BrokerPreflightError


def equity(**changes):
    return dict(instrumentId=1014,internalSymbolFull='CVX.US',internalAssetClassName='Stocks',
        internalExchangeName='NYSE',isActiveInPlatform=True,isCurrentlyTradable=True,
        isExchangeOpen=True,isBuyEnabled=True,isSellEnabled=True,**changes)


class SymbolMappingTests(unittest.TestCase):
    def adapter(self,items):
        a=object.__new__(EtoroBrokerAdapter);a.settings=SimpleNamespace(etoro_demo_trading=True)
        a._request_json=Mock(return_value={'items':items});return a

    def test_cvx_maps_verified_equity_not_the_exact_crypto_ticker(self):
        stock=equity();crypto={**stock,'instrumentId':100512,'internalSymbolFull':'CVX','internalAssetClassName':'Crypto','internalExchangeName':'Digital Currency'}
        paris={**stock,'instrumentId':13287,'internalSymbolFull':'CVX.PA','internalExchangeName':'Euronext Paris'}
        for rows in ([crypto,paris,stock],[stock,crypto,paris]):
            before=copy.deepcopy(rows);a=self.adapter(rows);i=a.resolve_instrument('CVX')
            self.assertEqual(a._request_json.call_args.kwargs['params']['internalSymbolFull'],'CVX.US')
            self.assertEqual(i['symbol'],'CVX');self.assertEqual(i['research_symbol'],'CVX')
            self.assertEqual(i['broker_symbol'],'CVX.US');self.assertEqual(i['instrument_id'],1014)
            self.assertEqual(i['symbol_resolution']['method'],'verified_alias');self.assertTrue(i['supported_asset'])
            self.assertEqual(rows,before)

    def test_quote_and_order_use_the_same_mapping_and_preserve_research_symbol(self):
        a=self.adapter([equity()]);a.get_rate=Mock(return_value={'bid':150,'ask':150.1})
        q=a.get_quote('CVX');self.assertTrue(q['executable'])
        self.assertEqual(q['ticker'],'CVX');self.assertEqual(q['quote_symbol'],'CVX.US');self.assertEqual(q['instrument']['broker_symbol'],'CVX.US')
        a.get_rate.assert_called_once_with(1014)
        payload=a._prepare_order({'symbol':'CVX','side':'LONG','units':1,'leverage':2,'stop_loss_price':140,'take_profit_price':160})
        self.assertEqual(payload['instrumentId'],1014)
        self.assertTrue(all(call.args[0]=='GET' for call in a._request_json.call_args_list))

    def test_cost_estimates_use_the_validated_broker_symbol(self):
        a=self.adapter([equity()])
        a.estimate_order_costs('CVX','LONG',1,2)
        call=a._request_json.call_args
        self.assertEqual(call.args[:2],('POST','/api/v2/trading/info/demo/costs'))
        self.assertEqual(call.kwargs['payload']['symbol'],'CVX.US')
        self.assertEqual(a._request_json.call_args_list[0].args[0],'GET')

    def test_cost_estimate_with_bad_identity_does_not_send_the_cost_request(self):
        a=self.adapter([{**equity(),'internalAssetClassName':'Crypto'}])
        with self.assertRaisesRegex(ValueError,'identity mismatch'):
            a.estimate_order_costs('CVX','LONG',1,2)
        self.assertTrue(all(call.args[0]=='GET' for call in a._request_json.call_args_list))

    def test_alias_requires_id_asset_and_exchange_to_match(self):
        for change in ({'instrumentId':9999},{'internalAssetClassName':'Crypto'},
                       {'internalExchangeName':'Euronext Paris'},{'internalAssetClassName':None},
                       {'internalExchangeName':None}):
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'identity mismatch'):
                select_instrument('CVX',[{**equity(),**change}])

    def test_missing_alias_target_does_not_fall_back_to_crypto_or_other_listing(self):
        a=self.adapter([{**equity(),'instrumentId':100512,'internalSymbolFull':'CVX','internalAssetClassName':'Crypto'}])
        q=a.get_quote('CVX');self.assertFalse(q['available'])
        self.assertIn('expected broker symbol CVX.US',q['reason'])
        with self.assertRaises(BrokerPreflightError):a.submit_order({'symbol':'CVX','units':1,'leverage':1})
        self.assertTrue(all(call.args[0]=='GET' for call in a._request_json.call_args_list))

    def test_ambiguous_matches_stop_instead_of_selecting_first(self):
        first={**equity(),'internalSymbolFull':'ACME'};other={**first,'instrumentId':123}
        for symbol,rows in [('ACME',[first,other]),('CVX',[equity(),{**equity(),'instrumentId':123}])]:
            with self.subTest(symbol=symbol),self.assertRaisesRegex(ValueError,'Ambiguous'):
                select_instrument(symbol,rows)
        self.assertEqual(select_instrument('ACME',[first,copy.deepcopy(first)])['instrumentId'],1014)

    def test_unmapped_symbols_do_not_acquire_guessed_suffixes(self):
        with self.assertRaisesRegex(ValueError,'No unverified alias'):
            select_instrument('SSAB-A.ST',[{**equity(),'internalSymbolFull':'SSAB-A'}])
        self.assertEqual(search_symbol('TSM'),'TSM')

    def test_mapping_does_not_override_closed_or_disabled_instrument_flags(self):
        a=self.adapter([{**equity(),'isExchangeOpen':False,'isBuyEnabled':False}])
        i=a.resolve_instrument('CVX');self.assertFalse(i['exchange_open']);self.assertFalse(i['buy_enabled'])
        with self.assertRaises(BrokerPreflightError):a.submit_order({'symbol':'CVX','units':1,'leverage':1})
        self.assertTrue(all(call.args[0]=='GET' for call in a._request_json.call_args_list))
