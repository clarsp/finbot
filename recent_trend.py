"""Recent price direction from the existing completed daily OHLC history.

No broker calls, new feed, probabilistic confidence or position sizing.
"""
import math
import pandas as pd

VERSION = 'recent_price_trend_v1'


def analyze_recent_trend(bars):
    result = {'available': False, 'version': VERSION, 'state': 'unknown',
              'source': 'existing completed adjusted daily OHLC',
              'minimum_displacement_atr': .5}
    try:
        if bars is None or len(bars) < 20:
            raise ValueError('At least 20 completed daily sessions are required.')
        frame = bars.copy()
        if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
            raise ValueError('Daily sessions must be chronological and unique.')
        tail = frame.tail(20)
        for column in ['Close', 'High', 'Low']:
            if not all(math.isfinite(float(v)) and float(v) > 0 for v in tail[column]):
                raise ValueError('Recent daily prices are missing or invalid.')
        if (tail.High < tail.Low).any():
            raise ValueError('Daily high is below low.')
        atr = float(frame.atr14.iloc[-1])
        if not math.isfinite(atr) or atr <= 0:
            raise ValueError('A positive daily ATR14 is required.')
        close = float(frame.Close.iloc[-1])
        returns = {n: 100 * (close / float(frame.Close.iloc[-1-n]) - 1) for n in [7, 14]}
        moves = {n: (close - float(frame.Close.iloc[-1-n])) / atr for n in [7, 14]}
        averages = frame.Close.rolling(5).mean()
        slope = (float(averages.iloc[-1]) - float(averages.iloc[-6])) / atr
        previous, latest = frame.iloc[-10:-5], frame.iloc[-5:]
        structure = {'previous_high': float(previous.High.max()), 'previous_low': float(previous.Low.min()),
                     'recent_high': float(latest.High.max()), 'recent_low': float(latest.Low.min()),
                     'method': 'two_nonoverlapping_five_session_ranges'}
        structure['state'] = ('bullish' if structure['recent_high'] > structure['previous_high'] and structure['recent_low'] > structure['previous_low'] else
                              'bearish' if structure['recent_high'] < structure['previous_high'] and structure['recent_low'] < structure['previous_low'] else 'mixed')
        # Conservatively exclude a week whose Friday label is later than the last
        # completed daily bar. Holiday-shortened current weeks wait until next week.
        as_of = pd.Timestamp(frame.index[-1])
        index = pd.DatetimeIndex(frame.index)
        weekly_frame = frame.copy()
        weekly_frame.index = index.tz_localize(None) if index.tz is not None else index
        weekly = weekly_frame.Close.resample('W-FRI').last().dropna()
        # The first sampled week may begin partway through a session week.
        # Discard it conservatively instead of claiming complete-week coverage.
        weekly = weekly.iloc[1:]
        cutoff = as_of.tz_localize(None) if as_of.tzinfo else as_of
        weekly = weekly.loc[weekly.index <= cutoff]
        valid_weekly = len(weekly) >= 5 and all(math.isfinite(float(v)) and float(v) > 0 for v in weekly.tail(5))
        week = {'available': valid_weekly, 'state': 'unknown',
                'closes': [{'week_ending': t.date().isoformat(), 'close': float(v)} for t, v in weekly.tail(5).items()]}
        if week['available']:
            avg = float(weekly.iloc[-4:].mean()); prior_avg = float(weekly.iloc[-5:-1].mean())
            last = float(weekly.iloc[-1])
            week.update(average_4_weeks=avg, previous_average_4_weeks=prior_avg,
                        state='bullish' if last > avg > prior_avg else 'bearish' if last < avg < prior_avg else 'mixed')
        state = 'mixed'
        for name, sign in [('bullish', 1), ('bearish', -1)]:
            if (all(sign * moves[n] >= .5 for n in [7, 14]) and sign * slope > 0
                    and name in {structure['state'], week['state']}):
                state = name
        result.update(available=True, state=state, as_of=as_of.isoformat(), close=close, atr14=atr,
                      return_7_sessions_pct=returns[7], return_14_sessions_pct=returns[14],
                      move_7_sessions_atr=moves[7], move_14_sessions_atr=moves[14],
                      average_5_sessions=float(averages.iloc[-1]), average_5_sessions_change_atr=slope,
                      price_structure=structure, completed_weeks=week,
                      rule='7/14-session moves each >=0.5 ATR in the same direction, five-session average slope agrees, and recent range structure or completed weekly direction confirms')
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError) as error:
        result['reason'] = str(error)
    return result


def active_recent_trend(candidate, rules):
    evidence = (candidate.get('daily_trend_context') or {}).get('recent_trend') or {}
    return evidence if rules.get('recent_trend_enabled', True) and evidence.get('available') and evidence.get('state') in {'bullish', 'bearish'} else {}


def recent_trend_note(evidence, applied=False):
    if not evidence or not evidence.get('available'):
        return ''
    week = evidence.get('completed_weeks') or {}
    return (f" Recent price trend: {evidence['state']}; completed through {evidence['as_of']}. "
            f"7-session change {evidence['return_7_sessions_pct']:+.2f}%, "
            f"14-session change {evidence['return_14_sessions_pct']:+.2f}%; "
            f"five-session average slope {evidence['average_5_sessions_change_atr']:+.2f} ATR; "
            f"range structure {evidence['price_structure']['state']}; completed weekly direction {week.get('state', 'unknown')}. "
            + ("Recent trend governs entry bias; company fundamentals are context, not a reversal signal." if applied else
               "Recent trend is observational here; legacy direction rules apply because it is disabled or not directional."))
