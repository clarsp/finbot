"""Local, advisory-only outcome reviews. No broker, LLM or execution capabilities."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import hashlib
import json
import math

VERSION = 'post-trade-v1'
TZ = ZoneInfo('Europe/Stockholm')
ACTIVE = ('OPEN','EXIT_PENDING','RECONCILIATION_REQUIRED','SUBMITTED_PENDING_RECONCILIATION','SUBMITTING')


def ensure_schema(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS position_management (
        trade_id INTEGER PRIMARY KEY REFERENCES trades(id), architecture_version TEXT NOT NULL,
        adopted_at TEXT NOT NULL, origin TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS configuration_audits (
        id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, evidence TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS performance_reviews (
        id INTEGER PRIMARY KEY, cadence TEXT NOT NULL, period_key TEXT NOT NULL,
        created_at TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL,
        version TEXT NOT NULL, evidence TEXT NOT NULL, UNIQUE(cadence,period_key));
    ''')


def adopt(conn, now=None):
    """Idempotent ownership metadata only; historical events/policies stay intact."""
    now = now or datetime.now(timezone.utc).isoformat()
    cursor = conn.execute('''INSERT OR IGNORE INTO position_management
        SELECT id,?,?,'existing_position' FROM trades
        WHERE broker_position_id IS NOT NULL AND status IN
        ('OPEN','EXIT_PENDING','RECONCILIATION_REQUIRED','SUBMITTED_PENDING_RECONCILIATION')''', (VERSION,now))
    conn.commit()
    return cursor.rowcount


def stamp(value):
    if not value:return None
    try:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:return None


def number(value):
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (TypeError,ValueError):return None


def cohort(trade):
    # Observational grouping, not a claim that legacy policy provenance is complete.
    config=json.loads(trade.get('config_json') or '{}')
    policy=json.loads(trade.get('policy_json') or '{}')
    frozen={k:policy[k] for k in ('framework','target_selection','stop_offset','target_offset','holding_hours') if k in policy}
    fingerprint=hashlib.sha256(json.dumps([config,frozen],sort_keys=True).encode()).hexdigest()[:10]
    return f"{trade.get('strategy_version') or 'legacy'} / {fingerprint}"


def statistics(rows):
    pnl=[r['net_profit'] for r in rows]
    wins=sum(x>0 for x in pnl); gains=sum(x for x in pnl if x>0);loss=-sum(x for x in pnl if x<0)
    return dict(closed_positions=len(pnl),net_profit=round(sum(pnl),2),
        average_net_profit=round(sum(pnl)/len(pnl),2) if pnl else None,
        win_rate_pct=round(100*wins/len(pnl),1) if pnl else None,
        profit_factor=round(gains/loss,3) if loss else None,
        reported_fees=round(sum(r['reported_fees'] or 0 for r in rows),2),
        fees_available=sum(r['reported_fees'] is not None for r in rows))



def paired_shadows(conn, start, end):
    rows=[{**dict(r), **json.loads(r['payload'])} for r in conn.execute("SELECT * FROM shadow_exits WHERE status NOT IN ('ACTIVE','INVALID')")]
    baseline={(r['trade_id'],r['horizon_hours']):r for r in rows if r['variant']=='production'}
    pairs=[]
    for row in rows:
        if row['variant']=='production':continue
        base=baseline.get((row['trade_id'],row['horizon_hours']))
        if not base:continue
        times=[stamp(row.get('exit_at')),stamp(base.get('exit_at'))]
        if any(t is None for t in times) or not start<=max(times)<end:continue
        actual=number(base.get('estimated_net_pnl'));alternative=number(row.get('estimated_net_pnl'))
        if actual is None or alternative is None:continue
        pairs.append(dict(trade_id=row['trade_id'],variant=row['variant'],horizon_hours=row['horizon_hours'],
            production_estimated_net=round(actual,2),alternative_estimated_net=round(alternative,2),
            estimated_difference=round(alternative-actual,2),
            observation_gaps=int(row.get('gap_count') or 0)+int(base.get('gap_count') or 0)))
    return pairs

def build_evidence(conn, start, end, minimum_sample=20):
    trades=[dict(r) for r in conn.execute('SELECT * FROM trades ORDER BY id')]
    # One actual broker position counts once, even if linked to multiple lifecycles.
    by_position={}
    for trade in trades:
        pid=str(trade.get('broker_position_id') or '').removesuffix('.0')
        if pid:by_position.setdefault(pid,trade)
    outcomes=[];cumulative=[];missing_pnl=0
    for row in conn.execute('SELECT * FROM broker_closed_positions'):
        closed=stamp(row['closed_at'])
        if closed is None or closed>=end:continue
        net=number(row['net_profit'])
        if net is None:
            missing_pnl+=int(start<=closed)
            continue
        trade=by_position.get(str(row['position_id']).removesuffix('.0'),{})
        outcome=dict(position_id=row['position_id'],trade_id=trade.get('id'),symbol=trade.get('symbol','Unlinked'),
            side=trade.get('side','Unknown'),policy_cohort=cohort(trade),net_profit=net,
            reported_fees=number(row['fees']),closed_at=row['closed_at'])
        cumulative.append(outcome)
        if start<=closed:outcomes.append(outcome)
    groups={}
    for field in ('side','symbol','policy_cohort'):
        grouped={}
        for outcome in outcomes:grouped.setdefault(outcome[field],[]).append(outcome)
        groups[field]={key:statistics(rows) for key,rows in grouped.items()}
    open_rows=[t for t in trades if t['status'] in ACTIVE and t.get('broker_position_id')]
    open_unique={str(t['broker_position_id']).removesuffix('.0'):t for t in open_rows}
    active_actions=[dict(r) for r in conn.execute("SELECT kind,state,attempts,alert_at FROM broker_actions WHERE state NOT IN ('APPLIED','CLOSED','SUPERSEDED','RESOLVED')")]
    shadow_counts={r['status']:r['n'] for r in conn.execute('SELECT status,count(*) n FROM shadow_exits GROUP BY status')}
    audit=conn.execute('SELECT id,created_at,evidence FROM configuration_audits ORDER BY id DESC LIMIT 1').fetchone()
    audit_summary=None
    if audit:
        saved=json.loads(audit['evidence'])
        audit_summary=dict(id=audit['id'],created_at=audit['created_at'],trades_checked=saved['total_trades_audited'],trades_with_issues=saved.get('trades_with_issues',0))
    paths=[]
    for row in conn.execute("SELECT trade_id,created_at,details FROM trade_events WHERE stage='validation' ORDER BY id"):
        generated=stamp(row['created_at'])
        if generated is None or not start<=generated<end:continue
        data=json.loads(row['details'] or '{}')
        if data.get('validation_type')!='historical_trade_outcome':continue
        paths.append(dict(trade_id=row['trade_id'],created_at=row['created_at'],available=bool(data.get('available')),
            first_exit=data.get('first_exit'),scope=data.get('validation_scope'),window_complete=data.get('window_complete'),
            reason=data.get('reason')))
    summary=statistics(outcomes)
    pairs=paired_shadows(conn,start,end)
    recommendations=[]
    if summary['closed_positions']<minimum_sample:
        recommendations.append(f"Only {summary['closed_positions']} broker-confirmed net outcomes in this period; fewer than the {minimum_sample}-position review threshold. Continue observation; no strategy adjustment is justified by this sample alone.")
    elif summary['net_profit']<0:
        recommendations.append('Net realized outcome is negative. Investigate policy cohorts, costs and loss concentration before proposing a controlled shadow experiment; do not infer the cause from aggregate P/L.')
    else:
        recommendations.append('Net realized outcome is positive in this period. Validate consistency across policy cohorts and later periods before increasing exposure.')
    attention=sum(bool(a['alert_at']) for a in active_actions)
    if audit_summary and audit_summary['trades_with_issues']:
        recommendations.append(f"Position Manager's latest configuration audit flags {audit_summary['trades_with_issues']} trades for review; inspect its recorded checks separately from broker recovery.")
    if paths:recommendations.append(f'{len(paths)} historical path analyses were generated in this period. These are retrospective reference-price scenarios, not broker-confirmed returns.')
    if attention:recommendations.append(f'{attention} unresolved broker actions need operational attention before interpreting execution quality.')
    if missing_pnl:recommendations.append(f'{missing_pnl} closed broker positions lack net P/L and are excluded from return metrics.')
    if not shadow_counts:recommendations.append('No shadow observations are available for a paired comparison. Keep the existing experiment running.')
    else:recommendations.append(f'{len(pairs)} completed same-trade, same-horizon shadow pairs in this period. Differences use estimated net costs and are exploratory; inspect gaps and policy cohorts before proposing a new experiment.')
    return dict(summary=summary,cumulative_summary=statistics(cumulative),outcomes=outcomes,groups=groups,missing_net_outcomes=missing_pnl,
        current_operations=dict(open_positions=len(open_unique),pending_closes=sum(t['status']=='EXIT_PENDING' for t in open_unique.values()),
            gross_entry_exposure=round(sum(abs(float(t.get('broker_fill_units') or t.get('quantity') or 0))*float(t.get('broker_fill_price') or t.get('entry_price') or 0) for t in open_unique.values()),2),
            unresolved_actions=len(active_actions),attention_actions=attention),
        configuration_audit=audit_summary,historical_path_analyses=paths,
        shadow_status_counts=shadow_counts,shadow_pairs=pairs,recommendations=recommendations,
        limitations=['Broker-reported net P/L is used as reported; fees are shown separately and are not subtracted twice.',
            'Open-position exposure and action state are current snapshots, not realized returns or historical period-end exposure.',
            'Legacy policy grouping is observational; captured settings may be incomplete. No causal attribution or profitability proof.',
            'Quote-observed shadow outcomes can miss intrabar touches. This review does not fabricate historical comparisons.'],
        advisory_only=True,minimum_sample=minimum_sample)


class PerformanceAnalystAgent:
    """Scheduled evidence and recommendations; never changes rules or submits orders."""
    def __init__(self, connect, rules=None):self.connect=connect;self.rules=rules or {}

    def run_due(self, now=None):
        if not self.rules.get('enabled',True):return []
        now=now or datetime.now(timezone.utc)
        local=now.astimezone(TZ);today=local.replace(hour=0,minute=0,second=0,microsecond=0)
        monday=today-timedelta(days=today.weekday())
        windows=[('daily',today-timedelta(days=1),today),('weekly',monday-timedelta(days=7),monday)]
        result=[];c=self.connect()
        try:
            for cadence,start,end in windows:
                key=start.date().isoformat()
                if c.execute('SELECT 1 FROM performance_reviews WHERE cadence=? AND period_key=?',(cadence,key)).fetchone():continue
                evidence=build_evidence(c,start.astimezone(timezone.utc),end.astimezone(timezone.utc),int(self.rules.get('minimum_closed_positions',20)))
                cur=c.execute('INSERT OR IGNORE INTO performance_reviews(cadence,period_key,created_at,window_start,window_end,version,evidence) VALUES (?,?,?,?,?,?,?)',
                    (cadence,key,now.isoformat(),start.isoformat(),end.isoformat(),VERSION,json.dumps(evidence,sort_keys=True)))
                if cur.rowcount:result.append(dict(id=cur.lastrowid,cadence=cadence,summary=evidence['summary']))
            c.commit()
        finally:c.close()
        return result


def list_reviews(conn, limit=30):
    return [{**dict(r),'evidence':json.loads(r['evidence'])} for r in conn.execute('SELECT * FROM performance_reviews ORDER BY id DESC LIMIT ?',(limit,))]
