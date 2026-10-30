"""Conservative boundary for the broker-backed history. No broker calls or imports."""
from datetime import datetime,timezone
import json

MARKER='legacy_history_cleanup_v1'
STRONG_EXECUTION={'submitted','executed','uncertain'}
PENDING={'SUBMITTING','SUBMITTED_PENDING_RECONCILIATION','RECONCILIATION_REQUIRED','EXIT_PENDING'}

def stamp(value):
    try:
        t=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
    except (ValueError,TypeError):return None

def json_object(raw):
    if isinstance(raw,dict):return raw
    try:
        value=json.loads(raw or '{}');return value if isinstance(value,dict) else {}
    except (TypeError,ValueError):return {}

def broker_linked(trade):
    return any(str(trade.get(k) or '').strip() for k in ('broker_order_id','broker_position_id','order_reference')) or trade.get('status') in PENDING

def execution_evidence(events):
    for e in events:
        if e.get('stage')!='execution':continue
        if e.get('decision') in STRONG_EXECUTION:return True
        details=json_object(e.get('details'))
        if details.get('broker_order_submitted') or details.get('broker_order_id') or details.get('broker_position_id'):return True
        order=details.get('broker_order')
        if isinstance(order,dict) and any(order.get(k) for k in ('order_id','position_id','orderId','positionId','id')):return True
    return False

def retain_trade(trade,cutoff,protected=(),events=()):
    # Missing dates and ambiguous submission evidence always stay for investigation.
    date=stamp(trade.get('created_at'));boundary=stamp(cutoff)
    return not boundary or not date or date>=boundary or int(trade['id']) in protected or broker_linked(trade) or execution_evidence(events)
