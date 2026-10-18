"""Local calendar pacing; does not pretend to be an exchange holiday calendar."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULTS = dict(weekend_pacing_enabled=True,weekend_trading_enabled=False,
    weekend_research_minutes=360,weekend_news_minutes=360,weekend_maintenance_minutes=15,
    timezone='Europe/Stockholm')


def policy(settings, now=None):
    result=dict(DEFAULTS)
    for key,default in DEFAULTS.items():
        raw=settings.get('research_schedule_'+key,default)
        result[key]=str(raw).lower() in {'true','1','yes','on'} if isinstance(default,bool) else int(raw) if isinstance(default,int) else str(raw)
    now=now or datetime.now(timezone.utc)
    result['is_weekend']=now.astimezone(ZoneInfo(result['timezone'])).weekday()>=5
    result['reduced_pacing']=result['is_weekend'] and result['weekend_pacing_enabled']
    return result


def interval(settings, kind, normal_minutes, now=None):
    p=policy(settings,now)
    return max(normal_minutes,p['weekend_'+kind+'_minutes']) if p['reduced_pacing'] else normal_minutes


def due_at(settings, kind, last, normal_minutes, now):
    due=last+timedelta(minutes=interval(settings,kind,normal_minutes,now))
    p=policy(settings,now)
    if p['reduced_pacing']:
        local=now.astimezone(ZoneInfo(p['timezone']))
        monday=(local+timedelta(days=7-local.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
        # Weekday cadence resumes without waiting out the weekend interval.
        due=min(due,monday.astimezone(timezone.utc))
    return due
