#!/usr/bin/env python3
"""Read-only research/target audit; no model calls, replay orders or DB writes."""
import argparse
import collections
import json
import sqlite3
from pathlib import Path


def audit(database, since):
    conn = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    neutral = collections.defaultdict(list)
    economics = {}
    strategy = collections.Counter()
    for row in conn.execute("SELECT t.id,t.symbol,e.stage,e.decision,e.details FROM trades t JOIN trade_events e ON e.trade_id=t.id WHERE t.created_at>=? ORDER BY e.id", (since,)):
        d = json.loads(row["details"] or "{}")
        if row["stage"] == "research" and d.get("signal") == "neutral":
            headlines = d.get("evidence", {}).get("headlines", [])
            titles = tuple(sorted({str(h.get("title") or "").casefold() for h in headlines}))
            neutral[row["symbol"]].append(titles)
        if row["stage"] == "strategy" and row["decision"] == "rejected":
            strategy.update(d.get("decision_gate", {}).get("rejection_reasons", []))
        if row["stage"] == "risk" and d.get("proposed_units") is not None:
            protection = d.get("protection") or {}
            stop_risk = d.get("planned_stop_risk")
            net = d.get("estimated_net_target_profit")
            if stop_risk and net is not None:
                economics[row["id"]] = {"id": row["id"], "symbol": row["symbol"],
                    "entry": d.get("proposed_entry"), "stop": d.get("proposed_stop"), "target": d.get("proposed_take_profit"),
                    "units": d["proposed_units"], "estimated_costs": d.get("estimated_costs"),
                    "net_target_profit": net, "planned_stop_risk": stop_risk, "net_reward_risk": net/stop_risk,
                    "structure_adjusted": (protection.get("target_selection") or {}).get("adjusted"),
                    "reasons": d.get("reasons", []) + d.get("execution_reasons", [])}
    conn.close()
    return {"since_utc": since, "strategy_rejections": dict(strategy),
        "repeated_neutrals": [{"symbol": s, "evaluations": len(v), "distinct_headline_sets": len(set(v))} for s, v in neutral.items() if len(v)>1],
        "sized_candidates": list(economics.values())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--since", required=True, help="UTC SQLite timestamp")
    args = parser.parse_args()
    print(json.dumps(audit(args.db, args.since), indent=2))
