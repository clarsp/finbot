"""Elapsed time schedules reassessment; it never authorizes a position close."""
import json
import math
from datetime import datetime,timedelta,timezone

VERSION = 3


def timing_fields(maximum, forecast=None):
    interval = float(maximum)
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError('A finite positive review interval is required.')
    review = interval
    if forecast is not None:
        forecast = float(forecast)
        if math.isfinite(forecast) and forecast > 0:
            review = min(interval, forecast)
        else:
            forecast = None
    return {'timing_policy_version': VERSION, 'holding_hours': interval,
            'max_hold_hours': None, 'time_exit_enabled': False,
            'forecast_horizon_hours': forecast, 'review_after_hours': review,
            'review_interval_hours': review}


def migrate_open_policy(trade):
    """Keep original opening time, protective levels and frozen shadow experiments."""
    if trade.get('status') not in {'OPEN', 'RECONCILIATION_REQUIRED'}:
        return None
    policy = json.loads(trade.get('policy_json') or '{}')
    if policy.get('timing_policy_version', 0) >= VERSION:
        return None
    framework = policy.get('framework')
    if framework not in {'DIRECTIONAL_NEWS', 'CONDITIONAL_MEAN_REVERSION'}:
        return None
    rules = json.loads(trade.get('config_json') or '{}').get('risk_manager', {})
    key = 'mean_reversion_max_hold_hours' if framework == 'CONDITIONAL_MEAN_REVERSION' else 'news_max_hold_hours'
    interval = rules.get(key, policy.get('holding_hours', 24 if framework == 'CONDITIONAL_MEAN_REVERSION' else 48))
    fields = timing_fields(interval, policy.get('forecast_horizon_hours') or policy.get('review_after_hours') or policy.get('holding_hours'))
    return {**policy, **fields, 'previous_hard_limit_hours': policy.get('max_hold_hours',policy.get('holding_hours'))}


def stamp(value):
    result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def review_due(trade,policy,now=None):
    now=now or datetime.now(timezone.utc)
    if policy.get('timing_policy_version',0)<VERSION:return False
    due=policy.get('next_review_at')
    if due:return now>=stamp(due)
    opened=stamp(trade.get('opened_at') or trade['created_at'])
    return now>=opened+timedelta(hours=float(policy['review_after_hours']))


def invalidation_reason(trade,evidence,observed_at,now=None,max_age_hours=24):
    """Preserve the existing structural-news exit, requiring fresh saved research."""
    now=now or datetime.now(timezone.utc)
    if not observed_at:return None
    try:
        age=(now-stamp(observed_at)).total_seconds()
        confidence=float(evidence.get('confidence') or 0)
    except (ValueError,TypeError):return None
    if not 0<=age<=float(max_age_hours)*3600 or not math.isfinite(confidence):return None
    ai=evidence.get('openai') or {}
    if (ai.get('reaction_assessment')=='structural' and confidence>=.7
        and evidence.get('signal')==('bearish' if trade['side']=='LONG' else 'bullish')):
        return 'structural_news_invalidated_thesis'
    return None
