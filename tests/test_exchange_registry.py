"""Automatic discovery verifies identity and persists data without inventing hours."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from brokers import exchange_registry as registry
from brokers.etoro import EtoroBrokerAdapter
from brokers.etoro_sessions import execution_session


class ExchangeRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'exchange_calendar_registry.json'
        env = patch.dict(os.environ, FINBOT_DB_PATH=str(Path(self.tmp.name)/'finbot.db'))
        env.start(); self.addCleanup(env.stop)
        self.request = Mock(return_value={'exchangeInfo': [
            {'exchangeID': 12, 'exchangeDescription': 'SIX'},
            {'exchangeID': 14, 'exchangeDescription': 'Oslo Stock Exchange'},
            {'exchangeID': 10, 'exchangeDescription': 'Bolsa De Madrid'},
            {'exchangeID': 58, 'exchangeDescription': 'XJSE'},
            {'exchangeID': 99, 'exchangeDescription': 'Unknown Venue'},
            {'exchangeID': 40, 'exchangeDescription': 'CME'}]})

    def instrument(self, label='SIX', exchange_id=12):
        return {'exchange_name': label, 'exchange_id': exchange_id}

    def test_registered_library_alias_discovers_saves_and_survives_process_cache_reset(self):
        instrument = self.instrument()
        first = registry.discover_calendar(instrument, self.request)
        self.assertEqual((first['status'], first['calendar']), ('verified', 'SIX'))
        self.assertEqual(first['canonical_calendar'], 'XSWX')
        self.assertTrue(self.path.exists())
        with patch.dict(registry._directory, clear=True), patch.dict(registry._memory, clear=True):
            second = registry.discover_calendar(instrument, Mock(side_effect=AssertionError('no refresh needed')))
        self.assertTrue(second['cached'])
        self.assertEqual(second['broker_exchange']['exchangeID'], 12)
        self.request.assert_called_once_with('GET', '/api/v1/market-data/exchanges')
        self.assertFalse((Path(self.tmp.name)/'finbot.db').exists())

    def test_verified_venue_names_and_future_library_mics_are_discovered(self):
        for label, exchange_id, expected in [('Oslo Stock Exchange', 14, 'XOSL'),
                ('Bolsa De Madrid', 10, 'XMAD'), ('XJSE', 58, 'XJSE')]:
            with self.subTest(label=label):
                result = registry.discover_calendar(self.instrument(label, exchange_id), self.request)
                self.assertEqual(result['status'], 'verified')
                self.assertEqual(result['calendar'], expected)
        self.assertEqual(self.request.call_count, 1)  # Shared directory refresh.

    def test_unknown_and_valid_but_unsupported_venues_record_reason_and_retry(self):
        for label, exchange_id in [('Made Up Exchange', 999), ('Unknown Venue', 99), ('CME', 40)]:
            with self.subTest(label=label):
                result = registry.discover_calendar(self.instrument(label, exchange_id), self.request)
                self.assertEqual(result['status'], 'unresolved'); self.assertIsNone(result['calendar'])
                self.assertGreater(result['retry_at'], result['checked_epoch'])
                self.assertTrue(result['reason'])
        self.assertEqual(len(json.loads(self.path.read_text())['records']), 3)

    def test_id_name_mismatch_and_ambiguous_broker_identity_are_not_registered(self):
        result = registry.discover_calendar(self.instrument('SIX', 14), self.request)
        self.assertEqual(result['status'], 'unresolved')
        other = Mock(return_value={'exchangeInfo': [{'exchangeID': 1, 'exchangeDescription': 'NYSE'},
                                                   {'exchangeID': 2, 'exchangeDescription': 'NYSE'}]})
        with patch.dict(registry._directory, clear=True):
            result = registry.discover_calendar(self.instrument('NYSE', None), other)
        self.assertEqual(result['status'], 'unresolved')

    def test_missing_instrument_exchange_id_requires_unique_directory_name(self):
        result = registry.discover_calendar(self.instrument('SIX', None), self.request)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['broker_exchange']['exchangeID'], 12)

    def test_failure_is_throttled_and_refresh_retries_after_expiration(self):
        fail = Mock(side_effect=TimeoutError('timeout'))
        result = registry.discover_calendar(self.instrument(), fail)
        again = registry.discover_calendar(self.instrument(), fail)
        self.assertEqual(result['status'], 'unresolved'); self.assertTrue(again['cached'])
        self.assertEqual(fail.call_count, 1)
        with patch('brokers.exchange_registry.time.time', return_value=result['retry_at']+1):
            result = registry.discover_calendar(self.instrument(), self.request)
        self.assertEqual(result['status'], 'verified')

    def test_library_change_reverifies_and_bad_registry_is_rebuilt(self):
        first = registry.discover_calendar(self.instrument(), self.request)
        content = json.loads(self.path.read_text())
        for row in content['records'].values(): row['calendar_version'] = 'older-version'
        self.path.write_text(json.dumps(content))
        result = registry.discover_calendar(self.instrument(), self.request)
        self.assertNotIn('cached', result)
        self.assertEqual(result['calendar_version'], first['calendar_version'])
        self.path.write_text('[]')
        self.assertEqual(registry.discover_calendar(self.instrument(), self.request)['status'], 'verified')

    def test_dynamic_calendar_retains_holidays_and_exchange_identity(self):
        instrument = self.instrument('Bolsa De Madrid', 10)
        instrument['exchange_calendar_resolution'] = registry.discover_calendar(instrument, self.request)
        opened = execution_session(instrument, datetime.fromisoformat('2026-10-29T10:00:00+00:00'))
        holiday = execution_session(instrument, datetime.fromisoformat('2026-12-25T10:00:00+00:00'))
        self.assertTrue(opened['is_regular_session']); self.assertFalse(holiday['is_regular_session'])
        instrument['exchange_id'] = 99
        self.assertFalse(execution_session(instrument)['is_regular_session'])

    def test_adapter_discovers_unknown_before_inactive_or_closed_market_checks(self):
        raw = dict(instrumentId=123, internalSymbolFull='TEST', internalAssetClassName='Stocks',
            internalExchangeName='SIX', internalExchangeId=12, isCurrentlyTradable=False,
            isActiveInPlatform=False, isExchangeOpen=False)
        a = object.__new__(EtoroBrokerAdapter); a.settings = SimpleNamespace(etoro_demo_trading=True)
        a._find_instrument = Mock(return_value=raw); a._request_json = self.request
        resolved = a.resolve_instrument('TEST')
        self.assertEqual(resolved['exchange_calendar_resolution']['status'], 'verified')
        self.assertFalse(resolved['tradeable'])
        self.assertFalse(resolved['exchange_open'])
        self.assertEqual(resolved['exchange_id'], 12)

    def test_storage_failure_cannot_claim_registration(self):
        with patch('brokers.exchange_registry._save', side_effect=PermissionError('read only')):
            result = registry.discover_calendar(self.instrument(), self.request)
        self.assertEqual(result['status'], 'unresolved')
        self.assertIsNone(result['calendar'])

    def test_new_labels_do_not_clobber_previously_discovered_records(self):
        registry.discover_calendar(self.instrument(), self.request)
        registry.discover_calendar(self.instrument('Oslo Stock Exchange', 14), self.request)
        self.assertEqual(len(json.loads(self.path.read_text())['records']), 2)

    def test_no_fuzzy_name_or_substring_matching(self):
        request = Mock(return_value={'exchangeInfo': [{'exchangeID': 1, 'exchangeDescription': 'SIX Crypto'}]})
        self.assertEqual(registry.discover_calendar(self.instrument('SIX Crypto', 1), request)['status'], 'unresolved')
