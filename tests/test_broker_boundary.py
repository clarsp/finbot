"""Provider contract and eToro state semantics; no network or database writes."""
import ast
import copy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from brokers.contracts import BrokerAdapter, instrument_blockers
from brokers.etoro import EtoroBrokerAdapter
from brokers.errors import BrokerPreflightError
import brokers.factory as factory


class BrokerBoundaryTests(unittest.TestCase):
    def adapter(self, **fields):
        raw = dict(instrumentId=9287,internalSymbolFull='RIVN',internalAssetClassName='Stocks',
                   internalExchangeName='NASDAQ',isCurrentlyTradable=True,isExchangeOpen=True,
                   isActiveInPlatform=True,isDelisted=False,isBuyEnabled=True,isSellEnabled=True)
        raw.update(fields)
        a = object.__new__(EtoroBrokerAdapter)
        a.settings = SimpleNamespace(etoro_demo_trading=True)
        a._request_json = Mock(return_value={'items':[raw]})
        return a

    def test_closed_inactive_but_listed_identity_is_resolved_without_guessing_cause(self):
        a=self.adapter(isCurrentlyTradable=False,isExchangeOpen=False,isActiveInPlatform=False)
        instrument=a.resolve_instrument('RIVN')
        self.assertTrue(instrument['resolved']);self.assertFalse(instrument['delisted'])
        reasons=instrument_blockers(instrument)
        self.assertTrue(any('exchange is closed' in r for r in reasons))
        self.assertTrue(any('inactive on its platform' in r for r in reasons))
        self.assertFalse(any('delisted' in r for r in reasons))
        self.assertIn('fields',a._request_json.call_args.kwargs['params'])

    def test_closed_instrument_can_have_indicative_quote_but_no_order_is_sent(self):
        a=self.adapter(isCurrentlyTradable=False,isExchangeOpen=False)
        a.get_rate=Mock(return_value={'bid':10,'ask':10.1})
        quote=a.get_quote('RIVN')
        self.assertTrue(quote['available']);self.assertFalse(quote['executable'])
        self.assertEqual(quote['status'],'indicative')
        with self.assertRaises(BrokerPreflightError) as caught:
            a.submit_order({'symbol':'RIVN','side':'LONG','units':1,'leverage':1})
        self.assertFalse(caught.exception.request_sent)
        self.assertTrue(all(call.args[0]=='GET' for call in a._request_json.call_args_list))

    def test_missing_status_is_unknown_and_does_not_authorize_submission(self):
        a=self.adapter(isCurrentlyTradable=None)
        self.assertIn('Current broker tradability is unknown.',instrument_blockers(a.resolve_instrument('RIVN')))
        with self.assertRaises(BrokerPreflightError):a.submit_order({'symbol':'RIVN','units':1,'leverage':1})

    def test_delisting_and_direction_restrictions_are_distinct(self):
        i=self.adapter(isDelisted=True,isSellEnabled=False).resolve_instrument('RIVN')
        self.assertIn('Broker reports the instrument is delisted.',instrument_blockers(i))
        self.assertIn('Broker has disabled short orders for this instrument.',instrument_blockers(i,'SHORT'))

    def test_success_failure_invalid_delayed_and_stale_quotes_remain_distinct(self):
        for rate,available,executable,status in [({'bid':10,'ask':11},True,True,'executable'),
                ({'bid':11,'ask':10},False,False,'invalid_quote'),
                ({'bid':10,'ask':11,'quoteType':'delayed'},True,False,'indicative'),
                ({'bid':10,'ask':11,'timestamp':(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()},True,False,'indicative')]:
            a=self.adapter();a.get_rate=Mock(return_value={'date':rate.get('timestamp') or datetime.now(timezone.utc).isoformat(),'quoteType':'realtime',**rate});q=a.get_quote('RIVN')
            self.assertEqual((q['available'],q['executable'],q['status']),(available,executable,status))
        a=self.adapter();a.get_rate=Mock(side_effect=TimeoutError('rates read timed out'))
        q=a.get_quote('RIVN');self.assertEqual(q['status'],'request_failed');self.assertIn('timed out',q['reason'])

    def test_native_stop_type_is_explicit_and_demo_only(self):
        a=self.adapter()
        a.modify_position('p',95,110,stop_type='trailing',request_id='ref')
        self.assertEqual(a._request_json.call_args.kwargs['payload'],{'stopLossRate':95,'stopLossType':'trailing','takeProfitRate':110})
        a.modify_position('p',95,None)
        self.assertEqual(a._request_json.call_args.kwargs['payload'],{'stopLossRate':95,'stopLossType':'fixed'})
        with self.assertRaises(ValueError):a.modify_position('p',95,None,stop_type='unsupported')
        a.settings.etoro_demo_trading=False
        with self.assertRaises(ValueError):a.modify_position('p',95,None,stop_type='trailing')

    def test_portfolio_confirms_trailing_type_without_guessing_missing_flags(self):
        a=self.adapter();a._request_json.return_value={'positions':[{'positionId':1,'isBuy':False,'stopLossRate':105,'isTslEnabled':True},{'positionId':2,'isTslEnabled':False},{'positionId':3}]}
        positions=a.get_positions()['positions']
        self.assertEqual([p['stop_loss_type'] for p in positions],['trailing','fixed',None])
        self.assertEqual(positions[0]['side'],'SHORT')

    def test_provider_selection_is_fail_closed_and_new_adapter_does_not_modify_core(self):
        with self.assertRaises(ValueError):factory.create_broker_adapter(SimpleNamespace(broker_provider='ibkr'))
        # A different implementation satisfies the same public lifecycle contract.
        methods={name:(lambda self,*a,**k: {}) for name in BrokerAdapter.__dict__ if not name.startswith('_')}
        independent=type('IndependentBroker',(),methods)()
        with patch.dict(factory._FACTORIES,clear=True),patch.object(factory,'_DEMO_READY',set()):
            factory.register_broker('alternative',lambda settings:independent)
            settings=SimpleNamespace(broker_provider='alternative')
            self.assertIs(factory.create_broker_adapter(settings),independent)
            self.assertFalse(factory.demo_execution_enabled(settings))
            with self.assertRaises(ValueError):factory.register_broker('alternative',lambda settings:independent)

    def test_alternative_broker_quote_runs_through_core_freshness_gate(self):
        import makemoney
        q={'provider':'alternative','available':True,'executable':True,'fetched_at':datetime.now(timezone.utc).isoformat()}
        self.assertTrue(makemoney.is_fresh_broker_quote(q))
        self.assertFalse(makemoney.is_fresh_broker_quote({**q,'executable':False}))

    def test_etoro_adapter_has_no_application_or_integrations_import(self):
        source=(Path(__file__).resolve().parents[1]/'brokers/etoro.py').read_text()
        modules=[n.module or '' for n in ast.walk(ast.parse(source)) if isinstance(n,ast.ImportFrom)]
        self.assertFalse(any(m in {'integrations','makemoney','streamlit_app'} for m in modules))

    def test_order_dispatch_for_a_different_broker_uses_only_its_adapter(self):
        import integrations
        settings=SimpleNamespace(broker_provider='alternative')
        methods={name:(lambda self,*a,**k: {}) for name in BrokerAdapter.__dict__ if not name.startswith('_')}
        adapter=type('IndependentBroker',(),methods)()
        adapter.submit_order=Mock(return_value={'order_id':'alternative-1','provider':'alternative'})
        with patch.dict(factory._FACTORIES,{'alternative':lambda settings:adapter},clear=True),patch.object(integrations.IntegrationSettings,'from_environment',return_value=settings):
            result=integrations._submit_broker_order({'symbol':'TEST','side':'LONG','units':1})
        self.assertEqual(result['provider'],'alternative')
        adapter.submit_order.assert_called_once_with({'symbol':'TEST','side':'LONG','units':1})

    def test_changing_broker_setting_cannot_reuse_another_providers_position_ids(self):
        import sqlite3
        from brokers.contracts import validate_runtime_binding
        c=sqlite3.connect(':memory:')
        c.executescript("""CREATE TABLE trading_profiles(id INTEGER,broker_account_id INTEGER,status TEXT);
        CREATE TABLE broker_accounts(id INTEGER,connection_id INTEGER);
        CREATE TABLE broker_connections(id INTEGER,provider TEXT);
        INSERT INTO trading_profiles VALUES(1,1,'active');INSERT INTO broker_accounts VALUES(1,1);
        INSERT INTO broker_connections VALUES(1,'etoro');""")
        validate_runtime_binding(c,'etoro')
        with self.assertRaisesRegex(ValueError,'migration'):validate_runtime_binding(c,'alternative')

    def test_all_order_preflight_failures_are_distinguished_from_broker_submission(self):
        a=self.adapter()
        for invalid in ({'symbol':'RIVN','units':-1,'leverage':1},
                        {'symbol':'RIVN','units':1,'leverage':1.5}):
            with self.assertRaises(BrokerPreflightError) as error:a.submit_order(invalid)
            self.assertFalse(error.exception.request_sent)
        self.assertTrue(all(call.args[0]=='GET' for call in a._request_json.call_args_list))

    def test_session_context_exposes_generic_indicative_state_with_legacy_alias(self):
        from market_sessions import build_market_session_context
        ctx=build_market_session_context(quote={'available':True,'executable':False})
        self.assertEqual(ctx['broker_execution_state'],'QUOTE_INDICATIVE')
        self.assertFalse(ctx['broker_quote_executable'])
        self.assertEqual(ctx['etoro_execution_state'],ctx['broker_execution_state'])

    def test_unimplemented_provider_cannot_fall_through_to_simulated_trading(self):
        import asyncio,makemoney
        with patch.object(makemoney.IntegrationSettings,'from_environment',return_value=SimpleNamespace(broker_provider='ibkr')),patch.object(makemoney,'get_agent_rules',side_effect=AssertionError('must not enter simulation')):
            result=asyncio.run(makemoney.run_trading_cycle(use_openai=False))
        self.assertEqual(result['status'],'broker_disabled')
        self.assertEqual(result['trades'],[])
