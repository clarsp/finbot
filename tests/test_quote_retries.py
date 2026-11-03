import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from brokers.quotes import assess_rate, read_rate_with_retries, MissingQuote
from brokers.etoro import EtoroBrokerAdapter
from brokers.errors import BrokerAPIError


class QuoteRetryTests(unittest.TestCase):
    def rate(self, seconds=0, **overrides):
        return {'instrumentId':1004, 'bid':100, 'ask':100.1, 'quoteType':'realtime',
                'date':(datetime.now(timezone.utc)-timedelta(seconds=seconds)).isoformat(), **overrides}

    def test_stale_then_fresh_uses_one_retry_and_keeps_actual_broker_date(self):
        read=Mock(side_effect=[self.rate(90), self.rate()]); sleep=Mock()
        result=read_rate_with_retries(read,sleep)
        self.assertEqual(result['quote_validation']['code'],'fresh')
        self.assertEqual(len(result['quote_validation']['attempts']),2)
        self.assertEqual(result['timestamp'],result['date']);sleep.assert_called_once_with(2)

    def test_two_retries_exhausted_or_successful_on_third_read(self):
        for third in [self.rate(90), self.rate()]:
            read=Mock(side_effect=[self.rate(90),self.rate(90),third]);sleep=Mock()
            result=read_rate_with_retries(read,sleep)
            self.assertEqual(read.call_count,3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list],[2,5])
            self.assertEqual(result['quote_validation']['retries_used'],2)
            self.assertEqual(result['quote_validation']['code'],assess_rate(third)['code'])

    def test_missing_invalid_delayed_undated_and_future_quotes_do_not_retry(self):
        for changes,code in [({'bid':None},'missing_quote'),({'ask':99},'invalid_quote'),
                ({'date':None},'missing_timestamp'),({'quoteType':'delayed'},'delayed_quote'),
                ({'date':(datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat()},'clock_skew')]:
            read=Mock(return_value=self.rate(**changes));sleep=Mock()
            self.assertEqual(read_rate_with_retries(read,sleep)['quote_validation']['code'],code)
            self.assertEqual(read.call_count,1);sleep.assert_not_called()

    def test_request_failure_and_rate_limit_are_not_retried_or_hidden_as_stale(self):
        for error in [TimeoutError('timeout'),BrokerAPIError('limited',status_code=429,retry_after_seconds=20)]:
            read=Mock(side_effect=error);sleep=Mock()
            with self.assertRaises(type(error)):read_rate_with_retries(read,sleep)
            self.assertEqual(read.call_count,1);sleep.assert_not_called()
            self.assertEqual(len(error.quote_attempts),1)

    def test_exchange_reconciliation_and_quote_share_one_observation(self):
        a=object.__new__(EtoroBrokerAdapter);a.settings=SimpleNamespace(etoro_demo_trading=True)
        a._find_instrument=Mock(return_value=dict(instrumentId=1004,internalSymbolFull='MSFT',
            internalAssetClassName='Stocks',internalExchangeName='NASDAQ',isCurrentlyTradable=True,
            isActiveInPlatform=True,isExchangeOpen=False))
        a._request_json=Mock(return_value={'eligibilities':[{'instrumentId':1004,'symbol':'MSFT','allowOpenPosition':True}]})
        a._get_rate=Mock(side_effect=[self.rate(90),self.rate()])
        with patch('brokers.etoro.execution_session',return_value={'is_regular_session':True}),patch('brokers.quotes.time.sleep'):
            quote=a.get_quote('MSFT')
        self.assertTrue(quote['executable'],quote['reason'])
        self.assertEqual(a._get_rate.call_count,2)
        self.assertEqual(quote['quote_validation']['retries_used'],1)
        self.assertEqual(quote['instrument']['execution_availability']['quote']['quote_at'],quote['quote_at'])
        self.assertTrue(all('/execution/' not in call.args[1] for call in a._request_json.call_args_list))

    def test_failed_stale_retries_are_labelled_stale_during_open_session(self):
        a=object.__new__(EtoroBrokerAdapter);a.settings=SimpleNamespace(etoro_demo_trading=True)
        a._find_instrument=Mock(return_value=dict(instrumentId=1004,internalSymbolFull='MSFT',
            internalAssetClassName='Stocks',internalExchangeName='NASDAQ',isCurrentlyTradable=True,
            isActiveInPlatform=True,isExchangeOpen=False))
        a._request_json=Mock(return_value={'eligibilities':[{'instrumentId':1004,'symbol':'MSFT','allowOpenPosition':True}]})
        a._get_rate=Mock(return_value=self.rate(90))
        with patch('brokers.etoro.execution_session',return_value={'is_regular_session':True}),patch('brokers.quotes.time.sleep'):
            quote=a.get_quote('MSFT')
        self.assertFalse(quote['executable']);self.assertEqual(quote['quote_validation']['code'],'stale_quote')
        self.assertEqual(a._get_rate.call_count,3)
        self.assertNotIn('exchange is closed',quote['reason'])
        self.assertIn('2 retries',quote['reason'])

    def test_core_rechecks_broker_age_even_when_retrieval_time_is_fresh(self):
        import makemoney as app
        now=datetime.now(timezone.utc)
        quote=dict(available=True,executable=True,provider='eToro',fetched_at=now.isoformat(),
                   quote_at=(now-timedelta(seconds=59)).isoformat())
        self.assertTrue(app.is_fresh_broker_quote(quote,checked_at=now))
        self.assertFalse(app.is_fresh_broker_quote(quote,checked_at=now+timedelta(seconds=2)))

    def test_missing_quote_during_open_session_is_not_retried_by_outer_layers(self):
        a=object.__new__(EtoroBrokerAdapter);a.settings=SimpleNamespace(etoro_demo_trading=True)
        a._find_instrument=Mock(return_value=dict(instrumentId=1004,internalSymbolFull='MSFT',
            internalAssetClassName='Stocks',internalExchangeName='NASDAQ',isCurrentlyTradable=True,
            isActiveInPlatform=True,isExchangeOpen=False))
        a._request_json=Mock(return_value={'eligibilities':[{'instrumentId':1004,'symbol':'MSFT','allowOpenPosition':True}]})
        a._get_rate=Mock(side_effect=MissingQuote('No quote returned for instrument 1004'))
        with patch('brokers.etoro.execution_session',return_value={'is_regular_session':True}):
            quote=a.get_quote('MSFT')
        self.assertEqual(a._get_rate.call_count,1)
        self.assertEqual(quote['status'],'missing_quote')
        self.assertEqual(quote['quote_validation']['code'],'missing_quote')
