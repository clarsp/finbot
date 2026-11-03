"""Conservative calendar guard for reconciling eToro search metadata conflicts."""
from datetime import datetime, timezone
from functools import lru_cache

import exchange_calendars
import pandas as pd
from .exchange_registry import normalized_name


# Only explicitly supported exchange identities may reconcile a false search flag.
CALENDARS = {
    'NASDAQ': 'XNYS', 'NYSE': 'XNYS', 'NYSE ARCA': 'XNYS',
    'NYSE AMERICAN': 'XNYS', 'AMEX': 'XNYS', 'S&P500': 'XNYS',
    'LSE': 'XLON', 'LONDON STOCK EXCHANGE': 'XLON',
    'XETRA': 'XETR', 'EURONEXT PARIS': 'XPAR',
    'EURONEXT AMSTERDAM': 'XAMS', 'STOCKHOLM': 'XSTO',
    'TOKYO': 'XTKS', 'HONG KONG': 'XHKG', 'ASX': 'XASX',
    'TORONTO': 'XTSE',
}


@lru_cache(maxsize=32)
def _calendar(name):
    return exchange_calendars.get_calendar(name, side='left')


def execution_session(instrument, now=None):
    now = now or datetime.now(timezone.utc)
    name = CALENDARS.get(normalized_name(instrument.get('exchange_name')))
    discovery = instrument.get('exchange_calendar_resolution') or {}
    if (not name and discovery.get('status') == 'verified'
            and discovery.get('exchange_name') == normalized_name(instrument.get('exchange_name'))
            and discovery.get('exchange_id') == instrument.get('exchange_id')
            and discovery.get('source') == 'etoro_exchange_directory+installed_calendar_catalog'
            and discovery.get('retry_at', 0) > datetime.now(timezone.utc).timestamp()
            and discovery.get('calendar_version') == exchange_calendars.__version__):
        name = discovery.get('calendar')
    result = {'calendar': name, 'checked_at': now.isoformat(), 'is_regular_session': False}
    if discovery:
        result['discovery'] = discovery
    if not name:
        return {**result, 'reason': discovery.get('reason') or 'Exchange calendar discovery is required.'}
    try:
        result['is_regular_session'] = bool(_calendar(name).is_open_on_minute(pd.Timestamp(now).floor('min')))
        result['reason'] = 'Calendar regular session open.' if result['is_regular_session'] else 'Calendar closed, holiday, break or outside regular session.'
    except Exception as error:
        result['reason'] = 'Execution calendar could not be verified: ' + str(error)
    return result
