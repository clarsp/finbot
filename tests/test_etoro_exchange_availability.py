"""Search flags cannot alone authorize or permanently veto demo execution."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from brokers.etoro import EtoroBrokerAdapter
from brokers.contracts import instrument_blockers
from brokers.errors import BrokerPreflightError
from brokers.etoro_sessions import execution_session, CALENDARS, _calendar


class ExecutionCalendarTests(unittest.TestCase):
    def test_us_holidays_early_close_weekend_dst_and_boundaries(self):
        for stamp, expected in [
            ('2026-10-29T13:29:59+00:00', False),
            ('2026-10-29T13:30:00+00:00', True),
            ('2026-10-29T19:59:59+00:00', True),
            ('2026-10-29T20:00:00+00:00', False),
            ('2026-10-31T15:00:00+00:00', False),
            ('2026-11-02T13:30:00+00:00', False),
            ('2026-11-02T14:30:00+00:00', True),
            ('2026-11-26T16:00:00+00:00', False),
            ('2026-11-27T17:59:00+00:00', True),
            ('2026-11-27T18:00:00+00:00', False),
        ]:
            with self.subTest(stamp=stamp):
                self.assertEqual(execution_session({'exchange_name': 'NASDAQ'},
                    datetime.fromisoformat(stamp))['is_regular_session'], expected)

    def test_unknown_exchange_or_unavailable_calendar_stays_closed(self):
        self.assertFalse(execution_session({'exchange_name': 'UNKNOWN'})['is_regular_session'])
        with patch('brokers.etoro_sessions._calendar', side_effect=RuntimeError('unavailable')):
            self.assertFalse(execution_session({'exchange_name': 'NASDAQ'})['is_regular_session'])

    def test_mapped_calendars_exist_and_hong_kong_midday_break_is_closed(self):
        for name in set(CALENDARS.values()):
            with self.subTest(name=name): self.assertIsNotNone(_calendar(name))
        self.assertFalse(execution_session({'exchange_name': 'HONG KONG'},
            datetime.fromisoformat('2026-10-29T04:30:00+00:00'))['is_regular_session'])


class ExchangeAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.raw = dict(instrumentId=1004, internalSymbolFull='MSFT',
            internalAssetClassName='Stocks', internalExchangeName='NASDAQ',
            isCurrentlyTradable=True, isActiveInPlatform=True,
            isExchangeOpen=False, isBuyEnabled=True, isSellEnabled=True)
        self.eligibility = dict(instrumentId=1004, symbol='MSFT', allowOpenPosition=True,
            allowEntryOrders=True, leverageConfigs=[dict(direction='long',
            leverageValues=[1, 2], isPotential=False), dict(direction='short',
            leverageValues=[1, 2], isPotential=False)])
        self.rate = dict(instrumentId=1004, bid=100, ask=100.1, quoteType='realtime',
            date=datetime.now(timezone.utc).isoformat())
        self.session = patch('brokers.etoro.execution_session',
            return_value={'is_regular_session': True})
        self.session_mock = self.session.start()
        self.addCleanup(self.session.stop)

    def adapter(self):
        a = object.__new__(EtoroBrokerAdapter)
        a.settings = SimpleNamespace(etoro_demo_trading=True)
        a._find_instrument = Mock(side_effect=lambda symbol: copy.deepcopy(self.raw))
        a._request_json = Mock(side_effect=lambda *args, **kw: {
            'eligibilities': [copy.deepcopy(self.eligibility)], 'notFoundInstrumentIds': []})
        a.get_rate = Mock(side_effect=lambda instrument_id: copy.deepcopy(self.rate))
        return a

    def order(self, side='LONG', leverage=2):
        return dict(symbol='MSFT', side=side, units=1, leverage=leverage,
                    stop_loss_price=90 if side == 'LONG' else 110)

    def test_conflict_is_reconciled_without_rewriting_broker_flag(self):
        a = self.adapter()
        for side in ['LONG', 'SHORT']:
            resolved = a.resolve_instrument('MSFT')
            self.assertFalse(resolved['exchange_open'])
            self.assertFalse(resolved['instrument']['isExchangeOpen'])
            self.assertTrue(resolved['tradeable'])
            self.assertEqual(instrument_blockers(resolved, side), [])
            self.assertEqual(a._prepare_order(self.order(side))['instrumentId'], 1004)
        self.assertTrue(all(call.args[:2] == ('POST', '/api/v2/trading/info/demo/eligibility')
                            for call in a._request_json.call_args_list))

    def test_premarket_weekend_break_unknown_and_session_end_stay_blocked(self):
        a = self.adapter()
        self.session_mock.return_value = {'is_regular_session': False}
        with self.assertRaises(BrokerPreflightError): a._prepare_order(self.order())
        a._request_json.assert_not_called()
        self.session_mock.side_effect = [{'is_regular_session': True}, {'is_regular_session': False}]
        with self.assertRaises(BrokerPreflightError): a._prepare_order(self.order())

    def test_inactive_delisted_untradable_and_disabled_direction_stay_blocked(self):
        for field, value in [('isActiveInPlatform', False), ('isDelisted', True),
                             ('isCurrentlyTradable', False), ('isBuyEnabled', False)]:
            with self.subTest(field=field):
                original = self.raw.copy(); self.raw[field] = value
                with self.assertRaises(BrokerPreflightError): self.adapter()._prepare_order(self.order())
                self.raw = original

    def test_missing_denied_mismatched_and_potential_permissions_stay_blocked(self):
        for change in [dict(allowOpenPosition=False), dict(allowOpenPosition=None),
                       dict(instrumentId=123), dict(symbol='MSFT.RTH'),
                       dict(leverageConfigs=[]), dict(leverageConfigs=[dict(
                           direction='long', leverageValues=[2], isPotential=True)])]:
            with self.subTest(change=change):
                original = self.eligibility.copy(); self.eligibility.update(change)
                with self.assertRaises(BrokerPreflightError): self.adapter()._prepare_order(self.order())
                self.eligibility = original
        with self.assertRaises(BrokerPreflightError): self.adapter()._prepare_order(self.order(leverage=5))

    def test_bad_or_undated_quotes_never_resolve_the_conflict(self):
        for change in [dict(date=None), dict(date='invalid'), dict(quoteType='delayed'),
                       dict(quoteType=None), dict(bid=-1), dict(ask=99),
                       dict(date=(datetime.now(timezone.utc)-timedelta(seconds=61)).isoformat()),
                       dict(date=(datetime.now(timezone.utc)+timedelta(seconds=20)).isoformat())]:
            with self.subTest(change=change):
                original = self.rate.copy(); self.rate.update(change)
                with self.assertRaises(BrokerPreflightError): self.adapter()._prepare_order(self.order())
                self.rate = original

    def test_eligibility_failures_and_missing_duplicate_rows_fail_closed(self):
        for result in [{}, {'eligibilities': []}, {'eligibilities': [self.eligibility]*2},
                       {'eligibilities': [self.eligibility], 'notFoundInstrumentIds': [1004]}]:
            a = self.adapter(); a._request_json.side_effect = None; a._request_json.return_value = result
            with self.assertRaises(BrokerPreflightError): a._prepare_order(self.order())
        a = self.adapter(); a._request_json.side_effect = TimeoutError('timeout')
        with self.assertRaises(BrokerPreflightError): a._prepare_order(self.order())

    def test_preflight_rechecks_after_previously_successful_quote(self):
        a = self.adapter(); self.assertTrue(a.get_quote('MSFT')['executable'])
        self.eligibility['allowOpenPosition'] = False
        with self.assertRaises(BrokerPreflightError): a._prepare_order(self.order())

    def test_real_trading_never_uses_demo_permission(self):
        a = self.adapter(); a.settings.etoro_demo_trading = False
        self.assertFalse(a.resolve_instrument('MSFT')['tradeable'])
        a._request_json.assert_not_called()
        with self.assertRaises(ValueError): a._prepare_order(self.order())

    def test_actual_v2_date_is_used_for_normal_quote_freshness(self):
        self.raw['isExchangeOpen'] = True
        self.rate['date'] = (datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
        a = self.adapter(); quote = a.get_quote('MSFT')
        self.assertFalse(quote['executable'])
        self.assertIn('stale', quote['reason'])
        a._get_rate = Mock(return_value=self.rate)
        self.assertEqual(EtoroBrokerAdapter.get_rate(a, 1004)['timestamp'], self.rate['date'])
