"""Completed regular-session volume evidence; no broker authorization or sizing."""
from datetime import datetime, timezone
import math
import pandas as pd

CALENDARS = {'NMS': 'XNYS', 'NGM': 'XNYS', 'NCM': 'XNYS', 'NYQ': 'XNYS',
             'PCX': 'XNYS', 'ASE': 'XNYS', 'LSE': 'XLON', 'STO': 'XSTO',
             'GER': 'XETR', 'PAR': 'XPAR', 'AMS': 'XAMS', 'TOR': 'XTSE',
             'HKG': 'XHKG', 'JPX': 'XTKS', 'ASX': 'XASX'}


def from_bars(bars, calendar, now=None):
    """Compare exact five-minute slots across at least ten prior sessions."""
    now = pd.Timestamp(now or datetime.now(timezone.utc)).tz_convert('UTC')
    result = {'available': False, 'source': 'Yahoo underlying regular-session 5m OHLCV',
              'observed_at': now.isoformat(), 'method': 'same_elapsed_session_slots_v1'}
    try:
        frame = bars.copy()
        if frame.empty or frame.index.tz is None or frame.index.has_duplicates:
            raise ValueError('Missing, undated or duplicate bars.')
        frame.index = frame.index.tz_convert('UTC')
        session = calendar.date_to_session(now.date(), direction='none')
        opened, closed = calendar.session_open(session), calendar.session_close(session)
        if not opened <= now < closed:
            raise ValueError('Outside underlying regular session.')
        slots = pd.date_range(opened, closed, freq='5min', inclusive='left')
        slots = pd.DatetimeIndex([s for s in slots if calendar.is_trading_minute(s) and s + pd.Timedelta(minutes=5) <= now])
        if len(slots) < 2 or (now - (slots[-1] + pd.Timedelta(minutes=5))).total_seconds() > 600:
            raise ValueError('Insufficient completed bars or session break.')
        def valid_slice(expected):
            selected = frame.reindex(expected)
            values = selected[['High', 'Low', 'Close', 'Volume']]
            if not values.map(lambda x: pd.notna(x) and math.isfinite(float(x))).all().all():
                raise ValueError('Incomplete session coverage.')
            if (values[['High', 'Low', 'Close']] <= 0).any().any() or (values.Volume < 0).any() or (values.High < values.Low).any():
                raise ValueError('Invalid OHLCV.')
            return values
        current = valid_slice(slots)
        baseline = []
        for prior in calendar.sessions_window(session, -25)[:-1]:
            prior_slots = calendar.session_open(prior) + (slots - opened)
            if prior_slots[-1] + pd.Timedelta(minutes=5) > calendar.session_close(prior):
                continue
            if not all(calendar.is_trading_minute(s) for s in prior_slots):
                continue
            try:
                total = float(valid_slice(prior_slots).Volume.sum())
                if total > 0:
                    baseline.append(total)
            except ValueError:
                continue
        baseline = baseline[-20:]
        total = float(current.Volume.sum())
        if len(baseline) < 10 or total <= 0:
            raise ValueError('At least ten complete positive-volume comparison sessions required.')
        typical = (current.High + current.Low + current.Close) / 3
        result.update(available=True, rvol=total / (sum(baseline) / len(baseline)),
                      vwap=float((typical * current.Volume).sum() / total),
                      last=float(current.Close.iloc[-1]), previous=float(current.Close.iloc[-2]),
                      through=(slots[-1] + pd.Timedelta(minutes=5)).isoformat(),
                      session_open=opened.isoformat(), session_close=closed.isoformat(),
                      baseline_sessions=len(baseline), completed_bars=len(slots), volume=total,
                      vwap_method='typical_price_bar_approximation')
    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
        result['reason'] = str(error)
    return result


def load_volume_context(symbol):
    try:
        import yfinance as yf
        import exchange_calendars as xcals
        from market_instruments import reference_symbol
        ticker = yf.Ticker(reference_symbol(symbol))
        bars = ticker.history(period='1mo', interval='5m', auto_adjust=True, actions=False, prepost=False, timeout=5)
        metadata = ticker.get_history_metadata()
        name = CALENDARS.get(metadata.get('exchangeName'))
        if not name:
            return {'available': False, 'reason': 'Underlying exchange calendar is not explicitly mapped.'}
        return from_bars(bars, xcals.get_calendar(name))
    except Exception as error:
        return {'available': False, 'reason': f'Volume context unavailable ({type(error).__name__}).'}


def confirmation(candidate, side, rules, now=None):
    evidence = candidate.get('volume_context') or {}
    result = {'qualified': False, 'side': side, 'evidence': evidence,
              'risk_budget_changed': False}
    try:
        now = pd.Timestamp(now or datetime.now(timezone.utc)).tz_convert('UTC')
        through = pd.Timestamp(evidence['through'])
        if not rules.get('volume_confirmation_enabled', True) or not evidence.get('available') or side not in {'LONG', 'SHORT'}:
            raise ValueError('Volume confirmation disabled or unavailable.')
        if not 0 <= (now - through).total_seconds() <= 900 or not pd.Timestamp(evidence['session_open']) <= now < pd.Timestamp(evidence['session_close']):
            raise ValueError('Volume evidence is stale or outside its session.')
        atr = float(candidate['daily_trend_context']['atr14'])
        quote = candidate.get('market_quote') or {}
        price = float(quote.get('ask' if side == 'LONG' else 'bid') or candidate['price'])
        vwap, rvol = float(evidence['vwap']), float(evidence['rvol'])
        last, previous = float(evidence['last']), float(evidence['previous'])
        if not all(math.isfinite(v) and v > 0 for v in [atr, price, vwap, rvol, last, previous]):
            raise ValueError('Invalid volume confirmation inputs.')
        sign = 1 if side == 'LONG' else -1
        distance = sign * (price - vwap) / atr
        result.update(directional_vwap_distance_atr=distance, rvol=rvol)
        result['qualified'] = (rvol >= float(rules.get('volume_min_rvol', 1.5))
                               and 0 <= distance <= float(rules.get('volume_max_vwap_distance_atr', .75))
                               and sign * (last - vwap) >= 0 and sign * (last - previous) > 0)
        result['reason'] = 'Direction, participation and entry stretch confirmed.' if result['qualified'] else 'Participation, VWAP alignment or follow-through is insufficient.'
    except (KeyError, ValueError, TypeError, OverflowError) as error:
        result['reason'] = str(error)
    return result


def adjusted_rules(candidate, side, rules):
    evidence = confirmation(candidate, side, rules)
    adjusted = dict(rules)
    changes = {}
    if evidence['qualified']:
        for key, default, cap in [('min_confidence', .7, .05),
                                   ('fundamental_confirmation_min_confidence', .6, .05),
                                   ('countertrend_long_min_confidence', .85, .025),
                                   ('countertrend_short_min_confidence', .85, .025)]:
            base = float(rules.get(key, default))
            reduction = min(cap, max(0, float(rules.get('volume_confidence_relaxation', .05))))
            adjusted[key] = min(base, max(.6, base - reduction))
            changes[key] = {'baseline': base, 'effective': adjusted[key]}
    return adjusted, {**evidence, 'thresholds': changes}


def prioritize(pool, observations, limit):
    """Reserve half the new-candidate slots for recency; RVOL orders the rest."""
    limit = max(0, limit)
    recent = pool[:(limit + 1) // 2]
    seen = {symbol for symbol, _ in recent}
    def score(pair):
        evidence = observations.get(pair[0]) or {}
        value = evidence.get('rvol', 0) if evidence.get('available') else 0
        return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else 0
    active = sorted((pair for pair in pool if pair[0] not in seen), key=score, reverse=True)
    return recent + active[:limit - len(recent)]


def decision_note(evidence):
    """Human-readable explanation shared by agent and lifecycle messages."""
    if not evidence or not evidence.get('qualified'):
        return ''
    side = evidence.get('side', '')
    labels = {'min_confidence': 'base',
              'fundamental_confirmation_min_confidence': 'fundamentals-confirmed',
              f'countertrend_{side.lower()}_min_confidence': f'{side} countertrend'}
    changes = []
    for key, label in labels.items():
        values = evidence.get('thresholds', {}).get(key)
        if values and values['effective'] < values['baseline']:
            changes.append(f"{label} {values['baseline'] * 100:g}% to {values['effective'] * 100:g}%")
    if not changes:
        return ''
    admission = (' This relaxation enabled strategy approval.' if evidence.get('relaxation_used') else
                 ' Strategy also passed baseline thresholds.' if evidence.get('baseline_accepted') is True else
                 ' Other eligibility checks still apply.')
    return (f" Volume confirmation applied for {side}: RVOL {evidence['rvol']:.2f}x; "
            f"directional VWAP distance {evidence['directional_vwap_distance_atr']:.2f} ATR. "
            f"Confidence thresholds: {'; '.join(changes)}.{admission} Risk allocation and limits unchanged.")
