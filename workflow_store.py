"""Durable account leases, reversible analysis cleanup, and broker evidence."""
from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
import hashlib
import json
import time
import uuid
import zlib

OPERATIONAL_DETAIL_LIMIT=16*1024
OPERATIONAL_SUMMARY_KEYS={'trade_id','ticker','symbol','company_name','signal','confidence','status','decision',
    'analysis_status','analysis_decision','execution_status','execution_reasons','reasons','mode','strategy',
    'candidate_pool_size','approved_candidate_count','opened_count','max_trades_per_cycle','selection_rank',
    'selection_score','automation_enabled','broker_order_submitted','neutral_reason'}


def compact_trade_event_detail(stage, value):
    """Replace same-event copies with immutable local evidence references."""
    detail=copy.deepcopy(value or {})
    if not isinstance(detail,dict):return detail
    def reference(container,key,path):
        if isinstance(container,dict) and key in container:
            container[key]={'evidence_ref':path}
    if stage=='strategy' and isinstance(detail.get('candidate_context'),dict):
        analysis=detail.get('analysis') if isinstance(detail.get('analysis'),dict) else {}
        gate=detail.get('decision_gate') if isinstance(detail.get('decision_gate'),dict) else {}
        for container in (analysis,gate):
            reference(container,'daily_trend_context','candidate_context.daily_trend_context')
            reference(container,'market_quote','candidate_context.market_quote')
            reference(container,'fundamentals','candidate_context.fundamentals')
        reference(analysis,'decision_gate','decision_gate')
        detail['evidence_layout']='candidate_context is canonical; evidence_ref paths resolve within this immutable event'
    if stage=='risk':
        quote=detail.get('broker_quote')
        if isinstance(quote,dict) and 'instrument' in quote and detail.get('broker_instrument'):
            quote['instrument']={'evidence_ref':'broker_instrument'}
        target=((detail.get('protection') or {}).get('target_selection') if isinstance(detail.get('protection'),dict) else None)
        if isinstance(target,dict) and 'structure' in target and (detail.get('daily_trend_context') or {}).get('price_structure'):
            target['structure']={'evidence_ref':'daily_trend_context.price_structure'}
        detail['evidence_layout']='daily_trend_context and broker_instrument are canonical; evidence_ref paths resolve within this immutable event'
    return detail


def compact_operational_detail(value):
    """Keep operational indexes small; trade_events remain the canonical evidence log."""
    encoded=json.dumps(value or {},sort_keys=True,separators=(',',':'),default=str).encode()
    if len(encoded)<=OPERATIONAL_DETAIL_LIMIT:return value or {}
    source=value if isinstance(value,dict) else {}
    summary={key:source[key] for key in OPERATIONAL_SUMMARY_KEYS if key in source and
             (source[key] is None or isinstance(source[key],(str,int,float,bool)) or
              len(json.dumps(source[key],default=str))<=2048)}
    trades=source.get('trades')
    if isinstance(trades,list):
        summary['trades']=[{key:item.get(key) for key in ('trade_id','symbol','side','status','broker_order_submitted','reason') if key in item}
                           for item in trades[:100] if isinstance(item,dict)]
    summary['evidence_storage']='trade_events'
    summary['operational_compaction']={'original_bytes':len(encoded),'sha256':hashlib.sha256(encoded).hexdigest(),
        'omitted_keys':sorted(str(key) for key in source if key not in summary)}
    return summary


def compact_operational_history(conn, *, apply=False):
    """Preview/apply removal of duplicated research/cycle JSON outside trade_events."""
    candidates=[];before=after=0
    for table,column,where in (('signals','payload','length(payload)>?'),
            ('agent_events','details',"action IN ('dynamic_news_research','news_strategy_cycle') AND length(details)>?")):
        for row in conn.execute(f'SELECT id,{column} value FROM {table} WHERE {where}',(OPERATIONAL_DETAIL_LIMIT,)):
            try:value=json.loads(row['value'] or '{}')
            except (TypeError,ValueError):continue
            compact=json.dumps(compact_operational_detail(value),sort_keys=True)
            if len(compact)>=len(row['value']):continue
            candidates.append((table,int(row['id']),len(row['value']),len(compact)))
            before+=len(row['value']);after+=len(compact)
            if apply:conn.execute(f'UPDATE {table} SET {column}=? WHERE id=?',(compact,row['id']))
    return {'rows':len(candidates),'before_bytes':before,'after_bytes':after,'reclaimable_bytes':before-after,
            'applied':bool(apply),'tables':{table:sum(1 for item in candidates if item[0]==table) for table in ('signals','agent_events')}}


def purge_operational_history(conn, retention_days=7, now=None, *, apply=False):
    """Expire legacy signal polling rows; keep the newest row for each sector."""
    from datetime import datetime,timedelta,timezone
    now=now or datetime.now(timezone.utc);cutoff=(now-timedelta(days=int(retention_days))).strftime('%Y-%m-%d %H:%M:%S')
    predicate='created_at<? AND id NOT IN (SELECT max(id) FROM signals GROUP BY sector)'
    count,payload_bytes=conn.execute(f'SELECT count(*),coalesce(sum(length(payload)),0) FROM signals WHERE {predicate}',(cutoff,)).fetchone()
    if apply and count:conn.execute(f'DELETE FROM signals WHERE {predicate}',(cutoff,))
    return {'count':int(count),'payload_bytes':int(payload_bytes),'retention_days':int(retention_days),'cutoff':cutoff,'applied':bool(apply)}


def ensure_schema(conn):
    import position_policy
    position_policy.ensure_schema(conn)
    position_policy.ensure_price_trailing_schema(conn)
    import performance_analysis
    performance_analysis.ensure_schema(conn)
    import broker_recovery
    broker_recovery.ensure_schema(conn)
    import shadow_tracking
    shadow_tracking.ensure_schema(conn)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS workflow_leases (name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS archived_analysis (trade_id INTEGER PRIMARY KEY, archived_at TEXT DEFAULT CURRENT_TIMESTAMP, payload BLOB NOT NULL);
    CREATE TABLE IF NOT EXISTS broker_snapshots (id INTEGER PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP, equity REAL NOT NULL, cash REAL, benchmark REAL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS broker_closed_positions (position_id TEXT PRIMARY KEY, closed_at TEXT, net_profit REAL, fees REAL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS article_companies (article_key TEXT NOT NULL, ticker TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(article_key,ticker));
    CREATE TABLE IF NOT EXISTS analysis_restores (trade_id INTEGER PRIMARY KEY, restored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS maintenance_runs (id INTEGER PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP, action TEXT, details TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS neutral_research_cache (symbol TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL, analyzed_at REAL NOT NULL, reuse_count INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS evidence_blobs (sha256 TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, payload TEXT NOT NULL);
    """)

    import outcome_notifications
    outcome_notifications.ensure_schema(conn)


class AccountBusy(ValueError):
    pass


@asynccontextmanager
async def lease(connection_factory, name: str):
    owner = str(uuid.uuid4())
    conn = connection_factory()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM workflow_leases WHERE name = ? AND expires_at < ?", (name, time.time()))
        cursor = conn.execute("INSERT OR IGNORE INTO workflow_leases VALUES (?,?,?)", (name, owner, time.time()+300))
        conn.commit()
        if not cursor.rowcount:
            raise AccountBusy(f"{name} is already running; retry after the current account operation completes.")
    finally:
        conn.close()

    holder = asyncio.current_task()

    async def renew():
        while True:
            await asyncio.sleep(15)
            c = connection_factory()
            try:
                renewed = c.execute("UPDATE workflow_leases SET expires_at=? WHERE name=? AND owner=?", (time.time()+300, name, owner))
                c.commit()
                if renewed.rowcount != 1:
                    raise RuntimeError("Account operation lease was lost.")
            except Exception:
                holder.cancel()
                raise
            finally:
                c.close()

    task = asyncio.create_task(renew())
    try:
        yield owner
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        c = connection_factory()
        try:
            c.execute("DELETE FROM workflow_leases WHERE name=? AND owner=?", (name, owner))
            c.commit()
        finally:
            c.close()


def purge_archived_analysis(conn, retention_days=7, now=None, *, apply=False, basis='archived_at'):
    """Preview or purge expired never-submitted archives by original lifecycle age."""
    from datetime import datetime, timedelta, timezone
    if not 1 <= int(retention_days) <= 365:
        raise ValueError("Archived analysis retention must be 1–365 days.")
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=int(retention_days))).strftime("%Y-%m-%d %H:%M:%S")
    if basis not in {'archived_at','created_at'}:raise ValueError('Archive purge basis must be archived_at or created_at.')
    expired=[];compressed_bytes=0
    for row in conn.execute("SELECT trade_id,archived_at,payload FROM archived_analysis ORDER BY trade_id"):
        value=row['archived_at']
        if basis=='created_at':
            content=json.loads(zlib.decompress(row["payload"]));value=(content.get('trade') or {}).get('created_at')
        if str(value or "") < cutoff:
            expired.append(int(row["trade_id"]));compressed_bytes+=len(row["payload"])
    if apply and expired:
        conn.executemany("DELETE FROM archived_analysis WHERE trade_id=?",((trade_id,) for trade_id in expired))
    return {"count":len(expired),"trade_ids":expired,"compressed_bytes":compressed_bytes,
            "retention_days":int(retention_days),"cutoff":cutoff,"basis":basis,"applied":bool(apply)}


def archive_analysis(conn, retention_days=3, now=None, archive_retention_days=7):
    """Hide recent never-submitted analysis, then purge it after the restore window."""
    from datetime import datetime, timedelta, timezone
    if not 1 <= int(retention_days) <= 365:
        raise ValueError("Analysis retention must be 1–365 days.")
    if int(archive_retention_days)<int(retention_days):
        raise ValueError("Archived analysis retention must be at least the active analysis retention.")
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=int(retention_days))).strftime("%Y-%m-%d %H:%M:%S")
    archived = []
    conn.execute("BEGIN IMMEDIATE")
    rows = conn.execute("""SELECT * FROM trades WHERE created_at < ?
        AND status IN ('ANALYSIS_ONLY','DEFERRED_CAPACITY','REJECTED_RESEARCH','REJECTED_STRATEGY','REJECTED_RISK','REJECTED_EXECUTION')
        AND broker_order_id IS NULL AND broker_position_id IS NULL AND order_reference IS NULL AND allocated_capital=0 AND quantity=0
        AND NOT EXISTS (SELECT 1 FROM analysis_restores r WHERE r.trade_id=trades.id AND r.restored_at >= ?)""", (cutoff, cutoff)).fetchall()
    for row in rows:
        trade = dict(row)
        events = [dict(e) for e in conn.execute("SELECT * FROM trade_events WHERE trade_id=?", (trade["id"],))]
        if any(e["stage"] == "execution" and (
            e["decision"] in {"submitted", "executed", "uncertain"}
            or (json.loads(e.get("details") or "{}")).get("broker_order_submitted")
        ) for e in events):
            continue
        content = zlib.compress(json.dumps({"trade": trade, "events": events}).encode())
        conn.execute("INSERT INTO archived_analysis(trade_id,payload) VALUES (?,?)", (trade["id"], content))
        conn.execute("DELETE FROM trade_events WHERE trade_id=?", (trade["id"],))
        conn.execute("DELETE FROM trades WHERE id=?", (trade["id"],))
        archived.append(trade["id"])
    purged=purge_archived_analysis(conn,archive_retention_days,now,apply=True)
    conn.execute("INSERT INTO maintenance_runs(action,details) VALUES ('archive_analysis',?)", (json.dumps({"count": len(archived), "retention_days": retention_days,
        "purged":purged["count"],"purged_compressed_bytes":purged["compressed_bytes"],"archive_retention_days":archive_retention_days}),))
    conn.commit()
    return archived


def restore_analysis(conn, trade_id):
    row = conn.execute("SELECT payload FROM archived_analysis WHERE trade_id=?", (trade_id,)).fetchone()
    if not row:
        raise ValueError("Archived lifecycle not found.")
    content = json.loads(zlib.decompress(row["payload"]))
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, records in (("trades", [content["trade"]]), ("trade_events", content["events"])):
            for record in records:
                columns = list(record)
                conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(record.values()))
        conn.execute("INSERT INTO analysis_restores(trade_id) VALUES (?) ON CONFLICT(trade_id) DO UPDATE SET restored_at=CURRENT_TIMESTAMP", (trade_id,))
        conn.execute("DELETE FROM archived_analysis WHERE trade_id=?", (trade_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def record_snapshot(conn, account, positions, benchmark=None):
    if not account.get("available") or not positions.get("available"):
        return
    conn.execute("INSERT INTO broker_snapshots(equity,cash,benchmark,payload) VALUES (?,?,?,?)",
                 (account["equity"], account.get("cash_available"), benchmark,
                  json.dumps({"account": account, "positions": positions})))
    conn.commit()


def broker_performance(conn):
    snapshots = [dict(row) for row in conn.execute("SELECT id,created_at,equity,cash,benchmark FROM broker_snapshots ORDER BY id")]
    closed = [dict(row) for row in conn.execute("SELECT position_id,net_profit,fees FROM broker_closed_positions")]
    result = {"status": "inconclusive", "reason": "Cash-flow history and independent forward outcomes are not fully verified.",
              "snapshot_count": len(snapshots), "closed_positions": len(closed),
              "reported_closed_net_profit": sum(r["net_profit"] or 0 for r in closed),
              "reported_closed_fees": sum(r["fees"] or 0 for r in closed),
              "fees_complete": bool(closed) and all(r["fees"] is not None for r in closed)}
    groups = {}
    for row in conn.execute("SELECT t.strategy_version,t.policy_json,h.net_profit FROM trades t JOIN broker_closed_positions h ON h.position_id=t.broker_position_id WHERE h.net_profit IS NOT NULL"):
        policy = json.loads(row["policy_json"] or "{}")
        kind = "saved_fixture_test" if policy.get("test_workflow_id") else policy.get("framework", "legacy")
        key = f"{row['strategy_version'] or 'legacy'} / {kind}"
        group = groups.setdefault(key, {"closed_positions": 0, "net_profit": 0.0, "wins": 0})
        group["closed_positions"] += 1
        group["net_profit"] += row["net_profit"]
        group["wins"] += int(row["net_profit"] > 0)
    result["strategy_outcomes"] = groups
    if snapshots:
        first, last = snapshots[0], snapshots[-1]
        payload = json.loads(conn.execute("SELECT payload FROM broker_snapshots WHERE id=?", (last["id"],)).fetchone()[0])
        positions = payload.get("positions", {}).get("positions", [])
        result["positions_snapshot"] = {**payload.get("positions", {}), "fetched_at": payload.get("positions", {}).get("fetched_at") or last["created_at"]}
        peak = first["equity"]
        drawdown = 0
        for r in snapshots:
            peak = max(peak, r["equity"])
            drawdown = max(drawdown, (peak-r["equity"])/peak)
        result.update({"broker_equity": last["equity"], "cash_available": last["cash"],
                       "first_observed_equity": first["equity"],
                       "invested_capital": sum(float(p.get("amount") or 0) for p in positions),
                       "gross_exposure": sum(abs(float(p.get("units") or 0)*float(p.get("current_rate") or p.get("open_rate") or 0)) for p in positions),
                       "unrealized_pnl": payload.get("account", {}).get("unrealized_pnl"),
                       "equity_change_pct": (last["equity"]/first["equity"]-1)*100,
                       "observed_equity_drawdown_pct": drawdown*100,
                       "benchmark_return_pct": (last["benchmark"]/first["benchmark"]-1)*100 if first["benchmark"] and last["benchmark"] else None,
                       "as_of": last["created_at"], "source": "Recorded eToro demo account snapshots"})
    return result
