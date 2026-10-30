import asyncio,unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
import pandas as pd
import integrations,strategy
from market_instruments import reference_symbol,broker_symbol,sp500_index_supported

class SP500Tests(unittest.TestCase):
 def test_only_verified_sp500_index_contract_is_added(self):
  self.assertTrue(sp500_index_supported('SPX500','indices',27))
  self.assertFalse(sp500_index_supported('NSDQ100','indices',28))
  self.assertFalse(sp500_index_supported('SPX500','indices',999))
  a=object.__new__(integrations.EtoroBrokerAdapter)
  a._find_instrument=Mock(return_value={'internalAssetClassName':'Indices','internalInstrumentId':27,'isCurrentlyTradable':True,'internalExchangeName':'CFD'})
  r=a.resolve_instrument('SPX500');self.assertTrue(r['supported_asset']);self.assertEqual(r['currency'],'USD')
 def test_reference_prices_and_broker_symbols_are_separate(self):
  self.assertEqual(reference_symbol('SPX500'),'^GSPC');self.assertEqual(broker_symbol('^GSPC'),'SPX500')
  self.assertEqual(reference_symbol('SPY'),'SPY');self.assertEqual(reference_symbol('USOIL'),'CL=F')
  fake=Mock();fake.history.return_value=pd.DataFrame()
  with patch.object(strategy.yf,'Ticker',return_value=fake) as ticker:strategy.load_daily_bars('SPX500')
  ticker.assert_called_once_with('^GSPC')
 def test_index_context_does_not_fetch_company_statements(self):
  instrument=Mock();instrument.info={'currency':'USD','exchangeTimezoneName':'America/New_York'};instrument.get_history_metadata.return_value={}
  with patch.object(integrations.yf,'Ticker',return_value=instrument):context=integrations._fetch_company_fundamentals('SPX500')
  self.assertFalse(context['company_financials_applicable']);self.assertEqual(context['instrument_family'],'sp500');self.assertNotIn('total_revenue_ttm',context)
 def test_news_discovery_includes_index_and_etfs_within_existing_limit(self):
  settings=SimpleNamespace(news_symbol_limit=40,news_fallback_tickers=['MSFT']);provider=integrations.YahooFinanceNewsProvider(settings)
  with patch.object(provider,'_screen_tickers',return_value=['AAPL']),patch.object(provider,'_fetch_ticker_news',return_value=[]) as fetch:
   asyncio.run(provider.fetch_news())
  self.assertEqual(fetch.call_args.args[0],['SPX500','SPY','VOO','IVV','AAPL'])
 def test_index_strategy_requires_direct_market_evidence(self):
  import copy,makemoney
  rules=copy.deepcopy(makemoney.DEFAULT_AGENT_RULES['strategist'])
  candidate={'ticker':'SPX500','company_name':'S&P 500 Index','confidence':.95,'price':7000,'signal':'bullish','company_news_sentiment':'bullish','fundamentals':{'instrument_family':'sp500','data_status':'available','company_financials_applicable':False},'ai_analysis':{'company_relevance':'indirect'},'daily_trend_context':{'available':True,'trend_state':'bullish','sma50':6800,'atr14':70}}
  rejected=makemoney.evaluate_strategist_gate(candidate,'LONG',[],rules)
  self.assertFalse(rejected['accepted']);self.assertTrue(any('direct index-wide' in r for r in rejected['rejection_reasons']))
  candidate['ai_analysis']['company_relevance']='direct'
  accepted=makemoney.evaluate_strategist_gate(candidate,'LONG',[],rules)
  self.assertTrue(accepted['accepted']);self.assertFalse(accepted['fundamentals_strong']);self.assertFalse(accepted['fundamentals_weak'])
