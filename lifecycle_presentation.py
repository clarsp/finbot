"""Compact lifecycle presentation without changing persisted audit evidence."""


def group_shadow_events(events):
    """One shadow step per trade; preserve every observation in its evidence trail."""
    grouped=list(events)
    for stage,field in [('shadow','shadow_observations'),('reassessment','reassessments')]:
        observations=[e for e in grouped if e.get('stage')==stage]
        if not observations:continue
        result=[];inserted=False
        for event in grouped:
            if event.get('stage')!=stage:result.append(event)
            elif not inserted:
                result.append({**observations[-1],field:observations});inserted=True
        grouped=result
    return grouped


def lifecycle_status_label(trade):
    """Describe broker involvement without implying that a preflight check is an order."""
    status = trade.get('status', '')
    reconciliation = trade.get('reconciliation_status', '')
    if status == 'REJECTED_EXECUTION':
        return 'Broker rejected' if reconciliation in {'broker_rejected', 'broker_confirmed_rejection'} else 'Execution rejected'
    if status == 'SUBMITTED_PENDING_RECONCILIATION':
        return 'Broker pending · awaiting fill / confirmation'
    if status == 'RECONCILIATION_REQUIRED':
        return 'Broker outcome uncertain · reconciliation required'
    if status == 'SUBMITTING':
        return 'Broker submission in progress'
    if status == 'EXIT_PENDING':
        return 'Broker close pending'
    if status == 'ANALYSIS_ONLY':
        return 'Not submitted · execution blocked'
    if status == 'DEFERRED_CAPACITY':
        return 'Not submitted · capacity blocked'
    return status.replace('_', ' ').capitalize()


def research_is_expired(trade, max_age_hours, now):
    from datetime import datetime, timezone
    try:
        created = datetime.fromisoformat(str(trade['created_at']).replace('Z', '+00:00'))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (now-created).total_seconds()
        return age < 0 or age > float(max_age_hours)*3600
    except (KeyError, ValueError, TypeError):
        return True
