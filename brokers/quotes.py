"""Broker quote observations and bounded data-only retries."""
from datetime import datetime, timezone
import time

from .parsing import _broker_time, _coerce_optional_float


class MissingQuote(ValueError):
    pass


def assess_rate(rate, now=None):
    now = now or datetime.now(timezone.utc)
    bid, ask = _coerce_optional_float(rate.get('bid')), _coerce_optional_float(rate.get('ask'))
    stamp = _broker_time(rate.get('date') or rate.get('timestamp') or rate.get('lastUpdated'))
    age = (now-datetime.fromisoformat(stamp)).total_seconds() if stamp else None
    code, reason = 'fresh', 'Fresh realtime broker quote.'
    if bid is None or ask is None:
        code, reason = 'missing_quote', 'Broker returned no complete bid/ask quote.'
    elif bid <= 0 or ask < bid:
        code, reason = 'invalid_quote', 'Broker returned invalid bid/ask prices.'
    elif not stamp:
        code, reason = 'missing_timestamp', 'Broker quote timestamp is missing or invalid.'
    elif age < -5:
        code, reason = 'clock_skew', 'Broker quote timestamp is in the future.'
    elif 'delay' in str(rate.get('quoteType', '')).lower():
        code, reason = 'delayed_quote', 'Broker identifies the quote as delayed.'
    elif age > 60:
        code, reason = 'stale_quote', f'Quote received, but {age:.1f} seconds old; maximum 60 seconds.'
    elif str(rate.get('quoteType', '')).lower() != 'realtime':
        code, reason = 'unknown_quote_type', 'Broker has not identified the quote as realtime.'
    return {'code': code, 'reason': reason, 'quote_at': stamp, 'age_seconds': age,
            'checked_at': now.isoformat(), 'max_age_seconds': 60}


def read_rate_with_retries(read, sleep=None):
    """Initial read plus two stale-only retries; never retry an order or HTTP error."""
    sleep = sleep or time.sleep
    attempts = []
    for attempt in range(3):
        try:
            rate = read()
        except Exception as error:
            error.quote_attempts = [*attempts, {'attempt': attempt+1, 'code': 'missing_quote' if isinstance(error, MissingQuote) else 'rate_limited' if getattr(error, 'status_code', None) == 429 else 'request_failed'}]
            raise
        result = assess_rate(rate)
        attempts.append({**result, 'attempt': attempt+1})
        if result['code'] != 'stale_quote' or attempt == 2:
            if result['code'] == 'stale_quote':
                result['reason'] += ' Still stale after 3 reads (2 retries).'
            return {**rate, 'timestamp': result['quote_at'],
                    'quote_validation': {**result, 'attempts': attempts, 'retries_used': attempt}}
        sleep((2, 5)[attempt])
    raise AssertionError('Unreachable quote retry state')
