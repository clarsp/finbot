"""Discover broker exchange identities against installed calendar definitions.

Only exact names/MICs and explicit venue synonyms are accepted. Discovery adds
data records, never executable code or an invented trading schedule.
"""
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time

import exchange_calendars

logger = logging.getLogger(__name__)
_lock = threading.RLock()
_memory = {}
_directory = {}

# Identity synonyms for venues whose broker display name is not a calendar key.
# The active registry is populated on demand after broker identity verification.
VENUE_NAMES = {
    'FRA': 'XFRA', 'BOLSA DE MADRID': 'XMAD', 'BORSA ITALIANA': 'XMIL',
    'TYO': 'XTKS', 'TOKYO STOCK EXCHANGE': 'XTKS',
    'OSLO STOCK EXCHANGE': 'XOSL', 'STOCKHOLM STOCK EXCHANGE': 'XSTO',
    'COPENHAGEN STOCK EXCHANGE': 'XCSE', 'HELSINKI STOCK EXCHANGE': 'XHEL',
    'TORONTO STOCK EXCHANGE': 'XTSE', 'HONG KONG EXCHANGES': 'XHKG',
    'EURONEXT LISBON': 'XLIS', 'EURONEXT BRUSSELS': 'XBRU',
    'TADAWUL': 'XSAU', 'SYDNEY': 'XASX', 'VIENNA': 'XWBO',
    'DUBLIN EN': 'XDUB', 'PRAGUE SE': 'XPRA', 'WARSAW': 'XWAR',
    'BUDAPEST': 'XBUD', 'XETRA ETFS': 'XETR',
    'SHANGHAI STOCK EXCHANGE': 'XSHG', 'TSX VENTURE EXCHANGE': 'XTSX',
    'SINGAPORE EXCHANGE': 'XSES', 'NASDAQ ICELAND': 'XICE',
    'NASDAQ TALLINN': 'XTAL', 'NASDAQ VILNIUS': 'XLIT', 'NASDAQ RIGA': 'XRIS',
    'KOREA EXCHANGE': 'XKRX', 'TAIWAN STOCK EXCHANGE': 'XTAI',
    'TASE- TEL AVIV STOCKS EXCHANGE': 'XTAE',
}


def normalized_name(value):
    return re.sub(r'\s+', ' ', str(value or '').strip().upper())


def _path():
    # Derived data lives in the existing persistent volume, never a host DB.
    db_path = os.environ.get('FINBOT_DB_PATH')
    return Path(db_path).parent / 'exchange_calendar_registry.json' if db_path else None


def _records(path):
    if path is None:
        return dict(_memory)
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('records'), dict):
            return {}
        return {key: value for key, value in data['records'].items() if isinstance(value, dict)}
    except (OSError, ValueError, TypeError):
        return {}


def _save(key, record):
    path = _path()
    with _lock:
        if path is None:
            _memory[key] = record
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix('.lock').open('a') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            records = _records(path)
            records[key] = record
            records = dict(sorted(records.items(), key=lambda item: item[1].get('checked_epoch', 0))[-512:])
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.exchange-registry-', delete=False) as output:
                    temporary = Path(output.name)
                    json.dump({'version': 1, 'records': records}, output, sort_keys=True)
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)


def _exchange_directory(request, scope, now):
    with _lock:
        cached = _directory.get(scope)
        if cached and cached['expires_at'] > now:
            if cached.get('error'):
                raise ValueError(cached['error'])
            return cached['rows']
        try:
            result = request('GET', '/api/v1/market-data/exchanges')
            rows = result.get('exchangeInfo')
            if not isinstance(rows, list) or not rows:
                raise ValueError('Broker exchange directory is missing or empty.')
            _directory[scope] = {'rows': rows, 'expires_at': now + 900}
            return rows
        except Exception:
            _directory[scope] = {'error': 'Broker exchange directory unavailable; discovery will retry.', 'expires_at': now + 300}
            raise ValueError(_directory[scope]['error']) from None


def discover_calendar(instrument, request):
    """Verify unknown exchange, register a usable calendar, or record a retry."""
    label = normalized_name(instrument.get('exchange_name'))
    exchange_id = instrument.get('exchange_id')
    key = json.dumps([exchange_id, label], separators=(',', ':'))
    now = time.time()
    path = _path()
    with _lock:
        previous = _records(path).get(key) or {}
    if (previous.get('retry_at', 0) > now
            and previous.get('calendar_version') == exchange_calendars.__version__):
        return {**previous, 'cached': True}
    record = {'exchange_name': label, 'exchange_id': exchange_id,
              'status': 'unresolved', 'calendar': None, 'checked_epoch': now,
              'checked_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
              'calendar_version': exchange_calendars.__version__,
              'source': 'etoro_exchange_directory+installed_calendar_catalog', 'retry_at': now + 3600}
    try:
        if not label:
            raise ValueError('Instrument has no exchange name to verify.')
        rows = _exchange_directory(request, str(path), now)
        matches = [row for row in rows if isinstance(row, dict)
                   and normalized_name(row.get('exchangeDescription')) == label
                   and (exchange_id is None or str(row.get('exchangeID')) == str(exchange_id))]
        if len(matches) != 1 or not isinstance(matches[0].get('exchangeID'), int) or isinstance(matches[0]['exchangeID'], bool):
            raise ValueError('Exchange name/ID could not be uniquely verified in the broker directory.')
        record['broker_exchange'] = matches[0]
        aliases = {normalized_name(name): name for name in exchange_calendars.get_calendar_names()}
        candidate = VENUE_NAMES.get(label) or aliases.get(label)
        if not candidate:
            raise ValueError('Valid broker exchange has no exact supported calendar identity; calendar support is required.')
        calendar = exchange_calendars.get_calendar(candidate, side='left')
        if not calendar.name.startswith('X'):
            raise ValueError('Generic or product-dependent schedules cannot be automatically registered for an exchange.')
        record.update(status='verified', calendar=candidate, canonical_calendar=calendar.name,
                      timezone=str(calendar.tz), retry_at=now + 86400,
                      reason='Broker exchange identity and installed calendar verified.')
    except Exception as error:
        record['reason'] = str(error)
        logger.warning('Exchange calendar discovery unresolved exchange=%s id=%s reason=%s', label, exchange_id, error)
    try:
        _save(key, record)
    except OSError:
        # Do not claim registration when it could not be retained.
        record.update(status='unresolved', calendar=None, reason='Exchange registry could not be saved; discovery will retry.')
        logger.exception('Could not save exchange calendar discovery')
    return record
