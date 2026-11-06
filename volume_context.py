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
        # Older fixtures and a few providers omit Open. Close is a conservative
        # neutral proxy for candle-direction volume; Yahoo supplies real Open.
        if 'Open' not in frame:
            frame['Open'] = frame['Close']
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
            values = selected[['Open', 'High', 'Low', 'Close', 'Volume']]
            if not values.map(lambda x: pd.notna(x) and math.isfinite(float(x))).all().all():
                raise ValueError('Incomplete session coverage.')
            if (values[['Open', 'High', 'Low', 'Close']] <= 0).any().any() or (values.Volume < 0).any() or (values.High < values.Low).any():
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
        cumulative_volume = current.Volume.cumsum()
        cumulative_vwap = (typical * current.Volume).cumsum() / cumulative_volume.replace(0, float('nan'))
        vwap = float(cumulative_vwap.iloc[-1])
        earlier_vwap = float(cumulative_vwap.iloc[-min(4, len(cumulative_vwap))])
        ema9 = float(current.Close.ewm(span=9, adjust=False).mean().iloc[-1])
        ema20 = float(current.Close.ewm(span=20, adjust=False).mean().iloc[-1])
        recent = current.tail(3); previous_range = current.iloc[-6:-3]
        structure_state = 'mixed'
        if len(previous_range) == 3:
            if recent.High.max() > previous_range.High.max() and recent.Low.min() > previous_range.Low.min():
                structure_state = 'bullish'
            elif recent.High.max() < previous_range.High.max() and recent.Low.min() < previous_range.Low.min():
                structure_state = 'bearish'
        opening = current.head(min(6, len(current)))
        up_volume = float(current.loc[current.Close >= current.Open, 'Volume'].sum())
        down_volume = float(current.loc[current.Close < current.Open, 'Volume'].sum())
        elapsed_minutes = int((slots[-1] + pd.Timedelta(minutes=5) - opened).total_seconds() / 60)
        session_minutes = int((closed - opened).total_seconds() / 60)
        prior = frame.loc[frame.index < opened, 'Close']
        previous_close = float(prior.iloc[-1]) if not prior.empty else None
        session_open_price = float(current.Open.iloc[0])
        result.update(available=True, rvol=total / (sum(baseline) / len(baseline)),
                      vwap=vwap, vwap_slope='rising' if vwap > earlier_vwap else 'falling' if vwap < earlier_vwap else 'flat',
                      vwap_change=float(vwap-earlier_vwap), ema9=ema9, ema20=ema20,
                      intraday_structure_state=structure_state,
                      opening_range_high=float(opening.High.max()), opening_range_low=float(opening.Low.min()),
                      above_opening_range=bool(float(current.Close.iloc[-1]) > float(opening.High.max())),
                      below_opening_range=bool(float(current.Close.iloc[-1]) < float(opening.Low.min())),
                      up_volume=up_volume, down_volume=down_volume,
                      directional_volume_ratio=(up_volume / down_volume if down_volume > 0 else None),
                      session_elapsed_minutes=elapsed_minutes,
                      session_remaining_minutes=max(0, session_minutes-elapsed_minutes),
                      time_of_day=('opening' if elapsed_minutes <= 30 else 'closing' if session_minutes-elapsed_minutes <= 60 else 'midday'),
                      previous_session_close=previous_close, session_open_price=session_open_price,
                      gap_pct=(100*(session_open_price/previous_close-1) if previous_close else None),
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
        result = from_bars(bars, xcals.get_calendar(name))
        if result.get('available') and reference_symbol(symbol) != 'SPY':
            try:
                benchmark = yf.Ticker('SPY').history(period='5d', interval='5m', auto_adjust=True, actions=False, prepost=False, timeout=5)
                benchmark.index = pd.DatetimeIndex(benchmark.index).tz_convert('UTC')
                through = pd.Timestamp(result['through']); opened = pd.Timestamp(result['session_open'])
                stock_session = bars.copy(); stock_session.index = pd.DatetimeIndex(stock_session.index).tz_convert('UTC')
                stock_session = stock_session.loc[(stock_session.index >= opened) & (stock_session.index < through)]
                benchmark = benchmark.loc[(benchmark.index >= opened) & (benchmark.index < through)]
                if len(stock_session) >= 4 and len(benchmark) >= 4:
                    def change(frame, count=None):
                        sample = frame.tail(count) if count else frame
                        return 100*(float(sample.Close.iloc[-1])/float(sample.Close.iloc[0])-1)
                    result['relative_strength'] = {
                        'available': True, 'benchmark': 'SPY',
                        'session_pct_points': change(stock_session)-change(benchmark),
                        'last_15m_pct_points': change(stock_session,4)-change(benchmark,4),
                        'last_60m_pct_points': change(stock_session,13)-change(benchmark,13),
                        'role': 'current relative performance; beta is not a substitute'}
            except Exception:
                result['relative_strength'] = {'available': False, 'benchmark': 'SPY'}
        return result
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
        intraday_state = evidence.get('intraday_structure_state')
        expected = 'bullish' if side == 'LONG' else 'bearish'
        richer_available = intraday_state in {'bullish','bearish','mixed'} and evidence.get('ema9') and evidence.get('ema20') and evidence.get('vwap_slope')
        ema_aligned = None
        if richer_available:
            ema_aligned = (float(evidence['ema9']) > float(evidence['ema20'])) if side == 'LONG' else (float(evidence['ema9']) < float(evidence['ema20']))
        vwap_slope_aligned = evidence.get('vwap_slope') == ('rising' if side == 'LONG' else 'falling')
        intraday_aligned = not richer_available or (intraday_state == expected and ema_aligned and vwap_slope_aligned)
        result.update(directional_vwap_distance_atr=distance, rvol=rvol,
                      intraday_structure_state=intraday_state or 'unavailable', ema_aligned=bool(ema_aligned) if richer_available else None,
                      vwap_slope=evidence.get('vwap_slope'), intraday_confirmation_applied=bool(richer_available))
        result['qualified'] = (rvol >= float(rules.get('volume_min_rvol', 1.5))
                               and 0 <= distance <= float(rules.get('volume_max_vwap_distance_atr', .75))
                               and sign * (last - vwap) >= 0 and sign * (last - previous) > 0
                               and intraday_aligned)
        result['reason'] = ('Direction, participation, intraday structure and entry stretch confirmed.' if result['qualified'] else
                            'Participation, VWAP/EMA alignment, intraday structure or follow-through is insufficient.')
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
    intraday = (f" Intraday structure {evidence.get('intraday_structure_state')}; VWAP slope {evidence.get('vwap_slope')}; EMA9/20 aligned {evidence.get('ema_aligned')}."
                if evidence.get('intraday_confirmation_applied') else "")
    return (f" Volume confirmation applied for {side}: RVOL {evidence['rvol']:.2f}x; "
            f"directional VWAP distance {evidence['directional_vwap_distance_atr']:.2f} ATR. "
            f"Confidence thresholds: {'; '.join(changes)}.{intraday}{admission} Risk allocation and limits unchanged.")
