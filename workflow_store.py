"""Durable account leases, reversible analysis cleanup, and broker evidence."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import time
import uuid
import zlib


def ensure_schema(conn):
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
    """)


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


def archive_analysis(conn, retention_days=3, now=None):
    """Hide old never-submitted analysis while retaining a compressed restore copy."""
    from datetime import datetime, timedelta, timezone
    if not 1 <= int(retention_days) <= 365:
        raise ValueError("Analysis retention must be 1–365 days.")
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
    conn.execute("INSERT INTO maintenance_runs(action,details) VALUES ('archive_analysis',?)", (json.dumps({"count": len(archived), "retention_days": retention_days}),))
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
