import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import time
import uuid
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from trading_workflow import VERSION as WORKFLOW_VERSION, size_order, mean_reversion_setup, reaction_from_bars, exit_reason
import workflow_store
from email_notifications import send_trade_execution_email
from market_sessions import build_market_session_context

# Load environment variables from .env.config if it exists
env_config_path = Path(__file__).resolve().parent / ".env.config"
if env_config_path.exists():
    with open(env_config_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)

from integrations import (
    EtoroBrokerAdapter,
    IntegrationSettings,
    OpenAIResearchAdapter,
    YahooFinanceNewsProvider,
    fetch_company_fundamentals,
    fetch_market_quote,
    fetch_market_trends,
    submit_broker_order,
    validate_trade_price_path,
)
from strategy import (
    BacktestCosts,
    STRATEGY_VERSION,
    configured_symbols,
    evaluate_latest_signal,
    load_daily_bars,
    run_portfolio_backtest,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("FINBOT_DB_PATH", str(Path(__file__).resolve().parent / "finbot.db"))).expanduser().resolve()
DATASET_PATH = Path(__file__).resolve().parent / "research_feed.json"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
PROFILE_AVATARS = {
    "account_circle": "Classic",
    "face": "Face",
    "support_agent": "Support",
    "terminal": "Builder",
    "bolt": "Bolt",
    "psychology": "Analyst",
}

DEFAULT_AGENT_RULES: Dict[str, Dict[str, Any]] = {
    "researcher": {"enabled": True, "status": "idle", "min_confidence": 0.6, "allowed_sectors": ["Technology", "Energy"], "max_fetch_per_cycle": 6, "include_market_fundamentals": True, "default_leverage": 2},
    "strategist": {
        "enabled": True,
        "status": "idle",
        "min_confidence": 0.7,
        "fundamental_confirmation_min_confidence": 0.6,
        "countertrend_short_min_confidence": 0.85,
        "countertrend_long_min_confidence": 0.85,
        "conditional_mean_reversion_min_atr_multiple": 1.25,
        "conditional_mean_reversion_min_confidence": 0.7,
        "conditional_mean_reversion_confirmation_atr": 0.2,
        "conditional_mean_reversion_stop_cushion_atr": 0.25,
        "strong_fundamentals_min_revenue_growth_pct": 15.0,
        "strong_fundamentals_min_net_profit_margin_pct": 20.0,
        "strong_fundamentals_max_debt_to_equity": 1.0,
        "weak_fundamentals_max_revenue_growth_pct": 0.0,
        "weak_fundamentals_max_net_profit_margin_pct": 5.0,
        "weak_fundamentals_min_debt_to_equity": 2.0,
        "require_company_specific_news_for_fundamental_confirmation": True,
        "require_company_specific_news_for_countertrend_short": True,
        "require_company_specific_news_for_countertrend_long": True,
        "require_current_market_quote": True,
        "default_leverage": 2,
        "max_leverage": 3,
        "position_limit": 0.35,
    },
    "risk_manager": {
        "enabled": True,
        "status": "idle",
        "max_drawdown": 0.05,
        "max_volatility": 0.03,
        "exit_on_risk_breach": True,
        "stop_loss_pct": 0.02,
        "openai_trend_review_enabled": True,
        "trend_opposition_min_confidence": 0.8,
        "risk_per_trade_pct": float(os.getenv("FINBOT_RISK_PER_TRADE_PCT", "0.005")),
        "max_total_risk_pct": float(os.getenv("FINBOT_MAX_TOTAL_RISK_PCT", "0.02")),
        "max_gross_exposure_pct": float(os.getenv("FINBOT_MAX_GROSS_EXPOSURE_PCT", "1.0")),
        "max_open_positions": int(os.getenv("FINBOT_MAX_OPEN_POSITIONS", "30")),
        "max_single_position_exposure_pct": 0.10,
        "stress_gap_pct": 0.05,
        "max_stressed_loss_pct": 0.10,
        "sector_exposure_enabled": False,
        "max_sector_exposure_pct": 0.35,
        "stop_atr_multiple": 2.0,
        "target_reward_risk": 1.5,
        "minimum_reward_risk": 1.0,
        "news_max_hold_hours": 48.0,
        "mean_reversion_max_hold_hours": 24.0,
        "estimated_roundtrip_cost_bps": 10.0,
        "estimated_financing_annual_pct": 8.0,
    },
    "execution": {"enabled": True, "status": "idle", "paper_trading_only": True, "max_trade_value": 2500, "max_trades_per_cycle": 2, "allowed_side": ["LONG", "SHORT"], "max_order_units": 5.0, "max_order_spend_usd": 200.0, "analysis_retention_days": 3, "max_research_age_hours": 24.0},
}

INSTRUMENT_NAMES = {
    "AAPL": "Apple Inc.",
    "AMD": "Advanced Micro Devices, Inc.",
    "CL": "WTI Crude Oil Futures",
    "META": "Meta Platforms, Inc.",
    "MSFT": "Microsoft Corporation",
    "NVDA": "NVIDIA Corporation",
    "USOIL": "WTI Crude Oil",
    "XLE": "Energy Select Sector SPDR Fund",
    "XOM": "Exxon Mobil Corporation",
}
BASE_TAKE_PROFIT_PCT = 0.05
_benchmark_cache = {"until": 0.0, "value": None}


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def serialized_account(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        async with workflow_store.lease(get_connection, "etoro_demo_account"):
            return await function(*args, **kwargs)
    return wrapped


def load_news_reaction(symbol: str, headlines: list) -> dict:
    timestamp = newest_known_article_time(headlines)
    if not timestamp:
        return {"available": False, "reason": "A news timestamp is needed to measure its market reaction."}
    try:
        import yfinance as yf
        bars = yf.Ticker(symbol).history(period="5d", interval="5m", auto_adjust=True, actions=False)
        return reaction_from_bars(bars, datetime.fromtimestamp(timestamp, timezone.utc).isoformat())
    except Exception as error:
        return {"available": False, "reason": f"Post-news price history unavailable ({type(error).__name__})."}


def cleanup_stale_analysis() -> dict:
    days = int(get_agent_rules()["execution"].get("analysis_retention_days", 3))
    conn = get_connection()
    try:
        ids = workflow_store.archive_analysis(conn, days)
        return {"archived": len(ids), "retention_days": days}
    finally:
        conn.close()


def restore_archived_analysis(trade_id: int) -> None:
    conn = get_connection()
    try:
        workflow_store.restore_analysis(conn, trade_id)
    finally:
        conn.close()


def get_broker_performance() -> dict:
    conn = get_connection()
    try:
        result = workflow_store.broker_performance(conn)
        result["archived_analysis_count"] = conn.execute("SELECT COUNT(*) FROM archived_analysis").fetchone()[0]
        return result
    finally:
        conn.close()


@serialized_account
async def manage_etoro_demo_positions(settings=None) -> dict:
    """Independent maintenance: broker history, fill-based protection and exits."""
    settings = settings or IntegrationSettings.from_environment()
    if settings.broker_provider != "etoro" or not settings.etoro_demo_trading:
        return {"status": "not_demo"}
    adapter = EtoroBrokerAdapter(settings)
    account, snapshot, history = await asyncio.gather(
        asyncio.to_thread(adapter.get_account_snapshot), asyncio.to_thread(adapter.get_positions),
        asyncio.to_thread(adapter.get_closed_positions), return_exceptions=True)
    if not isinstance(snapshot, dict) or not snapshot.get("available"):
        return {"status": "broker_unavailable"}
    positions = snapshot["positions"]
    conn = get_connection()
    rows = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE status IN ('SUBMITTING','SUBMITTED_PENDING_RECONCILIATION','OPEN','RECONCILIATION_REQUIRED','EXIT_PENDING','CLOSED_PNL_UNAVAILABLE') AND (broker_order_id IS NOT NULL OR broker_position_id IS NOT NULL OR order_reference IS NOT NULL)")]
    conn.close()
    closed = {str(p["position_id"]): p for p in history.get("positions", []) if p.get("position_id")} if isinstance(history, dict) and history.get("available") else {}
    for trade in rows:
        position_id = str(trade.get("broker_position_id") or "").removesuffix(".0")
        position = next((p for p in positions if str(p.get("position_id")).removesuffix(".0") == position_id), None)
        if not position and (trade.get("order_reference") or trade.get("broker_order_id")) and trade["status"] in {"SUBMITTING", "SUBMITTED_PENDING_RECONCILIATION", "RECONCILIATION_REQUIRED"}:
            try:
                lookup = await asyncio.to_thread(adapter.lookup_order, order_id=trade.get("broker_order_id"), reference_id=trade.get("order_reference"))
                executions = lookup.get("positionExecutions") or []
                if len(executions) == 1:
                    position_id = str(executions[0]["positionId"])
                    conn = get_connection()
                    conn.execute("UPDATE trades SET broker_position_id=?, broker_order_id=COALESCE(?,broker_order_id), status='SUBMITTED_PENDING_RECONCILIATION' WHERE id=?", (position_id, str(lookup.get("orderId")) if lookup.get("orderId") is not None else None, trade["id"]))
                    conn.commit(); conn.close()
                    position = next((p for p in positions if str(p.get("position_id")) == position_id), None)
                elif str((lookup.get("status") or {}).get("name", "")).lower() in {"rejected", "cancelled", "canceled"}:
                    conn = get_connection()
                    conn.execute("UPDATE trades SET status='REJECTED_EXECUTION', reconciliation_status='broker_confirmed_rejection', allocated_capital=0, quantity=0 WHERE id=?", (trade["id"],))
                    conn.commit(); conn.close()
            except Exception as error:
                logger.warning("Order lookup unresolved for lifecycle %s: %s", trade["id"], type(error).__name__)
        completed = closed.get(position_id)
        if completed and not position:
            conn = get_connection()
            conn.execute("INSERT INTO broker_closed_positions(position_id,closed_at,net_profit,fees,payload) VALUES (?,?,?,?,?) ON CONFLICT(position_id) DO UPDATE SET closed_at=excluded.closed_at,net_profit=excluded.net_profit,fees=excluded.fees,payload=excluded.payload", (position_id, completed.get("closed_at"), completed.get("net_profit"), completed.get("fees"), json.dumps(completed)))
            conn.execute("UPDATE trades SET status=?, closed_at=?, close_rate=?, realized_pnl=?, broker_fees=COALESCE(?,broker_fees), reconciliation_status='broker_history_confirmed' WHERE id=?", ("CLOSED" if completed.get("net_profit") is not None else "CLOSED_PNL_UNAVAILABLE", completed.get("closed_at"), completed.get("close_rate"), completed.get("net_profit") or 0, completed.get("fees"), trade["id"]))
            conn.commit(); conn.close()
            record_trade_event(trade["id"], "reconciliation", "execution", "completed", "accepted", "Broker closed-trade history confirms closure.", completed)
            continue
        if not position or trade["status"] == "EXIT_PENDING":
            continue
        policy = json.loads(trade.get("policy_json") or "{}")
        # Existing historical positions keep their original levels. New orders
        # carry a versioned policy whose directional ATR levels follow the fill.
        stop, target = trade.get("stop_loss_price"), trade.get("take_profit_price")
        if trade.get("strategy_version") == WORKFLOW_VERSION and not policy.get("fill_adjusted") and position.get("open_rate"):
            if policy.get("framework") != "CONDITIONAL_MEAN_REVERSION":
                stop = float(position["open_rate"]) + float(policy["stop_offset"])
                target = float(position["open_rate"]) + float(policy["target_offset"])
            policy["fill_adjusted"] = True
            conn = get_connection()
            conn.execute("UPDATE trades SET stop_loss_price=?,take_profit_price=?,policy_json=?,opened_at=COALESCE(opened_at,?) WHERE id=?", (stop, target, json.dumps(policy), position.get("opened_at") or datetime.now(timezone.utc).isoformat(), trade["id"]))
            conn.commit(); conn.close()
            trade.update(stop_loss_price=stop, take_profit_price=target, opened_at=trade.get("opened_at") or position.get("opened_at") or datetime.now(timezone.utc).isoformat())
        matches = stop is not None and position.get("stop_loss_price") is not None and abs(float(stop)-float(position["stop_loss_price"])) <= max(.01,abs(float(stop))*.0001)
        tp_matches = target is None and position.get("take_profit_price") is None or target is not None and position.get("take_profit_price") is not None and abs(float(target)-float(position["take_profit_price"])) <= max(.01,abs(float(target))*.0001)
        last_patch = float(policy.get("last_protection_request", 0))
        # The public API does not document a way to remove an existing take
        # profit from an open position.  Keep reporting that mismatch, but do
        # not repeatedly submit an unsupported clear request.  A requested
        # numeric target can still be corrected together with the stop.
        protection_update_needed = not matches or (target is not None and not tp_matches)
        protection_signature = json.dumps(
            {"stop_loss_price": round(float(stop), 8), "take_profit_price": round(float(target), 8) if target is not None else None},
            sort_keys=True,
        ) if stop is not None else None
        already_requested = policy.get("last_protection_request_signature") == protection_signature
        if stop and protection_update_needed and not already_requested and time.time()-last_patch > 300:
            try:
                response = await asyncio.to_thread(adapter.modify_position, position_id, float(stop), float(target) if target is not None else None)
                policy["last_protection_request"] = time.time()
                policy["last_protection_request_signature"] = protection_signature
                conn = get_connection()
                conn.execute("UPDATE trades SET policy_json=?, reconciliation_status='protection_update_pending' WHERE id=?", (json.dumps(policy), trade["id"]))
                conn.commit(); conn.close()
                record_trade_event(trade["id"], "reconciliation", "execution", "waiting", "pending", "Broker accepted a protection update; verification awaits a new snapshot.", response)
            except Exception as error:
                logger.warning("Protection update failed for %s: %s", trade["id"], type(error).__name__)
        if trade.get("strategy_version") != WORKFLOW_VERSION:
            continue
        captured = json.loads(trade.get("config_json") or "{}")
        caps = captured.get("execution", DEFAULT_AGENT_RULES["execution"])
        actual_units = abs(float(position.get("units") or 0))
        actual_spend = position.get("amount")
        if actual_spend is None:
            actual_spend = actual_units*float(position.get("open_rate") or 0)/max(1,float(position.get("leverage") or trade["leverage"]))
        size_violation = actual_units > float(caps["max_order_units"])+.000001 or float(actual_spend)+max(0,float(position.get("fees") or 0)) > float(caps["max_order_spend_usd"])+.01
        mark = position.get("current_rate") or position.get("open_rate")
        reason = None
        try:
            rate = await asyncio.to_thread(adapter._get_rate, int(position["instrument_id"]))
            mark = rate.get("bid" if trade["side"] == "LONG" else "ask")
        except Exception:
            mark = None
        if mark:
            reason = exit_reason({**trade, "policy": policy}, float(mark))
        else:
            # Time expiry needs no invented price to close a known position.
            reason = exit_reason({**trade, "policy": policy, "stop_loss_price": None, "take_profit_price": None}, 0)
        if not reason:
            conn = get_connection()
            latest = conn.execute("SELECT details FROM trade_events WHERE stage='research' AND trade_id IN (SELECT id FROM trades WHERE symbol=?) ORDER BY id DESC LIMIT 1", (trade["symbol"],)).fetchone()
            conn.close()
            evidence = json.loads(latest["details"]) if latest else {}
            ai = evidence.get("openai") or {}
            if ai.get("reaction_assessment") == "structural" and float(evidence.get("confidence", 0)) >= .7 and evidence.get("signal") == ("bearish" if trade["side"] == "LONG" else "bullish"):
                reason = "structural_news_invalidated_thesis"
        if size_violation:
            reason = "actual_fill_exceeded_configured_units_or_spend_cap"
        if reason:
            try:
                response = await asyncio.to_thread(adapter.close_position, position_id)
                conn = get_connection()
                conn.execute("UPDATE trades SET status='EXIT_PENDING',reconciliation_status='close_submitted' WHERE id=?", (trade["id"],))
                conn.commit(); conn.close()
                record_trade_event(trade["id"], "exit", "execution", "submitted", "pending", reason, response)
            except Exception as error:
                record_trade_event(trade["id"], "exit", "execution", "waiting", "unavailable", f"Close request failed: {type(error).__name__}", {})
    reconciliation = reconcile_news_demo_positions(positions)
    sync_linked_test_workflows()
    if time.time() >= _benchmark_cache["until"]:
        try:
            bars = await asyncio.to_thread(load_daily_bars, "SPY", "1y")
            _benchmark_cache.update(until=time.time()+3600, value=float(bars.iloc[-1]["Close"]) if bars is not None and not bars.empty else None)
        except Exception:
            _benchmark_cache.update(until=time.time()+3600, value=None)
    conn = get_connection()
    if isinstance(account, dict):
        workflow_store.record_snapshot(conn, account, snapshot, _benchmark_cache["value"])
    conn.close()
    return {"status": "completed", "reconciliation": reconciliation}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(stored_hash: str, password: str) -> bool:
    if stored_hash.startswith("scrypt$"):
        _, encoded_salt, encoded_digest = stored_hash.split("$", 2)
        salt = base64.urlsafe_b64decode(encoded_salt)
        expected = base64.urlsafe_b64decode(encoded_digest)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    legacy_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(stored_hash, legacy_hash)


def ensure_db() -> None:
    logger.info("Initializing SQLite database at %s", DB_PATH)
    conn = get_connection()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        logger.warning("WAL initialization was busy; retaining the current journal mode until next initialization.")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, avatar TEXT NOT NULL DEFAULT 'account_circle', is_admin INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, sector TEXT NOT NULL, signal TEXT NOT NULL, catalyst TEXT NOT NULL, confidence REAL NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS news_articles (id INTEGER PRIMARY KEY AUTOINCREMENT, article_key TEXT NOT NULL UNIQUE, ticker TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', published_at TEXT, fetched_at TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', relevance_status TEXT NOT NULL DEFAULT 'unreviewed', relevance_tickers TEXT NOT NULL DEFAULT '', relevance_reason TEXT NOT NULL DEFAULT '', relevance_analyzed_at TEXT);
        CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, symbol TEXT NOT NULL, side TEXT NOT NULL, leverage REAL NOT NULL, exit_target REAL NOT NULL, status TEXT NOT NULL, notes TEXT, allocated_capital REAL NOT NULL DEFAULT 0, entry_price REAL, current_price REAL, quantity REAL NOT NULL DEFAULT 0, realized_pnl REAL NOT NULL DEFAULT 0, closed_at TEXT, company_name TEXT NOT NULL DEFAULT '', stop_loss_price REAL, take_profit_price REAL, broker_order_id TEXT, broker_position_id TEXT, broker_fill_price REAL, broker_fill_units REAL, broker_fees REAL, stop_state TEXT NOT NULL DEFAULT 'unknown', reconciliation_status TEXT NOT NULL DEFAULT 'unverified', signal_timestamp TEXT, signal_provider TEXT, quote_provider TEXT, quote_timestamp TEXT);
        CREATE TABLE IF NOT EXISTS trade_events (id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER NOT NULL REFERENCES trades(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, stage TEXT NOT NULL, agent_name TEXT NOT NULL, status TEXT NOT NULL, decision TEXT NOT NULL, message TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS risk_events (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, pnl REAL NOT NULL, volatility REAL NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT NOT NULL UNIQUE, rules_json TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS agent_events (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, agent_name TEXT NOT NULL, status TEXT NOT NULL, action TEXT NOT NULL, message TEXT NOT NULL, decision TEXT NOT NULL DEFAULT 'pending', details TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS openai_usage_events (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, model TEXT NOT NULL, prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL, rate_limits_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS openai_usage_history (id INTEGER PRIMARY KEY AUTOINCREMENT, period_started_at TEXT NOT NULL, period_ended_at TEXT NOT NULL, tokens_used INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS auth_sessions (token_id TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS company_analytics (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL UNIQUE, company_name TEXT NOT NULL, sector TEXT NOT NULL, times_analyzed INTEGER NOT NULL DEFAULT 1, last_analyzed_at TEXT NOT NULL, last_signal TEXT, last_confidence REAL, avg_confidence REAL NOT NULL DEFAULT 0.0, trade_count INTEGER NOT NULL DEFAULT 0, trades_accepted INTEGER NOT NULL DEFAULT 0, acceptance_rate REAL NOT NULL DEFAULT 0.0, last_updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS research_cycle_companies (id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_timestamp TEXT NOT NULL, ticker TEXT NOT NULL, signal TEXT NOT NULL, confidence REAL NOT NULL, UNIQUE(cycle_timestamp, ticker));
        CREATE TABLE IF NOT EXISTS test_workflows (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, symbol TEXT NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL, broker_order_id TEXT, broker_position_id TEXT, details_json TEXT NOT NULL DEFAULT '{}');
        """
    )
    workflow_store.ensure_schema(conn)

    existing_user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    for column, definition in {
        "avatar": "TEXT NOT NULL DEFAULT 'account_circle'",
        "is_admin": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if column not in existing_user_columns:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")

    existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    for column, definition in {
        "allocated_capital": "REAL NOT NULL DEFAULT 0", "entry_price": "REAL", "current_price": "REAL",
        "quantity": "REAL NOT NULL DEFAULT 0", "realized_pnl": "REAL NOT NULL DEFAULT 0", "closed_at": "TEXT",
        "company_name": "TEXT NOT NULL DEFAULT ''", "stop_loss_price": "REAL", "take_profit_price": "REAL",
        "broker_order_id": "TEXT", "broker_position_id": "TEXT", "broker_fill_price": "REAL",
        "broker_fill_units": "REAL", "broker_fees": "REAL", "stop_state": "TEXT NOT NULL DEFAULT 'unknown'",
        "reconciliation_status": "TEXT NOT NULL DEFAULT 'unverified'", "signal_timestamp": "TEXT",
        "signal_provider": "TEXT", "quote_provider": "TEXT", "quote_timestamp": "TEXT",
        "strategy_version": "TEXT", "policy_json": "TEXT NOT NULL DEFAULT '{}'",
        "order_reference": "TEXT", "opened_at": "TEXT", "close_rate": "REAL",
        "config_json": "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {column} {definition}")

    existing_news_columns = {row["name"] for row in conn.execute("PRAGMA table_info(news_articles)")}
    for column, definition in {
        "relevance_status": "TEXT NOT NULL DEFAULT 'unreviewed'",
        "relevance_tickers": "TEXT NOT NULL DEFAULT ''",
        "relevance_reason": "TEXT NOT NULL DEFAULT ''",
        "relevance_analyzed_at": "TEXT",
    }.items():
        if column not in existing_news_columns:
            conn.execute(f"ALTER TABLE news_articles ADD COLUMN {column} {definition}")

    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_trade_events_trade_id_id ON trade_events(trade_id,id);
        CREATE INDEX IF NOT EXISTS idx_trades_symbol_id ON trades(UPPER(symbol),id);
        CREATE INDEX IF NOT EXISTS idx_trades_created_id ON trades(created_at,id);
        CREATE INDEX IF NOT EXISTS idx_agent_events_agent_id ON agent_events(agent_name,id);
        CREATE INDEX IF NOT EXISTS idx_agent_events_created_agent ON agent_events(created_at,agent_name);
        CREATE INDEX IF NOT EXISTS idx_news_articles_fetched ON news_articles(fetched_at);
    """)

    starting_capital = float(os.getenv("FINBOT_STARTING_CAPITAL", "10000"))
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('starting_capital', ?)",
        (str(starting_capital),),
    )
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('auth_signing_key', ?)", (secrets.token_urlsafe(48),))
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('ai_research_enabled', 'false')")
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('openai_tokens_used', '0')")
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('openai_tokens_synced_at', '')")
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('research_schedule_mode', 'manual')")
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('research_schedule_interval_minutes', '60')")
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('research_schedule_suspended_until', '')")
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('research_schedule_last_run', '')")
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('research_candidate_offset', '0')")
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('etoro_api_debug_logging', 'false')")
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('strategy_evaluation_json', '')")
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('demo_automated_trading_enabled', 'true')")
    # Older releases silently switched this preference off after a broker
    # reconciliation mismatch. Restore the requested default once on upgrade;
    # subsequent user changes remain persistent.
    automation_default_migration = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'demo_automation_default_migration_v1'"
    ).fetchone()
    if not automation_default_migration:
        conn.execute(
            "UPDATE app_settings SET value = 'true' WHERE key = 'demo_automated_trading_enabled'"
        )
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('demo_automation_default_migration_v1', 'complete')"
        )
    user_count = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    if user_count == 0:
        bootstrap_password = os.getenv("FINBOT_PASSWORD")
        if not bootstrap_password:
            conn.close()
            raise ValueError("FINBOT_PASSWORD is required to create the first administrator.")
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)",
            (os.getenv("FINBOT_USERNAME", "admin"), hash_password(bootstrap_password)),
        )
    admin_count = conn.execute("SELECT COUNT(*) AS count FROM users WHERE is_admin = 1").fetchone()["count"]
    if admin_count == 0:
        first_user = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if first_user:
            conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (first_user["id"],))
    for agent_name, rules in DEFAULT_AGENT_RULES.items():
        existing = conn.execute(
            "SELECT rules_json FROM agent_rules WHERE agent_name = ?",
            (agent_name,),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO agent_rules (agent_name, rules_json) VALUES (?, ?)",
                (agent_name, json.dumps(rules, sort_keys=True)),
            )
        else:
            saved_rules = json.loads(existing["rules_json"])
            merged_rules = {**rules, **saved_rules}
            leverage_keys = {
                "researcher": ("default_leverage",),
                "strategist": ("default_leverage", "max_leverage"),
            }.get(agent_name, ())
            for key in leverage_keys:
                try:
                    value = float(merged_rules[key])
                    if math.isfinite(value) and value.is_integer():
                        merged_rules[key] = int(value)
                except (KeyError, TypeError, ValueError):
                    pass
            serialized_rules = json.dumps(merged_rules, sort_keys=True)
            if serialized_rules != existing["rules_json"]:
                conn.execute(
                    "UPDATE agent_rules SET rules_json = ?, updated_at = CURRENT_TIMESTAMP WHERE agent_name = ?",
                    (serialized_rules, agent_name),
                )
    conn.commit()
    conn.close()


def load_research_feed() -> List[Dict[str, Any]]:
    if not DATASET_PATH.exists():
        return [
            {
                "timestamp": "2026-10-03T09:00:00Z",
                "sector": "Technology",
                "signal": "bullish",
                "catalyst": "earnings",
                "confidence": 0.82,
                "ticker": "AAPL",
                "notes": "Strong earnings guidance and platform spending rebound.",
            },
            {
                "timestamp": "2026-10-03T09:15:00Z",
                "sector": "Energy",
                "signal": "bearish",
                "catalyst": "OPEC_supply",
                "confidence": 0.68,
                "ticker": "USOIL",
                "notes": "Supply uptick and softer macro sentiment.",
            },
            {
                "timestamp": "2026-10-03T09:30:00Z",
                "sector": "Technology",
                "signal": "neutral",
                "catalyst": "valuation",
                "confidence": 0.44,
                "ticker": "MSFT",
                "notes": "Momentum is positive but valuation risk is elevated.",
            },
            {
                "timestamp": "2026-10-03T09:45:00Z",
                "sector": "Energy",
                "signal": "bullish",
                "catalyst": "macro_shift",
                "confidence": 0.76,
                "ticker": "XLE",
                "notes": "Demand pick-up and lower inventories support a positive turn.",
            },
            {
                "timestamp": "2026-10-03T10:00:00Z",
                "sector": "Technology",
                "signal": "bearish",
                "catalyst": "guidance",
                "confidence": 0.52,
                "ticker": "NVDA",
                "notes": "Margin pressure and slower enterprise pacing.",
            },
        ]

    with DATASET_PATH.open("r", encoding="utf-8") as f:
        payload = json.load(f)
        if isinstance(payload, list):
            return payload
    return []


def format_json_readable(data: Any) -> str:
    """Format JSON with comma-separated large numbers for readability."""
    def format_value(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: format_value(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [format_value(item) for item in obj]
        elif isinstance(obj, (int, float)) and abs(obj) >= 1000:
            if isinstance(obj, int):
                return f"{obj:,}"
            else:
                return f"{obj:,.2f}"
        else:
            return obj
    
    if isinstance(data, str):
        data = json.loads(data)
    
    formatted = format_value(data)
    return json.dumps(formatted, indent=2)


def calculate_leveraged_exit_prices(
    entry_price: Optional[float],
    side: str,
    leverage: float,
    stop_loss_pct: float,
    base_exit_target: float,
) -> tuple[Optional[float], Optional[float], float]:
    if leverage <= 0:
        raise ValueError("Trade leverage must be greater than zero.")
    if entry_price is None:
        return None, None, base_exit_target
    if side == "HOLD":
        return entry_price, entry_price, 1.0
    if side not in {"LONG", "SHORT"}:
        raise ValueError(f"Unsupported position side: {side}")

    stop_distance = stop_loss_pct / leverage
    adjusted_exit_target = 1 + (base_exit_target - 1) / leverage
    stop_loss_price = (
        entry_price * (1 - stop_distance)
        if side == "LONG"
        else entry_price * (1 + stop_distance)
    )
    take_profit_price = entry_price * adjusted_exit_target
    return stop_loss_price, take_profit_price, adjusted_exit_target


def get_agent_rules() -> Dict[str, Dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute("SELECT agent_name, rules_json FROM agent_rules").fetchall()
    conn.close()

    data: Dict[str, Dict[str, Any]] = {
        name: dict(rules) for name, rules in DEFAULT_AGENT_RULES.items()
    }
    for row in rows:
        data.setdefault(row["agent_name"], {}).update(json.loads(row["rules_json"]))
    return data


def get_ai_research_enabled() -> bool:
    conn = get_connection()
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'ai_research_enabled'").fetchone()
    conn.close()
    return bool(row and row["value"].lower() == "true")


def get_etoro_api_debug_logging() -> bool:
    conn = get_connection()
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'etoro_api_debug_logging'"
    ).fetchone()
    conn.close()
    return bool(row and row["value"].strip().lower() in {"1", "true", "yes", "on"})


def set_etoro_api_debug_logging(enabled: bool) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('etoro_api_debug_logging', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("true" if enabled else "false",),
    )
    conn.commit()
    conn.close()


def get_strategy_evaluation() -> Dict[str, Any]:
    conn = get_connection()
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'strategy_evaluation_json'").fetchone()
    conn.close()
    if not row or not row["value"]:
        return {"status": "not_run", "reason": "Historical evaluation has not run; this does not block demo execution."}
    try:
        result = json.loads(row["value"])
    except json.JSONDecodeError:
        return {"status": "inconclusive", "reason": "Saved strategy evaluation is invalid."}
    if result.get("evaluation_method") == "unavailable_without_historical_news_replay":
        return result
    expected_costs = BacktestCosts.from_environment()
    if (
        result.get("strategy") != STRATEGY_VERSION
        or result.get("symbols_used") != sorted(configured_symbols())
        or not expected_costs
        or result.get("cost_assumptions") != expected_costs.__dict__
        or result.get("risk_assumptions") != {
            "risk_per_trade_pct": _parse_risk_setting("FINBOT_RISK_PER_TRADE_PCT", 0.005),
            "max_total_risk_pct": _parse_risk_setting("FINBOT_MAX_TOTAL_RISK_PCT", 0.02),
            "max_positions": int(_parse_risk_setting("FINBOT_MAX_OPEN_POSITIONS", 5)),
            "max_gross_exposure_pct": _parse_risk_setting("FINBOT_MAX_GROSS_EXPOSURE_PCT", 1.0),
            "max_drawdown_pct": _parse_risk_setting("FINBOT_BACKTEST_MAX_DRAWDOWN_PCT", 20),
        }
    ):
        return {"status": "inconclusive", "reason": "Strategy, symbol universe, or configured costs changed; rerun evaluation."}
    return result


def get_demo_automated_trading_enabled() -> bool:
    conn = get_connection()
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'demo_automated_trading_enabled'").fetchone()
    conn.close()
    return bool(row and row["value"].strip().lower() in {"1", "true", "yes", "on"})


def set_demo_automated_trading_enabled(enabled: bool) -> None:
    settings = IntegrationSettings.from_environment()
    if enabled and settings.broker_provider != "etoro":
        raise ValueError("Select eToro as the broker provider before enabling demo automation.")
    if enabled and not settings.etoro_demo_trading:
        raise ValueError("Live execution is disabled; ETORO_DEMO_TRADING must remain true.")
    # Historical evaluation is a performance report, not an execution lock.
    # Demo execution still has per-order account, quote, sizing, and stop checks.
    conn = get_connection()
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('demo_automated_trading_enabled', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("true" if enabled else "false",),
    )
    conn.commit()
    conn.close()


async def _run_daily_trend_strategy_evaluation_retired() -> Dict[str, Any]:
    costs = BacktestCosts.from_environment()
    if costs is None:
        result = {
            "status": "inconclusive",
            "reason": "Set FINBOT_BACKTEST_SPREAD_BPS_PER_SIDE, FINBOT_BACKTEST_COMMISSION_BPS_PER_SIDE, FINBOT_BACKTEST_FINANCING_ANNUAL_PCT, and FINBOT_BACKTEST_FX_BPS_PER_SIDE before evaluation.",
            "strategy": STRATEGY_VERSION,
            "symbols_used": configured_symbols(),
        }
    else:
        symbols = configured_symbols()
        universe_errors: Dict[str, str] = {}
        resolved_symbols: list[str] = []
        if not symbols:
            universe_errors["<universe>"] = "The configured symbol allowlist is empty."
        try:
            adapter = EtoroBrokerAdapter(IntegrationSettings.from_environment())
        except Exception as error:
            adapter = None
            universe_errors["<eToro>"] = f"{type(error).__name__}: {error}"
        if adapter:
            for symbol in symbols:
                try:
                    instrument = await asyncio.to_thread(adapter.resolve_instrument, symbol)
                    if not instrument.get("tradeable") or not instrument.get("supported_asset"):
                        raise ValueError("eToro did not confirm a tradeable stock or ETF.")
                    if str(instrument.get("currency") or "").upper() != "USD":
                        raise ValueError("Only USD-denominated instruments are currently supported for risk conversion.")
                    resolved_symbols.append(symbol)
                except Exception as error:
                    universe_errors[symbol] = f"{type(error).__name__}: {error}"
        if universe_errors:
            result = {
                "status": "inconclusive",
                "reason": "The complete configured universe could not be confirmed as tradeable eToro USD stocks/ETFs.",
                "universe_errors": universe_errors,
                "symbols_configured": symbols,
                "symbols_resolved": resolved_symbols,
                "strategy": STRATEGY_VERSION,
            }
            symbols = []
        if not symbols:
            results = []
        else:
            results = await asyncio.gather(
                *(asyncio.to_thread(load_daily_bars, symbol, "5y") for symbol in symbols),
                return_exceptions=True,
            )
        if symbols:
            bars = {symbol: result for symbol, result in zip(symbols, results) if not isinstance(result, Exception)}
            risk_assumptions = {
                "risk_per_trade_pct": _parse_risk_setting("FINBOT_RISK_PER_TRADE_PCT", 0.005),
                "max_total_risk_pct": _parse_risk_setting("FINBOT_MAX_TOTAL_RISK_PCT", 0.02),
                "max_positions": int(_parse_risk_setting("FINBOT_MAX_OPEN_POSITIONS", 5)),
                "max_gross_exposure_pct": _parse_risk_setting("FINBOT_MAX_GROSS_EXPOSURE_PCT", 1.0),
                "max_drawdown_pct": _parse_risk_setting("FINBOT_BACKTEST_MAX_DRAWDOWN_PCT", 20),
            }
            result = await asyncio.to_thread(
                run_portfolio_backtest, bars, costs, leverage=2,
                risk_per_trade_pct=risk_assumptions["risk_per_trade_pct"],
                max_total_risk_pct=risk_assumptions["max_total_risk_pct"],
                max_positions=risk_assumptions["max_positions"],
                max_gross_exposure_pct=risk_assumptions["max_gross_exposure_pct"],
            )
            result["risk_assumptions"] = risk_assumptions
            result["symbols_configured"] = symbols
            if set(result.get("symbols_used", [])) != set(symbols):
                result["status"] = "inconclusive"
                result["reason"] = "Not all configured symbols have sufficient historical data; evaluation is not comparable across the full universe."
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('strategy_evaluation_json', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps(result, sort_keys=True),),
    )
    conn.commit()
    conn.close()
    return result


async def run_strategy_evaluation() -> Dict[str, Any]:
    """Report the current news strategy's historical-evaluation evidence status."""
    result = {
        "status": "inconclusive",
        "reason": "Historical point-in-time news and research decisions are not archived for a chronological performance backtest. This is informational and does not gate eToro demo execution.",
        "strategy": STRATEGY_VERSION,
        "evaluation_method": "unavailable_without_historical_news_replay",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    conn = get_connection()
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('strategy_evaluation_json', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps(result, sort_keys=True),),
    )
    conn.commit()
    conn.close()
    return result


def set_ai_research_enabled(enabled: bool) -> None:
    if enabled and not IntegrationSettings.from_environment().openai_api_key:
        raise ValueError("Set OPENAI_API_KEY in .env.config before enabling OpenAI research.")
    conn = get_connection()
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('ai_research_enabled', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("true" if enabled else "false",),
    )
    conn.commit()
    conn.close()


def record_openai_usage(model: str, usage: Dict[str, int], rate_limits: Dict[str, str]) -> None:
    total_tokens = max(0, int(usage.get("total_tokens", 0)))
    conn = get_connection()
    conn.execute(
        "INSERT INTO openai_usage_events (model, prompt_tokens, completion_tokens, total_tokens, rate_limits_json) VALUES (?, ?, ?, ?, ?)",
        (model, max(0, int(usage.get("prompt_tokens", 0))), max(0, int(usage.get("completion_tokens", 0))), total_tokens, json.dumps(rate_limits, sort_keys=True)),
    )
    conn.execute(
        "UPDATE app_settings SET value = CAST(CAST(value AS INTEGER) + ? AS TEXT) WHERE key = 'openai_tokens_used'",
        (total_tokens,),
    )
    conn.commit()
    conn.close()


def sync_openai_token_allowance() -> None:
    conn = get_connection()
    conn.execute("BEGIN IMMEDIATE")
    now = datetime.now(timezone.utc).isoformat()
    settings = {
        row["key"]: row["value"]
        for row in conn.execute(
            "SELECT key, value FROM app_settings WHERE key IN ('openai_tokens_used', 'openai_tokens_synced_at')"
        )
    }
    tokens_used = int(settings.get("openai_tokens_used", "0"))
    if tokens_used > 0:
        conn.execute(
            "INSERT INTO openai_usage_history (period_started_at, period_ended_at, tokens_used) VALUES (?, ?, ?)",
            (settings.get("openai_tokens_synced_at") or now, now, tokens_used),
        )
    conn.executemany(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (("openai_tokens_used", "0"), ("openai_tokens_synced_at", now)),
    )
    conn.commit()
    conn.close()


def get_openai_usage_summary() -> Dict[str, Any]:
    conn = get_connection()
    settings = {row["key"]: row["value"] for row in conn.execute(
        "SELECT key, value FROM app_settings WHERE key IN ('openai_tokens_used', 'openai_tokens_synced_at')"
    )}
    latest = conn.execute(
        "SELECT created_at, model, prompt_tokens, completion_tokens, total_tokens, rate_limits_json FROM openai_usage_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    history = [dict(row) for row in conn.execute(
        "SELECT period_started_at, period_ended_at, tokens_used FROM openai_usage_history ORDER BY id DESC"
    )]
    conn.close()
    used = int(settings.get("openai_tokens_used", "0"))
    return {
        "used": used,
        "synced_at": settings.get("openai_tokens_synced_at", ""),
        "history": history,
        "latest": {
            **dict(latest), "rate_limits": json.loads(latest["rate_limits_json"])
        } if latest else None,
    }


def configure_research_schedule(mode: str, interval_minutes: int) -> None:
    if mode not in {"manual", "automatic"}:
        raise ValueError("Research mode must be manual or automatic.")
    if not 1 <= interval_minutes <= 1440:
        raise ValueError("Research interval must be between 1 minute and 24 hours.")
    conn = get_connection()
    values = {
        "research_schedule_mode": mode,
        "research_schedule_interval_minutes": str(interval_minutes),
    }
    conn.executemany(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        values.items(),
    )
    conn.commit()
    conn.close()


def suspend_research_schedule(resume_at: Optional[str]) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('research_schedule_suspended_until', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (resume_at or "",),
    )
    conn.commit()
    conn.close()


def get_research_schedule() -> Dict[str, Any]:
    conn = get_connection()
    settings = {row["key"]: row["value"] for row in conn.execute(
        "SELECT key, value FROM app_settings WHERE key LIKE 'research_schedule_%'"
    )}
    conn.close()
    interval = int(settings.get("research_schedule_interval_minutes", "60"))
    suspended_until = settings.get("research_schedule_suspended_until", "")
    last_run = settings.get("research_schedule_last_run", "")
    now = datetime.now(timezone.utc)
    resume_at = datetime.fromisoformat(suspended_until) if suspended_until else None
    is_suspended = bool(resume_at and resume_at > now)
    next_run = datetime.fromisoformat(last_run) + timedelta(minutes=interval) if last_run else now
    if is_suspended:
        next_run = max(next_run, resume_at)
    return {
        "mode": settings.get("research_schedule_mode", "manual"),
        "interval_minutes": interval,
        "suspended_until": suspended_until,
        "suspended": is_suspended,
        "last_run": last_run,
        "next_run": next_run.isoformat(),
    }


def get_latest_research_cycle_status() -> Optional[Dict[str, Any]]:
    conn = get_connection()
    candidate_pool = conn.execute(
        """
        SELECT id, created_at, message
        FROM agent_events
        WHERE agent_name = 'researcher' AND action = 'candidate_pool'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not candidate_pool:
        conn.close()
        return None

    outcome = conn.execute(
        """
        SELECT id, created_at, message, decision, details
        FROM agent_events
        WHERE action = 'cycle_trade_selection' AND id > ?
        ORDER BY id
        LIMIT 1
        """,
        (candidate_pool["id"],),
    ).fetchone()
    end_id = outcome["id"] if outcome else None
    rejection_query = """
        SELECT agent_name, action, message
        FROM agent_events
        WHERE id > ? AND decision = 'rejected' AND status = 'completed'
          AND action NOT IN ('candidate_pool', 'cycle_trade_selection')
    """
    rejection_parameters: List[Any] = [candidate_pool["id"]]
    if end_id is not None:
        rejection_query += " AND id < ?"
        rejection_parameters.append(end_id)
    rejection_query += " ORDER BY id DESC LIMIT 8"
    rejections = conn.execute(rejection_query, rejection_parameters).fetchall()
    conn.close()

    details = json.loads(outcome["details"]) if outcome and outcome["details"] else {}
    return {
        "started_at": candidate_pool["created_at"],
        "candidate_pool_message": candidate_pool["message"],
        "completed_at": outcome["created_at"] if outcome else None,
        "outcome_message": outcome["message"] if outcome else None,
        "outcome_decision": outcome["decision"] if outcome else None,
        "outcome_details": details,
        "rejections": [dict(row) for row in rejections],
    }


def claim_due_research_cycle(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        settings = {row["key"]: row["value"] for row in conn.execute(
            "SELECT key, value FROM app_settings WHERE key LIKE 'research_schedule_%'"
        )}
        if settings.get("research_schedule_mode", "manual") != "automatic":
            conn.rollback()
            return False
        suspended_until = settings.get("research_schedule_suspended_until", "")
        if suspended_until and datetime.fromisoformat(suspended_until) > now:
            conn.rollback()
            return False
        last_run = settings.get("research_schedule_last_run", "")
        due_at = datetime.fromisoformat(last_run) + timedelta(
            minutes=int(settings.get("research_schedule_interval_minutes", "60"))
        ) if last_run else now
        if due_at > now:
            conn.rollback()
            return False
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('research_schedule_last_run', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (now.isoformat(),),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def update_company_analytics(ticker: str, company_name: str, sector: str, signal: str, confidence: float, accepted: bool) -> None:
    """Update company analytics after analysis."""
    conn = get_connection()
    existing = conn.execute(
        "SELECT times_analyzed, avg_confidence, trade_count, trades_accepted FROM company_analytics WHERE ticker = ?",
        (ticker,)
    ).fetchone()
    
    now = datetime.now(timezone.utc).isoformat()
    if existing:
        new_times = existing["times_analyzed"] + 1
        new_avg_confidence = (existing["avg_confidence"] * existing["times_analyzed"] + confidence) / new_times
        new_trades = existing["trade_count"] + 1
        new_accepted = existing["trades_accepted"] + (1 if accepted else 0)
        acceptance_rate = new_accepted / new_trades if new_trades > 0 else 0.0
        
        conn.execute(
            """UPDATE company_analytics 
               SET times_analyzed = ?, last_analyzed_at = ?, last_signal = ?, last_confidence = ?,
                   avg_confidence = ?, trade_count = ?, trades_accepted = ?, acceptance_rate = ?,
                   last_updated_at = ?
               WHERE ticker = ?""",
            (new_times, now, signal, confidence, new_avg_confidence, new_trades, new_accepted,
             acceptance_rate, now, ticker)
        )
    else:
        acceptance_rate = 1.0 if accepted else 0.0
        conn.execute(
            """INSERT INTO company_analytics 
               (ticker, company_name, sector, times_analyzed, last_analyzed_at, last_signal,
                last_confidence, avg_confidence, trade_count, trades_accepted, acceptance_rate, last_updated_at)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (ticker, company_name, sector, now, signal, confidence, confidence,
             1 if accepted else 0, acceptance_rate, now)
        )
    conn.commit()
    conn.close()


def record_research_cycle_company(cycle_timestamp: str, ticker: str, signal: str, confidence: float) -> None:
    """Record which companies were analyzed in this research cycle."""
    conn = get_connection()
    conn.execute(
        """INSERT INTO research_cycle_companies (cycle_timestamp, ticker, signal, confidence)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(cycle_timestamp, ticker) DO UPDATE SET signal = excluded.signal, confidence = excluded.confidence""",
        (cycle_timestamp, ticker, signal, confidence)
    )
    conn.commit()
    conn.close()


def get_company_analytics(days: int = 30) -> List[Dict[str, Any]]:
    """Get aggregated analytics per company."""
    conn = get_connection()
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT * FROM company_analytics 
           WHERE last_analyzed_at >= ? 
           ORDER BY times_analyzed DESC, acceptance_rate DESC, avg_confidence DESC""",
        (cutoff_date,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_company_analysis_history(ticker: str, days: int = 30) -> List[Dict[str, Any]]:
    """Get analysis history for a specific company."""
    conn = get_connection()
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    
    company_data = conn.execute(
        "SELECT * FROM company_analytics WHERE ticker = ?", (ticker,)
    ).fetchone()
    
    cycles = conn.execute(
        """SELECT cycle_timestamp, signal, confidence FROM research_cycle_companies
           WHERE ticker = ? AND cycle_timestamp >= ?
           ORDER BY cycle_timestamp DESC""",
        (ticker, cutoff_date)
    ).fetchall()
    
    trades = conn.execute(
        """SELECT id, created_at, side, status, entry_price, current_price, realized_pnl,
                  allocated_capital, leverage, quantity, take_profit_price
           FROM trades
           WHERE symbol = ? AND created_at >= ?
           ORDER BY created_at DESC""",
        (ticker, cutoff_date)
    ).fetchall()
    
    conn.close()
    return {
        "company": dict(company_data) if company_data else None,
        "analysis_cycles": [dict(row) for row in cycles],
        "trades": [dict(row) for row in trades],
    }


def get_least_analyzed_companies(limit: int = 10) -> List[Dict[str, Any]]:
    """Get companies analyzed least frequently to reduce repetition."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM company_analytics 
           ORDER BY times_analyzed ASC, last_analyzed_at ASC LIMIT ?""",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_decision_timeline(start_at: Optional[str] = None, end_at: Optional[str] = None) -> List[Dict[str, Any]]:
    conditions = []
    parameters: List[str] = []
    if start_at:
        conditions.append("created_at >= ?")
        parameters.append(start_at)
    if end_at:
        conditions.append("created_at <= ?")
        parameters.append(end_at)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    conn = get_connection()
    rows = conn.execute(
        f"SELECT created_at, agent_name, decision FROM agent_events {where_clause} ORDER BY id ASC",
        parameters,
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def update_agent_rules(agent_name: str, new_rules: Dict[str, Any]) -> Dict[str, Any]:
    current = get_agent_rules()
    merged = current.get(agent_name, DEFAULT_AGENT_RULES.get(agent_name, {}))
    merged.update(new_rules)
    for key, value in new_rules.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(float(value)):
                raise ValueError(f"{key} must be finite.")
            if key in {"max_order_units", "max_order_spend_usd", "analysis_retention_days", "max_research_age_hours", "stop_atr_multiple", "target_reward_risk", "max_open_positions", "risk_per_trade_pct", "max_total_risk_pct", "max_gross_exposure_pct", "max_single_position_exposure_pct", "max_stressed_loss_pct", "max_sector_exposure_pct", "news_max_hold_hours", "mean_reversion_max_hold_hours", "conditional_mean_reversion_min_atr_multiple", "conditional_mean_reversion_confirmation_atr", "conditional_mean_reversion_stop_cushion_atr"} and float(value) <= 0:
                raise ValueError(f"{key} must be greater than zero.")
            if key in {"stress_gap_pct", "estimated_roundtrip_cost_bps", "estimated_financing_annual_pct"} and float(value) < 0:
                raise ValueError(f"{key} cannot be negative.")
            if "confidence" in key and not 0 <= float(value) <= 1:
                raise ValueError("Confidence thresholds must be between zero and one.")
    for key in ("default_leverage", "max_leverage", "max_open_positions", "max_trades_per_cycle", "analysis_retention_days"):
        if key in merged and (float(merged[key]) < 1 or not float(merged[key]).is_integer()):
            raise ValueError(f"{key} must be a positive integer.")
    if int(merged.get("analysis_retention_days", 3)) > 365:
        raise ValueError("Analysis retention cannot exceed 365 days.")
    if agent_name == "risk_manager" and not merged.get("enabled", True):
        raise ValueError("Risk Manager is a required execution gate.")
    if agent_name == "execution" and any(side not in {"LONG", "SHORT"} for side in merged.get("allowed_side", [])):
        raise ValueError("Execution directions must be LONG and/or SHORT.")

    conn = get_connection()
    conn.execute(
        "UPDATE agent_rules SET rules_json = ?, updated_at = CURRENT_TIMESTAMP WHERE agent_name = ?",
        (json.dumps(merged, sort_keys=True), agent_name),
    )
    conn.commit()
    conn.close()
    return merged


def record_agent_event(
    agent_name: str,
    status: str,
    action: str,
    message: str,
    decision: str = "pending",
    details: Optional[Dict[str, Any]] = None,
) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO agent_events (agent_name, status, action, message, decision, details) VALUES (?, ?, ?, ?, ?, ?)",
        (
            agent_name,
            status,
            action,
            message,
            decision,
            json.dumps(details or {}, sort_keys=True),
        ),
    )
    conn.commit()
    conn.close()


def get_agent_summary(start_at: Optional[str] = None, end_at: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_connection()
    rules_map = {name: dict(rules) for name, rules in DEFAULT_AGENT_RULES.items()}
    for row in conn.execute("SELECT agent_name, rules_json FROM agent_rules").fetchall():
        rules_map.setdefault(row["agent_name"], {}).update(json.loads(row["rules_json"]))

    conditions = []
    parameters: List[str] = []
    if start_at:
        conditions.append("created_at >= ?")
        parameters.append(start_at)
    if end_at:
        conditions.append("created_at <= ?")
        parameters.append(end_at)
    event_where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    counters = conn.execute(
        f"""
        SELECT agent_name,
               SUM(CASE WHEN decision = 'accepted' THEN 1 ELSE 0 END) AS accepted_count,
               SUM(CASE WHEN decision = 'rejected' THEN 1 ELSE 0 END) AS rejected_count
        FROM agent_events
        {event_where}
        GROUP BY agent_name
        """,
        parameters,
    ).fetchall()
    counts = {
        row["agent_name"]: {
            "accepted_count": row["accepted_count"],
            "rejected_count": row["rejected_count"],
        }
        for row in counters
    }

    latest_events = conn.execute(
        f"""
        SELECT agent_name, status, action, message, decision
        FROM agent_events
        WHERE id IN (
            SELECT MAX(id) FROM agent_events {event_where} GROUP BY agent_name
        )
        """,
        parameters,
    ).fetchall()
    latest_map = {row["agent_name"]: dict(row) for row in latest_events}
    conn.close()

    summaries: List[Dict[str, Any]] = []
    for agent_name, rules in rules_map.items():
        latest = latest_map.get(agent_name, {})
        summaries.append(
            {
                "agent_name": agent_name,
                "status": latest.get("status", rules.get("status", "idle")),
                "action": latest.get("action", "waiting"),
                "message": latest.get("message", "No recent activity."),
                "decision": latest.get("decision", "pending"),
                "accepted_count": counts.get(agent_name, {}).get("accepted_count", 0),
                "rejected_count": counts.get(agent_name, {}).get("rejected_count", 0),
                "rules": rules,
            }
        )
    return summaries


def add_user(username: str, password: str, is_admin: bool = False) -> bool:
    if not username or not password or len(password) < 12:
        return False

    conn = get_connection()
    try:
        existing = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            return False

        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
            (username, hash_password(password), int(is_admin)),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_user_profile(username: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    row = conn.execute(
        "SELECT username, created_at, avatar, is_admin FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users() -> List[Dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT username, created_at, avatar, is_admin FROM users ORDER BY username COLLATE NOCASE"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def update_user_avatar(username: str, avatar: str) -> None:
    if avatar not in PROFILE_AVATARS:
        raise ValueError("Choose one of the available profile avatars.")
    conn = get_connection()
    cursor = conn.execute("UPDATE users SET avatar = ? WHERE username = ?", (avatar, username))
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        raise ValueError("User account was not found.")


def change_user_password(username: str, current_password: str, new_password: str) -> None:
    if len(new_password) < 12:
        raise ValueError("New password must contain at least 12 characters.")
    conn = get_connection()
    row = conn.execute(
        "SELECT password_hash FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if not row or not verify_password(row["password_hash"], current_password):
        conn.close()
        raise ValueError("Current password is incorrect.")
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE username = ?",
        (hash_password(new_password), username),
    )
    conn.commit()
    conn.close()


def set_user_admin(username: str, is_admin: bool) -> None:
    conn = get_connection()
    row = conn.execute("SELECT is_admin FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        conn.close()
        raise ValueError("User account was not found.")
    if row["is_admin"] and not is_admin:
        admin_count = conn.execute("SELECT COUNT(*) AS count FROM users WHERE is_admin = 1").fetchone()["count"]
        if admin_count <= 1:
            conn.close()
            raise ValueError("The last admin cannot be demoted.")
    conn.execute("UPDATE users SET is_admin = ? WHERE username = ?", (int(is_admin), username))
    conn.commit()
    conn.close()


def delete_user(username: str, acting_username: str) -> None:
    if username == acting_username:
        raise ValueError("You cannot delete your own account while signed in.")
    conn = get_connection()
    row = conn.execute("SELECT is_admin FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        conn.close()
        raise ValueError("User account was not found.")
    if row["is_admin"]:
        admin_count = conn.execute("SELECT COUNT(*) AS count FROM users WHERE is_admin = 1").fetchone()["count"]
        if admin_count <= 1:
            conn.close()
            raise ValueError("The last admin cannot be deleted.")
    conn.execute("DELETE FROM auth_sessions WHERE username = ?", (username,))
    conn.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    conn.close()


def authenticate_user(username: str, password: str) -> bool:
    if not username or not password:
        return False

    conn = get_connection()
    user = conn.execute(
        "SELECT password_hash FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if not user or not verify_password(user["password_hash"], password):
        conn.close()
        return False
    if not user["password_hash"].startswith("scrypt$"):
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (hash_password(password), username),
        )
        conn.commit()
    conn.close()
    return True


def create_session_token(username: str) -> str:
    now = int(time.time())
    token_id = secrets.token_urlsafe(24)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": username, "jti": token_id, "iat": now, "exp": now + SESSION_TTL_SECONDS}
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=").decode()
    signing_input = f"{encode(header)}.{encode(payload)}"
    conn = get_connection()
    secret = conn.execute("SELECT value FROM app_settings WHERE key = 'auth_signing_key'").fetchone()["value"]
    conn.execute(
        "INSERT INTO auth_sessions (token_id, username, expires_at) VALUES (?, ?, ?)",
        (token_id, username, payload["exp"]),
    )
    conn.commit()
    conn.close()
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def validate_session_token(token: str) -> Optional[str]:
    try:
        header_part, payload_part, signature_part = token.split(".")
        signing_input = f"{header_part}.{payload_part}"
        decode = lambda value: json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        payload = decode(payload_part)
        conn = get_connection()
        secret_row = conn.execute("SELECT value FROM app_settings WHERE key = 'auth_signing_key'").fetchone()
        session = conn.execute(
            "SELECT username, expires_at, revoked FROM auth_sessions WHERE token_id = ?",
            (payload["jti"],),
        ).fetchone()
        conn.close()
        if not secret_row or not session or session["revoked"] or session["expires_at"] <= int(time.time()):
            return None
        expected = hmac.new(secret_row["value"].encode(), signing_input.encode(), hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(signature_part + "=" * (-len(signature_part) % 4))
        if not hmac.compare_digest(expected, supplied) or payload.get("sub") != session["username"]:
            return None
        return session["username"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def revoke_session_token(token: str) -> None:
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
    except (IndexError, ValueError, json.JSONDecodeError):
        return
    conn = get_connection()
    conn.execute("UPDATE auth_sessions SET revoked = 1 WHERE token_id = ?", (payload.get("jti"),))
    conn.commit()
    conn.close()


def record_trade_event(
    trade_id: int, stage: str, agent_name: str, status: str, decision: str,
    message: str, details: Optional[Dict[str, Any]] = None,
) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO trade_events (trade_id, stage, agent_name, status, decision, message, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (trade_id, stage, agent_name, status, decision, message, json.dumps(details or {}, sort_keys=True)),
    )
    conn.commit()
    conn.close()


def set_starting_capital(amount: float) -> None:
    if amount <= 0:
        raise ValueError("Starting capital must be greater than zero.")
    conn = get_connection()
    invested = conn.execute(
        "SELECT COALESCE(SUM(allocated_capital), 0) AS amount FROM trades WHERE status = 'OPEN'"
    ).fetchone()["amount"]
    if amount < float(invested):
        conn.close()
        raise ValueError("Starting capital cannot be lower than capital allocated to open positions.")
    conn.execute("UPDATE app_settings SET value = ? WHERE key = 'starting_capital'", (str(float(amount)),))
    conn.commit()
    conn.close()


def get_portfolio_summary(as_of: Optional[str] = None) -> Dict[str, float]:
    conn = get_connection()
    starting = float(conn.execute("SELECT value FROM app_settings WHERE key = 'starting_capital'").fetchone()["value"])
    tracked_active = "(status = 'OPEN' OR (status IN ('EXIT_PENDING', 'SUBMITTED_PENDING_RECONCILIATION', 'RECONCILIATION_REQUIRED') AND (broker_order_id IS NOT NULL OR broker_position_id IS NOT NULL)))"
    if as_of:
        open_clause = f"created_at <= ? AND ({tracked_active} OR (status = 'CLOSED' AND closed_at > ?))"
        position_params = (as_of, as_of)
        realized_clause = "status = 'CLOSED' AND closed_at <= ?"
        realized_params = (as_of,)
    else:
        open_clause = tracked_active
        position_params = ()
        realized_clause = "status = 'CLOSED'"
        realized_params = ()
    positions = conn.execute(
        f"SELECT COALESCE(SUM(allocated_capital), 0) AS invested, COALESCE(SUM(allocated_capital * leverage), 0) AS gross_exposure FROM trades WHERE {open_clause}",
        position_params,
    ).fetchone()
    realized = conn.execute(
        f"SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM trades WHERE {realized_clause}",
        realized_params,
    ).fetchone()["pnl"]
    conn.close()
    invested = float(positions["invested"] or 0)
    gross_exposure = float(positions["gross_exposure"] or 0)
    realized = float(realized or 0)
    return {
        "starting_capital": starting,
        "cash_available": starting - invested + realized,
        "invested_capital": invested,
        "gross_exposure": gross_exposure,
        "realized_pnl": realized,
        "account_equity": starting + realized,
    }


def get_open_positions() -> List[Dict[str, Any]]:
    """Return open positions and broker-linked submissions awaiting final reconciliation."""
    conn = get_connection()
    positions = conn.execute(
        "SELECT * FROM trades WHERE status = 'OPEN' OR (status IN ('EXIT_PENDING', 'SUBMITTED_PENDING_RECONCILIATION', 'RECONCILIATION_REQUIRED') AND (broker_order_id IS NOT NULL OR broker_position_id IS NOT NULL)) ORDER BY id DESC"
    ).fetchall()
    conn.close()
    result = []
    for row in positions:
        item = dict(row)
        item["company_name"] = item.get("company_name") or INSTRUMENT_NAMES.get(item["symbol"], item["symbol"])
        result.append(item)
    return result


def get_trade_history(start_at: Optional[str] = None, end_at: Optional[str] = None, *,
                      include_details: bool = True, trade_ids: Optional[List[int]] = None,
                      limit: Optional[int] = None, offset: int = 0) -> List[Dict[str, Any]]:
    """Batch lifecycle reads; optional SQL pagination precedes event hydration.

    Compact mode preserves decisions/search/chart metadata, without decoding or
    transferring research documents and API logs. Order gates never use this cache.
    """
    if trade_ids is not None and not trade_ids:
        return []
    conn = get_connection()
    try:
        conditions, parameters = [], []
        if start_at:
            conditions.append("t.created_at >= ?")
            parameters.append(start_at)
        if end_at:
            conditions.append("t.created_at <= ?")
            parameters.append(end_at)
        if trade_ids is not None:
            conditions.append("t.id IN (SELECT value FROM json_each(?))")
            parameters.append(json.dumps([int(tid) for tid in trade_ids]))
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        pagination = ""
        if limit is not None:
            if int(limit) < 1 or int(offset) < 0:
                raise ValueError("Page size must be positive and offset nonnegative.")
            pagination = " LIMIT ? OFFSET ?"
            parameters.extend([int(limit), int(offset)])
        trades = conn.execute(f"""SELECT t.*,
            (SELECT MAX(prior.id) FROM trades prior WHERE UPPER(prior.symbol)=UPPER(t.symbol) AND prior.id<t.id) AS duplicate_of_trade_id
            FROM trades t {where} ORDER BY t.id DESC{pagination}""", parameters).fetchall()
        if not trades:
            return []
        ids = json.dumps([t["id"] for t in trades])
        event_params = [ids]
        event_filter = ""
        if end_at:
            event_filter = " AND created_at <= ?"
            event_params.append(end_at)
        if include_details:
            event_columns = "*"
        else:
            # Only fields required to preserve legacy status inference. Heavy
            # evidence remains in SQLite until the user opens a lifecycle.
            event_columns = """id,trade_id,created_at,stage,agent_name,status,decision,message,
                json_object('reasons',json_extract(details,'$.reasons'),
                            'execution_reasons',json_extract(details,'$.execution_reasons')) AS details"""
        events = conn.execute(f"SELECT {event_columns} FROM trade_events WHERE trade_id IN (SELECT value FROM json_each(?)){event_filter} ORDER BY id", event_params).fetchall()
        by_trade = {}
        for event in events:
            by_trade.setdefault(event["trade_id"], []).append({**dict(event), "details": json.loads(event["details"] or "{}")})
        result = []
        for trade in trades:
            item = dict(trade)
            item["company_name"] = item.get("company_name") or INSTRUMENT_NAMES.get(item["symbol"], item["symbol"])
            item["is_duplicate"] = item["duplicate_of_trade_id"] is not None
            item["events"] = by_trade.get(item["id"], [])
            item["evidence_loaded"] = include_details
            if not include_details:
                item.pop("policy_json", None)
                item.pop("config_json", None)
            # Earlier news-cycle rows were all stored as ANALYSIS_ONLY unless the
            # Researcher rejected them. Derive the visible status from their saved
            # gate events without rewriting historical database records.
            if item.get("status") == "ANALYSIS_ONLY" and item.get("signal_provider") == "Yahoo Finance News + OpenAI research":
                stages = {event["stage"]: event for event in item["events"]}
                research_event = stages.get("research")
                strategy_event = stages.get("strategy")
                risk_event = stages.get("risk")
                execution_event = stages.get("execution")
                if research_event and strategy_event and risk_event and execution_event:
                    derived_status = news_candidate_lifecycle_status(
                        research_decision=research_event["decision"],
                        strategy_result={"decision": strategy_event["decision"]},
                        risk_result={**risk_event["details"], "decision": risk_event["decision"]},
                        execution_result={**execution_event["details"], "decision": execution_event["decision"]},
                    )
                    if derived_status != "ANALYSIS_ONLY":
                        item["stored_status"] = item["status"]
                        item["status"] = derived_status
                        item["status_inferred_from_events"] = True
            result.append(item)
        return result
    finally:
        conn.close()


VALIDATION_STAGE_FOR_STATUS = {
    "OPEN": "execution",
    "CLOSED": "execution",
    "REJECTED_RESEARCH": "research",
    "REJECTED_STRATEGY": "strategy",
    "REJECTED_RISK": "risk",
    "REJECTED_EXECUTION": "execution",
    "DEFERRED_CAPACITY": "execution",
}


async def validate_trade_outcome(trade_id: int, max_days: int = 14) -> Dict[str, Any]:
    conn = get_connection()
    trade = conn.execute(
        "SELECT id, symbol, side, status, entry_price, stop_loss_price, take_profit_price "
        "FROM trades WHERE id = ?",
        (trade_id,),
    ).fetchone()
    stage = VALIDATION_STAGE_FOR_STATUS.get(trade["status"]) if trade else None
    execution = conn.execute(
        "SELECT created_at FROM trade_events WHERE trade_id = ? AND stage = ? "
        "ORDER BY id DESC LIMIT 1",
        (trade_id, stage),
    ).fetchone() if stage else None
    conn.close()

    if not trade or not stage or not execution:
        return {
            "available": False,
            "reason": "No matching research or decision-stage timestamp is available for this opportunity.",
        }
    if any(trade[field] is None for field in ("entry_price", "stop_loss_price", "take_profit_price")):
        return {
            "available": False,
            "reason": "This opportunity has no complete entry, stop-loss, and take-profit reference prices.",
        }

    result = await validate_trade_price_path(
        ticker=trade["symbol"],
        side=trade["side"],
        entry_price=trade["entry_price"],
        stop_loss_price=trade["stop_loss_price"],
        take_profit_price=trade["take_profit_price"],
        placed_at=execution["created_at"],
        max_days=max_days,
    )
    result["validated_at"] = datetime.now(timezone.utc).isoformat()
    result["validation_type"] = "historical_trade_outcome"
    result["validation_scope"] = (
        "executed_paper_trade" if trade["status"] in {"OPEN", "CLOSED"}
        else "counterfactual_opportunity"
    )
    result["trade_status_at_validation"] = trade["status"]
    result["decision_stage"] = stage
    if result.get("available"):
        message = (
            f"Historical path checked over up to {result['window_days']} days using "
            f"{result['bars_analyzed']} five-minute bars. First outcome: {result['first_exit']}."
        )
        status = "completed"
    else:
        message = result.get("reason", "Historical price validation is unavailable.")
        status = "warning"
    record_trade_event(
        trade_id,
        "validation",
        "risk_manager",
        status,
        "pending",
        message,
        result,
    )
    return result


async def validate_pending_trade_outcomes(
    min_age_days: float = 1,
    max_age_days: float = 18,
    max_validation_days: int = 14,
    progress_callback: Optional[Callable[[int, int, int, Optional[bool]], None]] = None,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    low_age = float(min_age_days)
    high_age = float(max_age_days)
    if not 1.0 <= low_age <= high_age <= 18.0:
        raise ValueError("Opportunity age range must be within 1–18 days and in ascending order.")
    eligible_ids = []
    for trade in get_trade_history():
        if trade["status"] not in VALIDATION_STAGE_FOR_STATUS:
            continue
        if any(trade.get(field) is None for field in ("entry_price", "stop_loss_price", "take_profit_price")):
            continue
        if any(
            event.get("details", {}).get("validation_type") == "historical_trade_outcome"
            and event.get("details", {}).get("available")
            for event in trade["events"]
        ):
            continue
        decision_stage = VALIDATION_STAGE_FOR_STATUS[trade["status"]]
        decision_event = next(
            (event for event in reversed(trade["events"]) if event["stage"] == decision_stage),
            None,
        )
        if not decision_event:
            continue
        decision_time = datetime.fromisoformat(decision_event["created_at"].replace("Z", "+00:00"))
        if decision_time.tzinfo is None:
            decision_time = decision_time.replace(tzinfo=timezone.utc)
        age_days = (now - decision_time.astimezone(timezone.utc)).total_seconds() / 86400
        if low_age <= age_days <= high_age:
            eligible_ids.append(int(trade["id"]))

    results = []
    total = len(eligible_ids)
    for index, trade_id in enumerate(eligible_ids, start=1):
        if progress_callback:
            progress_callback(index, total, trade_id, None)
        result = await validate_trade_outcome(trade_id, max_days=max_validation_days)
        results.append({"trade_id": trade_id, **result})
        if progress_callback:
            progress_callback(index, total, trade_id, bool(result.get("available")))
    return {
        "selected_count": len(eligible_ids),
        "available_count": sum(bool(result.get("available")) for result in results),
        "unavailable_count": sum(not result.get("available") for result in results),
        "age_range_days": {"older_than": low_age, "younger_than": high_age},
        "validation_window_days": min(14, max(1, int(max_validation_days))),
        "results": results,
    }


def close_paper_trade(trade_id: int, exit_price: float) -> bool:
    conn = get_connection()
    trade = conn.execute("SELECT * FROM trades WHERE id = ? AND status IN ('OPEN','RECONCILIATION_REQUIRED')", (trade_id,)).fetchone()
    if not trade:
        conn.close()
        return False
    direction = 1 if trade["side"] == "LONG" else -1
    pnl = (float(exit_price) - float(trade["entry_price"] or exit_price)) * trade["quantity"] * direction
    conn.execute(
        "UPDATE trades SET status = 'CLOSED', current_price = ?, realized_pnl = ?, closed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (exit_price, pnl, trade_id),
    )
    conn.execute(
        "INSERT INTO trade_events (trade_id, stage, agent_name, status, decision, message, details) VALUES (?, 'exit', 'execution', 'completed', 'accepted', ?, ?)",
        (trade_id, f"Paper position closed at {exit_price:.4f}; realized P&L {pnl:.2f}.", json.dumps({"exit_price": exit_price, "realized_pnl": pnl})),
    )
    conn.commit()
    conn.close()
    return True


def get_test_workflow_runs(limit: int = 10) -> List[Dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM test_workflows ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 50)),)
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["details"] = json.loads(item.pop("details_json") or "{}")
        except json.JSONDecodeError:
            item["details"] = {"error": "Saved workflow details are invalid."}
        result.append(item)
    return result


def _update_test_workflow(
    workflow_id: int,
    status: str,
    details: Dict[str, Any],
    *,
    order_id: Optional[str] = None,
    position_id: Optional[str] = None,
) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE test_workflows SET updated_at = CURRENT_TIMESTAMP, status = ?, "
        "broker_order_id = COALESCE(?, broker_order_id), "
        "broker_position_id = COALESCE(?, broker_position_id), details_json = ? WHERE id = ?",
        (status, order_id, position_id, json.dumps(details, sort_keys=True), workflow_id),
    )
    conn.commit()
    conn.close()


def _load_test_workflow(workflow_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM test_workflows WHERE id = ?", (workflow_id,)).fetchone()
    conn.close()
    if not row:
        return None
    item = dict(row)
    item["details"] = json.loads(item.pop("details_json") or "{}")
    return item


def sync_linked_test_workflows() -> None:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM test_workflows WHERE status NOT IN ('FAILED','REJECTED_EXECUTION')").fetchall()
    for row in rows:
        details = json.loads(row["details_json"] or "{}")
        trade_id = details.get("trade_id")
        if not trade_id:
            continue
        trade = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not trade or trade["status"] in {"ANALYSIS_ONLY","DEFERRED_CAPACITY"}:
            continue
        details["broker_state"] = {key: trade[key] for key in ("status","broker_fill_price","broker_fill_units","broker_fees","reconciliation_status","realized_pnl")}
        conn.execute("UPDATE test_workflows SET status=?,broker_order_id=?,broker_position_id=?,details_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (trade["status"],trade["broker_order_id"],trade["broker_position_id"],json.dumps(details),row["id"]))
    conn.commit(); conn.close()


def get_sample_research_inputs() -> List[Dict[str, Any]]:
    """Return saved bullish research fixtures that can be replayed in demo tests."""
    candidates = []
    for item in load_research_feed():
        symbol = str(item.get("ticker") or "").strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol) or str(item.get("signal") or "").lower() != "bullish":
            continue
        timestamp = str(item.get("timestamp") or "").strip()
        notes = str(item.get("notes") or "").strip()
        if not timestamp or not notes:
            continue
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        candidates.append({
            "symbol": symbol,
            "timestamp": timestamp,
            "sector": str(item.get("sector") or "Unknown"),
            "signal": "bullish",
            "catalyst": str(item.get("catalyst") or "unspecified"),
            "confidence": float(item.get("confidence") or 0),
            "notes": notes,
            "source": "research_feed.json sample fixture",
        })
    return candidates


@serialized_account
async def run_demo_test_workflow(symbol: str, amount: float = 10.0) -> Dict[str, Any]:
    """Replay saved research through the same durable order path as automation."""
    symbol = str(symbol or "").strip().upper()
    amount = float(amount)
    if not math.isfinite(amount) or not 1 <= amount <= 25:
        raise ValueError("Test stake must be between $1 and $25.")
    settings = IntegrationSettings.from_environment()
    if settings.broker_provider != "etoro" or not settings.etoro_demo_trading:
        raise ValueError("Test purchases require the eToro demo provider.")
    replay = next((r for r in get_sample_research_inputs() if r["symbol"] == symbol), None)
    if not replay:
        raise ValueError(f"No valid saved bullish sample research input is available for {symbol}.")
    conn = get_connection()
    active = conn.execute("SELECT id FROM test_workflows WHERE symbol=? AND status IN ('RUNNING','SUBMITTING','RECONCILIATION_REQUIRED','SUBMITTED_PENDING_RECONCILIATION','OPEN','EXIT_PENDING')", (symbol,)).fetchone()
    if active:
        conn.close()
        raise ValueError("This symbol already has an unresolved or open test workflow.")
    details = {"workflow_type": "sample_replay_demo_test", "research_input": replay,
               "warning": "Saved fixture test, not a new production research prediction. Included in account equity; excluded from strategy attribution.",
               "steps": [{"name": "research_replay", "status": "passed", "input": replay}]}
    cursor = conn.execute("INSERT INTO test_workflows(symbol,amount,status,details_json) VALUES (?,?,'RUNNING',?)", (symbol,amount,json.dumps(details)))
    workflow_id = cursor.lastrowid
    conn.commit(); conn.close()
    candidate = {"ticker": symbol, "company_name": symbol, "signal": replay["signal"],
                 "company_news_sentiment": replay["signal"], "confidence": replay["confidence"],
                 "notes": replay["notes"], "fundamentals": {}, "ai_analysis": {},
                 "test_workflow_id": workflow_id, "test_max_spend_usd": amount,
                 "related_news": [{"title": replay["notes"], "published_at": replay["timestamp"], "source": replay["source"]}]}
    trade_id = save_trade(symbol,"LONG",2,1,"ANALYSIS_ONLY",notes="Saved fixture demo test; not a production signal.")
    details["trade_id"] = trade_id
    # A saved fixture is explicitly replayed research; the shared helper performs
    # the actual fresh Strategist/Risk/Execution reviews before sending anything.
    record_trade_event(trade_id,"research","researcher","completed","accepted","Saved research fixture replay.",candidate)
    record_trade_event(trade_id,"strategy","strategist","waiting","accepted","Fixture admitted for fresh strategy review.",{"candidate_context":candidate})
    _update_test_workflow(workflow_id,"RUNNING",details)
    try:
        # Account lease is already held by this workflow.
        result = await place_analysis_candidate_demo_order.__wrapped__(trade_id)
        details["steps"].append({"name":"execution","status":"submitted","result":result})
    except Exception as error:
        details["steps"].append({"name":"execution","status":"failed","error":str(error),"api_logs":getattr(error,"api_logs",[])})
    conn = get_connection()
    trade = dict(conn.execute("SELECT * FROM trades WHERE id=?",(trade_id,)).fetchone())
    events = [dict(r) for r in conn.execute("SELECT * FROM trade_events WHERE trade_id=? ORDER BY id",(trade_id,))]
    conn.close()
    details["lifecycle_events"] = events
    status = trade["status"] if trade["status"] not in {"ANALYSIS_ONLY","DEFERRED_CAPACITY"} else "FAILED"
    _update_test_workflow(workflow_id,status,details,order_id=trade["broker_order_id"],position_id=trade["broker_position_id"])
    return {"workflow_id":workflow_id,"trade_id":trade_id,"status":status,"symbol":symbol,"amount":amount,
            "order_id":trade["broker_order_id"],"position_id":trade["broker_position_id"],"details":details,
            "reason": details["steps"][-1].get("error")}


async def reconcile_demo_test_workflow(workflow_id: int) -> Dict[str, Any]:
    workflow = _load_test_workflow(workflow_id)
    if not workflow:
        raise ValueError("Test workflow run was not found.")
    settings = IntegrationSettings.from_environment()
    if settings.broker_provider != "etoro" or not settings.etoro_demo_trading:
        raise ValueError("Test workflow reconciliation requires the eToro demo provider.")
    adapter = EtoroBrokerAdapter(settings)
    positions = await asyncio.to_thread(adapter.get_positions)
    if not positions.get("available"):
        raise ValueError("eToro demo positions could not be verified.")
    details = workflow["details"]
    if details.get("trade_id"):
        reconcile_news_demo_positions(positions["positions"])
        sync_linked_test_workflows()
        refreshed = _load_test_workflow(workflow_id)
        return {"workflow_id": workflow_id, "status": refreshed["status"], "details": refreshed["details"]}
    instrument_id = str(details.get("instrument_id") or "")
    matches = [item for item in positions["positions"] if (
        workflow.get("broker_position_id") and str(item.get("position_id")) == str(workflow["broker_position_id"])
    ) or (instrument_id and str(item.get("instrument_id")) == instrument_id)]
    if len(matches) > 1:
        raise ValueError("Multiple matching eToro positions were found; refusing ambiguous test reconciliation.")
    position = matches[0] if matches else None
    if position:
        details["broker_position"] = position
        status = "OPEN"
        position_id = str(position.get("position_id") or workflow.get("broker_position_id") or "") or None
        details.setdefault("steps", []).append({"name": "reconciliation", "status": "open", "position_id": position_id, "fill_price": position.get("open_rate"), "units": position.get("units"), "fees": position.get("fees")})
        _update_test_workflow(workflow_id, status, details, position_id=position_id)
    elif workflow["status"] == "EXIT_PENDING":
        details.setdefault("steps", []).append({"name": "reconciliation", "status": "closed_confirmed", "message": "No longer present in the complete eToro demo positions snapshot; close P&L is not reported by this workflow."})
        _update_test_workflow(workflow_id, "CLOSED_PNL_UNAVAILABLE", details)
        status = "CLOSED_PNL_UNAVAILABLE"
    else:
        details.setdefault("steps", []).append({"name": "reconciliation", "status": "pending", "message": "Position is not yet visible in the demo portfolio."})
        _update_test_workflow(workflow_id, workflow["status"], details)
        status = workflow["status"]
    return {"workflow_id": workflow_id, "status": status, "details": details}


@serialized_account
async def close_demo_test_workflow(workflow_id: int) -> Dict[str, Any]:
    workflow = _load_test_workflow(workflow_id)
    if not workflow or workflow.get("status") != "OPEN" or not workflow.get("broker_position_id"):
        raise ValueError("Only a reconciled open test position can be closed.")
    settings = IntegrationSettings.from_environment()
    if settings.broker_provider != "etoro" or not settings.etoro_demo_trading:
        raise ValueError("Test position closes require the eToro demo provider.")
    if workflow["details"].get("trade_id"):
        result = await request_etoro_demo_close.__wrapped__(int(workflow["details"]["trade_id"]))
        sync_linked_test_workflows()
        return {"workflow_id": workflow_id, "status": "EXIT_PENDING", "close": result}
    adapter = EtoroBrokerAdapter(settings)
    broker_positions = await asyncio.to_thread(adapter.get_positions)
    if not broker_positions.get("available") or not any(
        str(item.get("position_id")) == str(workflow["broker_position_id"])
        for item in broker_positions.get("positions", [])
    ):
        raise ValueError("The test position is no longer present in the broker snapshot; refresh its state before closing.")
    position = await asyncio.to_thread(adapter.close_position, workflow["broker_position_id"])
    details = workflow["details"]
    details.setdefault("steps", []).append({"name": "test_close", "status": "submitted", "response": position})
    _update_test_workflow(workflow_id, "EXIT_PENDING", details)
    return {"workflow_id": workflow_id, "status": "EXIT_PENDING", "close": position}


@serialized_account
async def request_etoro_demo_close(trade_id: int) -> Dict[str, Any]:
    settings = IntegrationSettings.from_environment()
    if settings.broker_provider != "etoro" or not settings.etoro_demo_trading:
        raise ValueError("Only an eToro demo position can be closed through this action.")
    conn = get_connection()
    trade = conn.execute("SELECT * FROM trades WHERE id = ? AND status IN ('OPEN','RECONCILIATION_REQUIRED')", (trade_id,)).fetchone()
    conn.close()
    if not trade or not trade["broker_position_id"]:
        raise ValueError("No known eToro demo position ID is stored for this trade.")
    adapter = EtoroBrokerAdapter(settings)
    snapshot = await asyncio.to_thread(adapter.get_positions)
    if not snapshot.get("available") or not any(str(p.get("position_id")) == str(trade["broker_position_id"]) for p in snapshot.get("positions", [])):
        raise ValueError("The broker did not verify that this position is currently open.")
    result = await asyncio.to_thread(adapter.close_position, str(trade["broker_position_id"]))
    conn = get_connection()
    conn.execute(
        "UPDATE trades SET status = 'EXIT_PENDING', reconciliation_status = 'close_submitted' WHERE id = ?",
        (trade_id,),
    )
    conn.commit()
    conn.close()
    record_trade_event(
        trade_id, "exit", "execution", "submitted", "pending",
        "Close request submitted to eToro demo; awaiting portfolio reconciliation.",
        {"broker_close": result},
    )
    return result


@serialized_account
async def update_etoro_demo_protection(
    trade_id: int, stop_loss_price: float, take_profit_price: float,
) -> Dict[str, Any]:
    """Submit one user-requested SL/TP update and persist it for reconciliation."""
    settings = IntegrationSettings.from_environment()
    if settings.broker_provider != "etoro" or not settings.etoro_demo_trading:
        raise ValueError("Protection updates are available only for eToro demo positions.")
    stop = float(stop_loss_price)
    target = float(take_profit_price)
    if not math.isfinite(stop) or not math.isfinite(target) or stop <= 0 or target <= 0:
        raise ValueError("Stop loss and take profit must be positive finite prices.")
    conn = get_connection()
    trade = conn.execute(
        "SELECT * FROM trades WHERE id = ? AND status IN ('OPEN','RECONCILIATION_REQUIRED')",
        (int(trade_id),),
    ).fetchone()
    conn.close()
    if not trade or not trade["broker_position_id"]:
        raise ValueError("No verified open eToro demo position is linked to this trade.")

    adapter = EtoroBrokerAdapter(settings)
    snapshot = await asyncio.to_thread(adapter.get_positions)
    matches = [
        position for position in snapshot.get("positions", [])
        if str(position.get("position_id")) == str(trade["broker_position_id"])
    ]
    if len(matches) != 1:
        raise ValueError("The broker did not return exactly one matching open position.")
    position = matches[0]
    instrument_id = position.get("instrument_id")
    mark = float(position.get("current_rate") or 0)
    if mark <= 0 and instrument_id:
        rate = await asyncio.to_thread(adapter._get_rate, int(instrument_id))
        mark = float(rate.get("bid" if str(trade["side"]).upper() == "LONG" else "ask") or 0)
    if mark <= 0:
        mark = float(position.get("open_rate") or 0)
    if mark <= 0:
        raise ValueError("The broker did not provide a usable position price.")
    side = str(trade["side"] or position.get("side") or "").upper()
    if side == "LONG" and not (stop < mark < target):
        raise ValueError("For a LONG position, stop loss must be below and take profit above the current broker price.")
    if side == "SHORT" and not (target < mark < stop):
        raise ValueError("For a SHORT position, take profit must be below and stop loss above the current broker price.")
    if side not in {"LONG", "SHORT"}:
        raise ValueError("The stored position direction is unavailable.")

    result = await asyncio.to_thread(
        adapter.modify_position, str(trade["broker_position_id"]), stop, target,
    )
    policy = json.loads(trade["policy_json"] or "{}")
    signature = json.dumps(
        {"stop_loss_price": round(stop, 8), "take_profit_price": round(target, 8)},
        sort_keys=True,
    )
    policy["last_protection_request"] = time.time()
    policy["last_protection_request_signature"] = signature
    policy["protection_updated_manually"] = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    conn.execute(
        "UPDATE trades SET stop_loss_price=?, take_profit_price=?, status='RECONCILIATION_REQUIRED', "
        "reconciliation_status='protection_update_pending', stop_state='broker_verification_pending', policy_json=? WHERE id=?",
        (stop, target, json.dumps(policy, sort_keys=True), int(trade_id)),
    )
    conn.commit()
    conn.close()
    record_trade_event(
        int(trade_id), "protection", "execution", "submitted", "pending",
        f"User submitted stop loss {stop:.4f} and take profit {target:.4f} to eToro demo; broker verification is pending.",
        {"stop_loss_price": stop, "take_profit_price": target, "broker_response": result},
    )
    return {"status": "submitted", "stop_loss_price": stop, "take_profit_price": target, "response": result}


def save_signal(sector: str, signal: str, catalyst: str, confidence: float, payload: Dict[str, Any]) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO signals (sector, signal, catalyst, confidence, payload) VALUES (?, ?, ?, ?, ?)",
        (sector, signal, catalyst, confidence, json.dumps(payload, sort_keys=True)),
    )
    conn.commit()
    conn.close()


def save_news_articles(articles: List[Dict[str, Any]]) -> Dict[str, int]:
    conn = get_connection()
    inserted = 0
    updated = 0
    for article in articles:
        fetched_at = datetime.fromisoformat(
            article["fetched_at"].replace("Z", "+00:00")
        ).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        existing = conn.execute(
            "SELECT 1 FROM news_articles WHERE article_key = ?",
            (article["article_key"],),
        ).fetchone()
        cursor = conn.execute(
            """
            INSERT INTO news_articles
                (article_key, ticker, title, summary, source, url, published_at, fetched_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(article_key) DO UPDATE SET
                ticker = excluded.ticker,
                title = excluded.title,
                summary = excluded.summary,
                source = excluded.source,
                url = excluded.url,
                published_at = excluded.published_at,
                fetched_at = excluded.fetched_at,
                payload = excluded.payload,
                relevance_status = CASE
                    WHEN news_articles.title != excluded.title OR news_articles.summary != excluded.summary
                    THEN 'unreviewed' ELSE news_articles.relevance_status END,
                relevance_tickers = CASE
                    WHEN news_articles.title != excluded.title OR news_articles.summary != excluded.summary
                    THEN '' ELSE news_articles.relevance_tickers END,
                relevance_reason = CASE
                    WHEN news_articles.title != excluded.title OR news_articles.summary != excluded.summary
                    THEN '' ELSE news_articles.relevance_reason END,
                relevance_analyzed_at = CASE
                    WHEN news_articles.title != excluded.title OR news_articles.summary != excluded.summary
                    THEN NULL ELSE news_articles.relevance_analyzed_at END
            """,
            (
                article["article_key"], article["ticker"], article["title"],
                article.get("summary", ""), article.get("source", "Yahoo Finance"),
                article.get("url", ""), article.get("published_at"),
                fetched_at, json.dumps(article, sort_keys=True),
            ),
        )
        if existing:
            updated += cursor.rowcount
        else:
            inserted += cursor.rowcount
        for ticker in set([article["ticker"], *(article.get("related_tickers") or [])]):
            conn.execute("INSERT INTO article_companies(article_key,ticker,evidence) VALUES (?,?,?) ON CONFLICT(article_key,ticker) DO UPDATE SET evidence=excluded.evidence", (article["article_key"], str(ticker).upper(), article.get("association_evidence") or "Yahoo ticker news association"))
    conn.commit()
    conn.close()
    return {"inserted": inserted, "updated": updated}


def get_news_articles(
    start_at: Optional[str] = None,
    end_at: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    conditions = []
    parameters: List[Any] = []
    if start_at:
        conditions.append("fetched_at >= ?")
        parameters.append(start_at)
    if end_at:
        conditions.append("fetched_at <= ?")
        parameters.append(end_at)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    parameters.append(limit)
    conn = get_connection()
    rows = conn.execute(
        f"SELECT article_key, ticker, title, summary, source, url, published_at, fetched_at, relevance_status, relevance_tickers, relevance_reason, relevance_analyzed_at FROM news_articles {where_clause} ORDER BY fetched_at DESC LIMIT ?",
        parameters,
    ).fetchall()
    conn.close()
    articles = [dict(row) for row in rows]
    for article in articles:
        article["relevance_tickers"] = [ticker for ticker in article["relevance_tickers"].split(",") if ticker]
    return articles


def classify_research_headlines(
    headlines: List[Dict[str, Any]], candidates: List[Dict[str, Any]],
) -> Dict[str, int]:
    sector_terms = {
        "technology": ("artificial intelligence", "ai", "semiconductor", "chip", "memory", "cloud", "software", "data center", "datacenter", "compute", "earnings", "guidance"),
        "energy": ("crude", "oil", "opec", "inventory", "inventories", "natural gas", "energy", "refinery", "production"),
    }
    totals = {"relevant": 0, "not_relevant": 0}
    for headline in headlines:
        text = f"{headline.get('title', '')} {headline.get('summary', '')}".lower()
        matched_tickers = []
        reasons = []
        for candidate in candidates:
            ticker = str(candidate.get("ticker", "")).upper()
            company = str(candidate.get("company_name", "")).lower()
            if ticker and headline.get("ticker", "").upper() == ticker:
                matched_tickers.append(ticker)
                reasons.append(f"Ticker matches {ticker}")
                continue
            if company and company in text:
                matched_tickers.append(ticker)
                reasons.append(f"Company name matches {company}")
                continue
            terms = sector_terms.get(str(candidate.get("sector", "")).lower(), ())
            matching_terms = [
                term for term in terms
                if (" " in term and term in text) or (" " not in term and re.search(rf"\b{re.escape(term)}\b", text))
            ]
            if matching_terms:
                matched_tickers.append(ticker)
                reasons.append(f"{candidate.get('sector')} topic match: {', '.join(matching_terms[:3])}")

        matched_tickers = list(dict.fromkeys(filter(None, matched_tickers)))
        status = "relevant" if matched_tickers else "not relevant"
        reason = "; ".join(dict.fromkeys(reasons)) if reasons else "No matching candidate ticker, company, or sector research terms."
        update_news_relevance(headline["article_key"], status, matched_tickers, reason)
        totals["relevant" if matched_tickers else "not_relevant"] += 1
    return totals


def update_news_relevance(article_key: str, status: str, tickers: List[str], reason: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE news_articles SET relevance_status = ?, relevance_tickers = ?, relevance_reason = ?, relevance_analyzed_at = CURRENT_TIMESTAMP WHERE article_key = ?",
        (status, ",".join(tickers), reason, article_key),
    )
    conn.commit()
    conn.close()


async def run_hourly_news_fetch() -> Dict[str, int]:
    settings = IntegrationSettings.from_environment()
    if settings.news_provider not in {"yfinance", "yahoo", "yahoo_finance"}:
        logger.info("News provider is '%s'; skipping remote news fetch.", settings.news_provider)
        return {"fetched": 0, "inserted": 0, "updated": 0}

    try:
        articles = await YahooFinanceNewsProvider(settings).fetch_news()
        counts = save_news_articles(articles)
        
        # The eToro workflow discovers research candidates from article tickers;
        # do not classify its headlines against the retired fixed research feed.
        if IntegrationSettings.from_environment().broker_provider != "etoro":
            researcher = ResearcherAgent()
            market_data = await researcher.run_data_fetch(max_candidates=100)
            candidates = market_data.get("candidates", [])
            if candidates:
                recent_news = get_news_articles(limit=200)
                classification_results = classify_research_headlines(recent_news, candidates)
                logger.info(
                    "Headline classification after fetch: %d relevant, %d not relevant",
                    classification_results.get("relevant", 0),
                    classification_results.get("not_relevant", 0),
                )
        
        record_agent_event(
            "researcher", "completed", "hourly_news_fetch",
            f"Fetched {len(articles)} Yahoo Finance headlines; stored {counts['inserted']} new and refreshed {counts['updated']} existing articles.",
            "accepted" if articles else "rejected",
            {"fetched": len(articles), **counts},
        )
        logger.info("Hourly Yahoo news fetch completed: %d fetched, %d new, %d refreshed", len(articles), counts["inserted"], counts["updated"])
        return {"fetched": len(articles), **counts}
    except Exception as error:
        logger.exception("Hourly news fetch failed; the dummy research feed remains available")
        record_agent_event(
            "researcher", "warning", "hourly_news_fetch", str(error), "rejected", {}
        )
        return {"fetched": 0, "inserted": 0, "updated": 0}


def save_trade(
    symbol: str,
    side: str,
    leverage: float,
    exit_target: float,
    status: str,
    notes: Optional[str] = None,
    allocated_capital: float = 0,
    entry_price: Optional[float] = None,
    current_price: Optional[float] = None,
    quantity: float = 0,
    company_name: str = "",
    stop_loss_price: Optional[float] = None,
    take_profit_price: Optional[float] = None,
    signal_timestamp: Optional[str] = None,
    signal_provider: Optional[str] = None,
    quote_provider: Optional[str] = None,
    quote_timestamp: Optional[str] = None,
) -> int:
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO trades (symbol, side, leverage, exit_target, status, notes, allocated_capital, entry_price, current_price, quantity, company_name, stop_loss_price, take_profit_price, signal_timestamp, signal_provider, quote_provider, quote_timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, side, leverage, exit_target, status, notes, allocated_capital, entry_price, current_price, quantity, company_name, stop_loss_price, take_profit_price, signal_timestamp, signal_provider, quote_provider, quote_timestamp),
    )
    conn.commit()
    trade_id = int(cursor.lastrowid)
    conn.close()
    return trade_id


def save_risk_event(pnl: float, volatility: float, action: str, details: str) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO risk_events (pnl, volatility, action, details) VALUES (?, ?, ?, ?)",
        (pnl, volatility, action, details),
    )
    conn.commit()
    conn.close()


def get_dashboard_summary(start_at: Optional[str] = None, end_at: Optional[str] = None, *, include_details: bool = True) -> Dict[str, Any]:
    conn = get_connection()
    conditions = []
    parameters: List[str] = []
    if start_at:
        conditions.append("created_at >= ?")
        parameters.append(start_at)
    if end_at:
        conditions.append("created_at <= ?")
        parameters.append(end_at)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    signal_columns = "sector,signal,catalyst,confidence,created_at" + (",payload" if include_details else "")
    latest_signal = conn.execute(
        f"SELECT {signal_columns} FROM signals {where_clause} ORDER BY id DESC LIMIT 1",
        parameters,
    ).fetchone()
    latest_trade = conn.execute(
        f"SELECT symbol, side, leverage, exit_target, status, notes, created_at FROM trades {where_clause} ORDER BY id DESC LIMIT 1",
        parameters,
    ).fetchone()
    risk_events = conn.execute(
        f"SELECT pnl, volatility, action, details, created_at FROM risk_events {where_clause} ORDER BY id DESC LIMIT 5",
        parameters,
    ).fetchall()
    conn.close()
    agent_summary = get_agent_summary(start_at, end_at)
    portfolio = get_portfolio_summary(end_at)
    performance = get_broker_performance()
    if IntegrationSettings.from_environment().broker_provider == "etoro" and performance.get("broker_equity") is not None:
        portfolio.update(starting_capital=performance["first_observed_equity"],
                         account_equity=performance["broker_equity"], cash_available=performance["cash_available"],
                         invested_capital=performance["invested_capital"], gross_exposure=performance["gross_exposure"],
                         source=performance["source"], as_of=performance["as_of"])

    return {
        "latest_signal": dict(latest_signal) if latest_signal else None,
        "latest_trade": dict(latest_trade) if latest_trade else None,
        "risk_events": [dict(row) for row in risk_events],
        "agents": agent_summary,
        "decision_timeline": get_decision_timeline(start_at, end_at) if include_details else [],
        "portfolio": portfolio,
        "broker_performance": performance,
        "open_positions": get_open_positions(),
        "trades": get_trade_history(start_at, end_at, include_details=include_details),
        "news": get_news_articles(start_at, end_at),
    }


class ResearcherAgent:
    async def analyze_news_candidate(
        self,
        *,
        ticker: str,
        company_name: str,
        sector: Optional[str],
        headlines: List[Dict[str, Any]],
        fundamentals: Dict[str, Any],
        market_context: Dict[str, Any],
        research_adapter: Optional[OpenAIResearchAdapter],
    ) -> Dict[str, Any]:
        """Analyze a live news-discovered company using the available evidence."""
        if not get_agent_rules().get("researcher", {}).get("enabled", True):
            return {"status":"unavailable", "signal":"neutral", "confidence":0.0, "reason":"Researcher Agent is disabled."}
        if research_adapter is None:
            return {
                "status": "unavailable",
                "reason": "OpenAI research is disabled or OPENAI_API_KEY is not configured.",
                "signal": "neutral",
                "confidence": 0.0,
            }
        try:
            result = await research_adapter.research({
                "ticker": ticker,
                "company_name": company_name,
                "sector": sector,
                "background": "News-discovered candidate. Base the assessment on the supplied dated evidence; identify uncertainty and do not invent facts.",
                "headlines": headlines,
                "fundamentals": fundamentals,
                "market_context": market_context,
            })
            return {**result, "status": "available"}
        except Exception as error:
            logger.warning("Researcher OpenAI analysis failed for %s: %s", ticker, type(error).__name__)
            return {
                "status": "unavailable",
                "reason": f"Research request failed ({type(error).__name__}).",
                "signal": "neutral",
                "confidence": 0.0,
            }

    def _next_payload_for_sector(self, sector: str) -> Dict[str, Any]:
        feed = load_research_feed()
        candidates = [item for item in feed if item.get("sector") == sector]
        if not candidates:
            candidates = feed
        conn = get_connection()
        fetch_count = conn.execute("SELECT COUNT(*) AS count FROM signals WHERE sector = ?", (sector,)).fetchone()["count"]
        conn.close()
        selected = candidates[int(fetch_count) % len(candidates)]
        ticker = selected.get("ticker", "UNKNOWN")
        return {
            "sector": selected.get("sector", sector),
            "signal": selected.get("signal", "neutral"),
            "catalyst": selected.get("catalyst", "macro"),
            "confidence": float(selected.get("confidence", 0.5)),
            "ticker": ticker,
            "company_name": selected.get("company_name", INSTRUMENT_NAMES.get(ticker, ticker)),
            "price": float(selected["price"]) if selected.get("price") is not None else None,
            "notes": selected.get("notes", "No notes supplied."),
            "timestamp": selected.get("timestamp", "2026-10-03T00:00:00Z"),
        }

    async def poll_technology(self) -> Dict[str, Any]:
        rules = get_agent_rules().get("researcher", DEFAULT_AGENT_RULES["researcher"])
        logger.info("Polling technology market signals...")
        await asyncio.sleep(0.2)
        payload = self._next_payload_for_sector("Technology")
        allowed = bool(rules.get("enabled", True)) and "Technology" in rules.get("allowed_sectors", ["Technology", "Energy"])
        decision = "accepted" if allowed and payload["signal"] != "neutral" and payload["confidence"] >= float(rules.get("min_confidence", 0.6)) else "rejected"
        save_signal(
            sector="Technology",
            signal=payload["signal"],
            catalyst=payload["catalyst"],
            confidence=payload["confidence"],
            payload=payload,
        )
        record_agent_event(
            "researcher",
            "completed",
            "fetch_market_data",
            f"Technology data fetched with confidence {payload['confidence']}",
            decision,
            payload,
        )
        return payload

    async def poll_energy(self) -> Dict[str, Any]:
        rules = get_agent_rules().get("researcher", DEFAULT_AGENT_RULES["researcher"])
        logger.info("Polling energy macro shifts...")
        await asyncio.sleep(0.2)
        payload = self._next_payload_for_sector("Energy")
        allowed = bool(rules.get("enabled", True)) and "Energy" in rules.get("allowed_sectors", ["Technology", "Energy"])
        decision = "accepted" if allowed and payload["signal"] != "neutral" and payload["confidence"] >= float(rules.get("min_confidence", 0.6)) else "rejected"
        save_signal(
            sector="Energy",
            signal=payload["signal"],
            catalyst=payload["catalyst"],
            confidence=payload["confidence"],
            payload=payload,
        )
        record_agent_event(
            "researcher",
            "completed",
            "fetch_market_data",
            f"Energy data fetched with confidence {payload['confidence']}",
            decision,
            payload,
        )
        return payload

    async def run_data_fetch(self, max_candidates: int) -> Dict[str, Any]:
        rules = get_agent_rules().get("researcher", DEFAULT_AGENT_RULES["researcher"])
        allowed_sectors = rules.get("allowed_sectors", ["Technology", "Energy"])
        feed = load_research_feed()
        candidates_by_ticker: Dict[str, Dict[str, Any]] = {}
        for item in feed:
            sector = item.get("sector", "Unknown")
            ticker = str(item.get("ticker", "UNKNOWN")).upper()
            if sector not in allowed_sectors or ticker == "UNKNOWN":
                continue
            candidates_by_ticker[ticker] = {
                "sector": sector,
                "signal": str(item.get("signal", "neutral")).lower(),
                "catalyst": item.get("catalyst", "macro"),
                "confidence": float(item.get("confidence", 0.5)),
                "ticker": ticker,
                "company_name": item.get("company_name", INSTRUMENT_NAMES.get(ticker, ticker)),
                "price": float(item["price"]) if item.get("price") is not None else None,
                "notes": item.get("notes", "No notes supplied."),
                "timestamp": item.get("timestamp", ""),
            }
        candidate_pool = sorted(
            candidates_by_ticker.values(),
            key=lambda item: item["timestamp"],
        )
        if not candidate_pool:
            record_agent_event(
                "researcher", "warning", "candidate_pool",
                "No candidates matched the enabled researcher sectors.", "rejected", {},
            )
            return {"candidates": [], "candidate_pool_size": 0}

        conn = get_connection()
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'research_candidate_offset'"
        ).fetchone()
        offset = int(row["value"]) % len(candidate_pool) if row else 0
        rotated_pool = candidate_pool[offset:] + candidate_pool[:offset]
        selected = rotated_pool[:max(0, int(max_candidates))]
        next_offset = (offset + len(selected)) % len(candidate_pool)
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('research_candidate_offset', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(next_offset),),
        )
        conn.commit()
        conn.close()

        record_agent_event(
            "researcher", "completed", "candidate_pool",
            f"Selected {len(selected)} of {len(candidate_pool)} unique candidates for this cycle.",
            "accepted" if selected else "rejected",
            {
                "candidate_pool_size": len(candidate_pool),
                "selected_count": len(selected),
                "max_fetch_per_cycle": max_candidates,
                "rotation_offset": offset,
                "tickers": [candidate["ticker"] for candidate in selected],
            },
        )
        return {"candidates": selected, "candidate_pool_size": len(candidate_pool)}


class StrategistAgent:
    def analyze_and_decide(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        rules = get_agent_rules().get("strategist", DEFAULT_AGENT_RULES["strategist"])
        logger.info("Analyzing market data and forming strategy...")
        tech_data = market_data.get("tech_data", {})
        energy_data = market_data.get("energy_data", {})

        signal = tech_data.get("signal")
        position = "LONG" if signal == "bullish" else "SHORT" if signal == "bearish" else "HOLD"
        daily_trend = tech_data.get("daily_trend_context") or {}
        trend_state = str(daily_trend.get("trend_state") or "unknown").lower()
        sentiment_value = tech_data.get("company_news_sentiment", signal)
        if isinstance(sentiment_value, str):
            news_sentiments = {sentiment_value.lower()}
        else:
            news_sentiments = {str(value).lower() for value in (sentiment_value or [])}
        if (tech_data.get("ai_analysis") or {}).get("reaction_assessment") == "temporary" and trend_state == "bullish" and "bearish" in news_sentiments:
            position = "LONG"
        elif (tech_data.get("ai_analysis") or {}).get("reaction_assessment") == "temporary" and trend_state == "bearish" and "bullish" in news_sentiments:
            position = "SHORT"
        default_leverage_value = float(rules.get("default_leverage", 2))
        max_leverage_value = float(rules.get("max_leverage", 3))
        if (
            not math.isfinite(default_leverage_value)
            or not math.isfinite(max_leverage_value)
            or not default_leverage_value.is_integer()
            or not max_leverage_value.is_integer()
        ):
            raise ValueError("Strategist default_leverage and max_leverage must be whole numbers.")
        default_leverage = int(default_leverage_value)
        max_leverage = int(max_leverage_value)
        leverage = min(default_leverage, max_leverage)
        exit_target = (
            1 + BASE_TAKE_PROFIT_PCT
            if position == "LONG"
            else 1 - BASE_TAKE_PROFIT_PCT
            if position == "SHORT"
            else 1.0
        )
        recent_news = market_data.get("recent_news", [])
        decision_gate = evaluate_strategist_gate(tech_data, position, recent_news, rules)
        decision = "accepted" if decision_gate["accepted"] and leverage > 0 else "rejected"
        if leverage <= 0:
            decision_gate["rejection_reasons"].append("Proposed leverage is not positive.")
            decision_gate["accepted"] = False

        payload = {
            "position": position,
            "leverage": leverage,
            "exit_target": exit_target,
            "assets": [tech_data.get("ticker", "AAPL"), energy_data.get("ticker", "USOIL")],
            "research_input": {
                "signal": tech_data.get("signal"),
                "confidence": tech_data.get("confidence"),
                "catalyst": tech_data.get("catalyst"),
                "notes": tech_data.get("notes"),
            },
            "news_context": recent_news,
            "ai_analysis": tech_data.get("ai_analysis"),
            "fundamentals": tech_data.get("fundamentals", {}),
            "daily_trend_context": tech_data.get("daily_trend_context", {}),
            "market_quote": tech_data.get("market_quote", {}),
            "market_session_context": tech_data.get("market_session_context", {}),
            "decision_gate": decision_gate,
            "notes": decision_gate.get("notes") or (
                f"Received {len(recent_news)} headlines; decision applied confidence, company evidence, and available trend context."
            ),
        }
        record_agent_event(
            "strategist",
            "completed",
            "build_strategy",
            (
                f"Strategist approved {position} at {float(tech_data.get('confidence', 0.0)):.0%} confidence."
                if decision == "accepted"
                else f"Strategist rejected {position}: {' '.join(decision_gate['rejection_reasons'])}"
            ),
            decision,
            payload,
        )
        return payload


class RiskManagerAgent:
    def review_news_demo_candidate(
        self,
        *,
        candidate: Dict[str, Any],
        strategy: Dict[str, Any],
        account: Dict[str, Any],
        broker_snapshot: Dict[str, Any],
        instrument: Dict[str, Any],
        atr14: Optional[float],
    ) -> Dict[str, Any]:
        """Apply portfolio and stop-distance checks without sending an order."""
        all_rules = get_agent_rules()
        rules = all_rules.get("risk_manager", DEFAULT_AGENT_RULES["risk_manager"])
        execution_rules = all_rules.get("execution", DEFAULT_AGENT_RULES["execution"])
        positions = broker_snapshot.get("positions") or []
        equity = account.get("equity")
        reasons: List[str] = []
        execution_reasons: List[str] = []
        if not bool(rules.get("enabled", True)):
            execution_reasons.append("Risk Manager is disabled.")
        if not account.get("available") or equity is None or float(equity) <= 0:
            execution_reasons.append("Verified positive account equity is required before an order can be sized.")
        if not broker_snapshot.get("available") or not isinstance(positions, list):
            execution_reasons.append("A complete eToro positions snapshot is required before order execution.")
        if account.get("pending_order_count", 0) or account.get("unsupported_mirror_positions", 0):
            execution_reasons.append("Broker pending orders or mirrored positions have unverified aggregate stop risk.")
        if not instrument.get("tradeable") or not instrument.get("supported_asset"):
            execution_reasons.append("eToro has not confirmed a tradeable supported instrument.")
        if strategy.get("position") == "SHORT" and instrument.get("short_enabled") is False:
            execution_reasons.append("eToro has disabled short orders for this instrument.")
        if strategy.get("position") == "LONG" and instrument.get("buy_enabled") is False:
            execution_reasons.append("eToro has disabled buy orders for this instrument.")
        if strategy.get("position") not in execution_rules.get("allowed_side", ["LONG", "SHORT"]):
            execution_reasons.append("The proposed direction is disabled in Execution settings.")

        daily_trend = candidate.get("daily_trend_context") or {}
        market_session = candidate.get("market_session_context") or {}
        trend_state = str(daily_trend.get("trend_state") or "unknown").lower()
        if not daily_trend.get("available") or trend_state not in {"bullish", "bearish", "mixed"}:
            trend_alignment = "unavailable"
            risk_budget_multiplier = 0.5
        elif strategy.get("position") == "LONG" and trend_state == "bearish":
            trend_alignment = "opposes_news_direction"
            required_confidence = float(get_agent_rules().get("strategist", DEFAULT_AGENT_RULES["strategist"]).get("countertrend_long_min_confidence", 0.85))
            risk_budget_multiplier = 0.5
            if float(candidate.get("confidence") or 0.0) < required_confidence:
                reasons.append(f"Risk Manager rejects the bullish countertrend setup below {required_confidence:.0%} research confidence.")
        elif strategy.get("position") == "SHORT" and trend_state == "bullish":
            trend_alignment = "opposes_news_direction"
            required_confidence = float(get_agent_rules().get("strategist", DEFAULT_AGENT_RULES["strategist"]).get("countertrend_short_min_confidence", 0.85))
            risk_budget_multiplier = 0.5
            if float(candidate.get("confidence") or 0.0) < required_confidence:
                reasons.append(f"Risk Manager rejects the bearish countertrend setup below {required_confidence:.0%} research confidence.")
        elif trend_state == "mixed":
            trend_alignment = "mixed_or_not_directional"
            risk_budget_multiplier = 0.5
        else:
            trend_alignment = "confirms_news_direction"
            risk_budget_multiplier = 1.0

        ticker = str(candidate.get("ticker") or "").upper()
        instrument_id = str(instrument.get("instrument_id") or "")
        matching_position = next((position for position in positions if (
            ticker == str(position.get("symbol") or "").upper()
            or (instrument_id and instrument_id == str(position.get("instrument_id") or ""))
        )), None)
        if matching_position:
            execution_reasons.append("A position in this instrument is already open.")
        max_positions = max(1, int(rules.get("max_open_positions", 30)))
        if len(positions) >= max_positions:
            execution_reasons.append(f"Open position limit reached ({len(positions)}/{max_positions}).")

        quote = candidate.get("market_quote") or {}
        side = strategy.get("position")
        entry = quote.get("ask" if side == "LONG" else "bid")
        if not quote.get("available") or quote.get("provider") != "eToro" or not entry:
            execution_reasons.append("A fresh verified eToro quote is required before order execution.")
        valid_atr = False
        try:
            valid_atr = atr14 is not None and math.isfinite(float(atr14)) and float(atr14) > 0
        except (TypeError, ValueError):
            pass
        if not valid_atr:
            execution_reasons.append("A valid positive 14-day ATR is required before order execution.")
        sized = {}
        approved_leverage = strategy.get("leverage", min(
            int(all_rules.get("strategist", {}).get("default_leverage", 2)),
            int(all_rules.get("strategist", {}).get("max_leverage", 3))))
        sizing_inputs_available = bool(
            entry and valid_atr and account.get("available") and equity is not None and float(equity) > 0
        )
        try:
            if not float(approved_leverage).is_integer():
                raise ValueError("Leverage must be a whole number.")
            if sizing_inputs_available:
                sized = size_order(
                    side=side, entry=entry, atr=atr14, equity=equity,
                    cash=account.get("cash_available") or 0, leverage=int(approved_leverage),
                    positions=positions, risk_rules=rules, sector=(candidate.get("fundamentals") or {}).get("sector"),
                    execution_rules={**execution_rules, "observed_spread": max(0, float(quote.get("ask") or 0)-float(quote.get("bid") or 0))},
                    multiplier=risk_budget_multiplier,
                    setup=strategy.get("decision_gate", {}).get("conditional_mean_reversion"),
                )
                execution_reasons.extend(sized.pop("execution_reasons"))
                forecast = (candidate.get("ai_analysis") or {}).get("forecast_horizon_hours")
                if forecast is not None and math.isfinite(float(forecast)) and float(forecast) > 0:
                    sized["holding_hours"] = min(sized["holding_hours"], float(forecast))
        except (ValueError, TypeError, KeyError) as error:
            execution_reasons.append(f"An executable price, ATR, equity and valid order configuration are required: {error}")
        result = {
            **sized, "agent": "Risk Manager", "decision": "accepted" if not reasons else "rejected",
            "status": "completed", "reasons": reasons, "execution_reasons": execution_reasons,
            "execution_ready": not reasons and not execution_reasons and bool(sized.get("proposed_units")),
            "verified_equity": equity, "open_position_count": len(positions), "max_open_positions": max_positions,
            "atr14": atr14, "daily_trend_context": daily_trend, "daily_trend_alignment": trend_alignment,
            "market_session_context": market_session,
            "strategy_notes": strategy.get("notes"), "strategy_framework": strategy.get("decision_gate", {}).get("strategy_framework"),
            "risk_budget_multiplier": risk_budget_multiplier, "leverage": int(approved_leverage) if str(approved_leverage).replace(".", "", 1).isdigit() else None,
            "broker_order_submitted": False,
        }
        record_agent_event(
            "risk_manager", "completed", "news_candidate_portfolio_risk",
            "; ".join(reasons) if reasons else "Portfolio, position, quote, and ATR risk checks passed; no order was submitted.",
            result["decision"], result,
        )
        return result

    def check_risk(
        self,
        pnl: float,
        volatility: float,
        strategy_signal: Dict[str, Any],
        trend_review: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        rules = get_agent_rules().get("risk_manager", DEFAULT_AGENT_RULES["risk_manager"])
        news_context = strategy_signal.get("news_context", [])
        logger.info("Checking risk thresholds...")
        if pnl < -float(rules.get("max_drawdown", 0.05)) or volatility > float(rules.get("max_volatility", 0.03)):
            action = "EXIT_IMMEDIATELY"
            details = "Risk thresholds breached. Emergency exit triggered."
            decision = "rejected" if bool(rules.get("exit_on_risk_breach", True)) else "accepted"
            logger.warning(details)
        else:
            action = "HOLD"
            details = "Within risk limits. Continue monitoring."
            decision = "accepted"
        trend_opposes = (
            trend_review
            and trend_review.get("status") == "complete"
            and trend_review.get("opposes_proposed_position")
            and float(trend_review.get("confidence", 0.0)) >= float(rules.get("trend_opposition_min_confidence", 0.8))
        )
        if trend_opposes:
            if decision == "accepted":
                action = "BLOCK_TRADE"
            decision = "rejected"
            details = (
                f"{details} OpenAI trend review opposes the proposed {strategy_signal.get('position')} "
                f"position with {float(trend_review['confidence']):.0%} confidence."
            )
        details = f"{details} Received {len(news_context)} research headlines."

        save_risk_event(pnl=pnl, volatility=volatility, action=action, details=details)
        record_agent_event(
            "risk_manager",
            "completed",
            "risk_check",
            details,
            decision,
            {
                "pnl": pnl,
                "volatility": volatility,
                "action": action,
                "max_drawdown": float(rules.get("max_drawdown", 0.05)),
                "max_volatility": float(rules.get("max_volatility", 0.03)),
                "exit_on_risk_breach": bool(rules.get("exit_on_risk_breach", True)),
                "news_context": news_context,
                "ai_analysis": strategy_signal.get("ai_analysis"),
                "trend_review": trend_review,
                "trend_opposition_min_confidence": float(rules.get("trend_opposition_min_confidence", 0.8)),
            },
        )
        return {
            "action": action,
            "details": details,
            "decision": decision,
            "pnl": pnl,
            "volatility": volatility,
            "thresholds": {
                "max_drawdown": float(rules.get("max_drawdown", 0.05)),
                "max_volatility": float(rules.get("max_volatility", 0.03)),
                "exit_on_risk_breach": bool(rules.get("exit_on_risk_breach", True)),
            },
            "news_context": news_context,
            "ai_analysis": strategy_signal.get("ai_analysis"),
            "trend_review": trend_review,
        }


class ExecutionAgent:
    def review_news_demo_candidate(
        self,
        *,
        candidate: Dict[str, Any],
        strategy: Dict[str, Any],
        risk: Dict[str, Any],
        evaluation: Dict[str, Any],
        automation_enabled: bool,
    ) -> Dict[str, Any]:
        """Evaluate demo execution readiness without using historical evaluation as a lock."""
        rules = get_agent_rules().get("execution", DEFAULT_AGENT_RULES["execution"])
        reasons = []
        if not bool(rules.get("enabled", True)):
            reasons.append("Execution Agent is disabled.")
        if not strategy.get("decision_gate", {}).get("accepted"):
            reasons.append("Strategist did not approve this candidate.")
        if risk.get("decision") != "accepted":
            reasons.append("Risk Manager did not approve this candidate.")
        if not automation_enabled:
            reasons.append("Automated demo orders are disabled.")
        reasons.extend(risk.get("execution_reasons") or [])
        execution_ready = not reasons and bool(risk.get("execution_ready"))
        capacity_deferred = any(
            phrase in str(reason).casefold()
            for reason in risk.get("execution_reasons") or []
            for phrase in (
                "open position limit reached",
                "position in this instrument is already open",
                "no risk budget remains",
            "portfolio exposure capacity",
            "per-cycle order limit reached",
            )
        )
        if not automation_enabled and strategy.get("decision_gate", {}).get("accepted") and risk.get("decision") == "accepted":
            status = "automation_disabled"
        elif execution_ready:
            status = "ready"
        elif capacity_deferred:
            status = "deferred_capacity"
        else:
            status = "waiting_for_execution_data" if strategy.get("decision_gate", {}).get("accepted") else "rejected"
        result = {
            "agent": "Execution",
            "decision": (
                "pending" if status in {"automation_disabled", "waiting_for_execution_data"}
                else "deferred" if status == "deferred_capacity"
                else "accepted" if execution_ready else "rejected"
            ),
            "status": status,
            "reasons": reasons,
            "analysis_completed": True,
            "symbol": candidate.get("ticker"),
            "proposed_side": strategy.get("position"),
            "strategy_notes": strategy.get("notes"),
            "strategy_framework": strategy.get("decision_gate", {}).get("strategy_framework"),
            "market_session_context": candidate.get("market_session_context", {}),
            "broker_order_submitted": False,
            "evaluation_status_informational_only": evaluation.get("status", "not_run"),
        }
        record_agent_event(
            "execution", "completed", "news_candidate_execution_review",
            "; ".join(reasons), result["decision"], result,
        )
        return result

    def place_trade(self, signal: Dict[str, Any], pnl: float = 0.0, volatility: float = 0.01) -> Dict[str, Any]:
        agent_rules = get_agent_rules()
        rules = agent_rules.get("execution", DEFAULT_AGENT_RULES["execution"])
        logger.info("Placing paper trade based on strategy signal.")
        symbol = signal["assets"][0]
        side = signal["position"]
        strategist_rules = agent_rules.get("strategist", DEFAULT_AGENT_RULES["strategist"])
        configured_leverage = min(
            float(strategist_rules.get("default_leverage", 2.0)),
            float(strategist_rules.get("max_leverage", 3.0)),
        )
        leverage = float(signal.get("leverage", configured_leverage))
        exit_target = float(signal.get("exit_target", 1.0))

        decision = "accepted" if side in rules.get("allowed_side", ["LONG", "SHORT"]) else "rejected"

        trade = {
            "symbol": symbol,
            "side": side,
            "leverage": leverage,
            "exit_target": exit_target,
            "status": "PAPER_FILLED" if decision == "accepted" else "REJECTED",
            "notes": "Paper trade for local testing." if decision == "accepted" else "Execution rejected by rule set.",
        }
        save_trade(
            symbol=trade["symbol"],
            side=trade["side"],
            leverage=trade["leverage"],
            exit_target=trade["exit_target"],
            status=trade["status"],
            notes=trade["notes"],
        )
        record_agent_event(
            "execution",
            "completed",
            "trade_execution",
            trade["notes"],
            decision,
            trade,
        )
        return trade


def evaluate_strategist_gate(
    candidate: Dict[str, Any],
    proposed_position: str,
    related_news: List[Dict[str, Any]],
    rules: Dict[str, Any],
) -> Dict[str, Any]:
    rejection_reasons = []
    confidence = float(candidate.get("confidence", 0.0))
    base_threshold = float(rules.get("min_confidence", 0.7))
    price = candidate.get("price")
    quote = candidate.get("market_quote") or {}
    quote_available = bool(quote.get("available"))

    if not bool(rules.get("enabled", True)):
        rejection_reasons.append("Strategist is disabled.")
    if proposed_position not in {"LONG", "SHORT"}:
        rejection_reasons.append("No directional position was proposed.")
    # Quotes determine whether an order can be sized and sent, not whether
    # news/fundamentals provide a directional thesis. Execution rechecks quote.

    fundamentals = candidate.get("fundamentals") or {}
    revenue_growth = fundamentals.get("revenue_yoy_growth_pct")
    profit_margin = fundamentals.get("net_profit_margin_pct")
    free_cash_flow = fundamentals.get("free_cash_flow_ttm")
    debt_to_equity = fundamentals.get("debt_to_equity_ratio")
    eps_values = [
        value for value in (
            fundamentals.get("trailing_eps"),
            fundamentals.get("forward_eps"),
        )
        if value is not None
    ]
    eps_known = bool(eps_values)
    eps_strong = eps_known and all(float(value) > 0 for value in eps_values)
    eps_weak = eps_known and all(float(value) < 0 for value in eps_values)
    strong_checks = (
        revenue_growth is not None
        and float(revenue_growth) >= float(rules.get("strong_fundamentals_min_revenue_growth_pct", 15.0)),
        profit_margin is not None
        and float(profit_margin) >= float(rules.get("strong_fundamentals_min_net_profit_margin_pct", 20.0)),
        free_cash_flow is not None and float(free_cash_flow) > 0,
        debt_to_equity is not None
        and 0 <= float(debt_to_equity) <= float(rules.get("strong_fundamentals_max_debt_to_equity", 1.0)),
        eps_strong,
    )
    weak_checks = (
        revenue_growth is not None
        and float(revenue_growth) <= float(rules.get("weak_fundamentals_max_revenue_growth_pct", 0.0)),
        profit_margin is not None
        and float(profit_margin) <= float(rules.get("weak_fundamentals_max_net_profit_margin_pct", 5.0)),
        free_cash_flow is not None and float(free_cash_flow) < 0,
        debt_to_equity is not None
        and float(debt_to_equity) >= float(rules.get("weak_fundamentals_min_debt_to_equity", 2.0)),
        eps_weak,
    )
    known_fundamental_count = sum(value is not None for value in (
        revenue_growth, profit_margin, free_cash_flow, debt_to_equity
    )) + int(eps_known)
    strong_fundamentals = known_fundamental_count >= 3 and sum(strong_checks) >= 3
    weak_fundamentals = known_fundamental_count >= 3 and sum(weak_checks) >= 3
    if strong_fundamentals and weak_fundamentals:
        strong_fundamentals = False
        weak_fundamentals = False

    ticker = str(candidate.get("ticker", "")).strip().upper()
    company_name = str(candidate.get("company_name", "")).strip().casefold()
    company_alias = company_name.split(",", 1)[0].removesuffix(" inc.").strip()
    short_alias = company_alias.split()[0] if company_alias else ""
    bullish_terms = re.compile(
        r"\b(?:grow\w*|strong\w*|beat\w*|exceed\w*|record\w*|rais\w*|increas\w*|"
        r"profit\w*|upgrad\w*|approv\w*|expand\w*|launch\w*|win\w*|positive|surge\w*|"
        r"upside|outperform\w*|improv\w*|accelerat\w*)\b",
        re.IGNORECASE,
    )
    bearish_terms = re.compile(
        r"\b(?:declin\w*|weak\w*|slow\w*|soften\w*|cut\w*|lower\w*|miss\w*|drop\w*|"
        r"fell|fall\w*|pressure|risk|reduc\w*|downturn|headwind\w*|adverse|concern\w*|"
        r"sensitivity|loss\w*|downgrad\w*|warn\w*|lawsuit\w*|probe\w*|layoff\w*)\b",
        re.IGNORECASE,
    )
    company_news_sentiments = set()
    supplied_sentiment = candidate.get("company_news_sentiment")
    if isinstance(supplied_sentiment, str):
        supplied_sentiment = [supplied_sentiment]
    for sentiment in supplied_sentiment or []:
        normalized_sentiment = str(sentiment).lower()
        if normalized_sentiment in {"bullish", "bearish"}:
            company_news_sentiments.add(normalized_sentiment)
    for article in ([] if company_news_sentiments else related_news):
        text = f"{article.get('title', '')} {article.get('summary', '')}"
        company_mentioned = (
            (ticker and re.search(rf"\b{re.escape(ticker)}\b", text, re.IGNORECASE))
            or (company_alias and company_alias in text.casefold())
            or (len(short_alias) >= 4 and re.search(rf"\b{re.escape(short_alias)}\b", text, re.IGNORECASE))
        )
        if not company_mentioned:
            continue
        if bullish_terms.search(text):
            company_news_sentiments.add("bullish")
        if bearish_terms.search(text):
            company_news_sentiments.add("bearish")

    company_news_supports_position = (
        (proposed_position == "LONG" and "bullish" in company_news_sentiments)
        or (proposed_position == "SHORT" and "bearish" in company_news_sentiments)
    )
    company_news_opposes_position = (
        (proposed_position == "LONG" and "bearish" in company_news_sentiments)
        or (proposed_position == "SHORT" and "bullish" in company_news_sentiments)
    )
    fundamentals_align = (
        (proposed_position == "LONG" and strong_fundamentals)
        or (proposed_position == "SHORT" and weak_fundamentals)
    )
    fundamentals_conflict = (
        (proposed_position == "LONG" and weak_fundamentals)
        or (proposed_position == "SHORT" and strong_fundamentals)
    )
    countertrend = fundamentals_conflict or company_news_opposes_position
    fundamental_confirmation = fundamentals_align and company_news_supports_position

    daily_trend = candidate.get("daily_trend_context") or {}
    trend_state = str(daily_trend.get("trend_state") or "unknown").lower()
    current_price = candidate.get("price")
    sma50 = daily_trend.get("sma50")
    atr14 = daily_trend.get("atr14")
    conditional_setup = (
        (proposed_position == "LONG" and trend_state == "bullish" and "bearish" in company_news_sentiments)
        or (proposed_position == "SHORT" and trend_state == "bearish" and "bullish" in company_news_sentiments)
    )
    setup_info = mean_reversion_setup(candidate, proposed_position, rules) if conditional_setup else {}
    conditional_approved = bool(conditional_setup and setup_info.get("approved"))
    conditional_failed_variance = bool(conditional_setup and not conditional_approved)
    variance_available = bool(setup_info.get("atr14"))
    variance_distance = setup_info.get("distance")
    conditional_atr_multiple = float(rules.get("conditional_mean_reversion_min_atr_multiple", 1.25))

    if conditional_approved:
        # A sufficiently extended price in the prevailing daily trend qualifies
        # this news/trend conflict for the explicit conditional mean-reversion track.
        required_confidence = float(rules.get("conditional_mean_reversion_min_confidence", 0.7))
    elif conditional_failed_variance:
        rejection_reasons.extend(setup_info.get("reasons") or ["The post-news reversal is not confirmed."])
        required_confidence = base_threshold
    elif countertrend:
        if proposed_position == "SHORT":
            required_confidence = float(rules.get("countertrend_short_min_confidence", 0.85))
            require_directional_headline = bool(rules.get("require_company_specific_news_for_countertrend_short", True))
        else:
            required_confidence = float(rules.get("countertrend_long_min_confidence", 0.85))
            require_directional_headline = bool(rules.get("require_company_specific_news_for_countertrend_long", True))
        if confidence < required_confidence:
            rejection_reasons.append(
                f"Fundamentals or company news conflict with the {proposed_position.lower()} thesis; "
                f"countertrend confidence must be at least {required_confidence:.0%}."
            )
        if require_directional_headline and not company_news_supports_position:
            rejection_reasons.append(
                f"No company-specific headline supports the countertrend {proposed_position.lower()} thesis."
            )
    elif fundamental_confirmation and bool(rules.get("require_company_specific_news_for_fundamental_confirmation", True)):
        required_confidence = float(rules.get("fundamental_confirmation_min_confidence", 0.6))
        if confidence < required_confidence:
            rejection_reasons.append(
                f"Fundamental and headline evidence confirm the {proposed_position.lower()} thesis, "
                f"but confidence is below the {required_confidence:.0%} confirmation minimum."
            )
    else:
        required_confidence = base_threshold
        if confidence < required_confidence:
            rejection_reasons.append(
                f"Confidence {confidence:.0%} is below the {required_confidence:.0%} minimum."
            )

    trend_alignment = "unavailable"
    if not daily_trend.get("available") or trend_state not in {"bullish", "bearish", "mixed"}:
        # Missing trend context is recorded as unavailable; it is not evidence
        # against the independently researched news thesis.
        trend_alignment = "unavailable"
    elif proposed_position == "LONG" and trend_state == "bearish":
        trend_alignment = "opposes_news_direction"
        required_trend_confidence = float(rules.get("countertrend_long_min_confidence", 0.85))
        if not conditional_approved and confidence < required_trend_confidence:
            rejection_reasons.append(
                f"Bullish news conflicts with the bearish daily SMA trend; countertrend confidence must be at least {required_trend_confidence:.0%}."
            )
    elif proposed_position == "SHORT" and trend_state == "bullish":
        trend_alignment = "opposes_news_direction"
        required_trend_confidence = float(rules.get("countertrend_short_min_confidence", 0.85))
        if not conditional_approved and confidence < required_trend_confidence:
            rejection_reasons.append(
                f"Bearish news conflicts with the bullish daily SMA trend; countertrend confidence must be at least {required_trend_confidence:.0%}."
            )
    elif proposed_position == "LONG" and trend_state == "bullish":
        trend_alignment = "confirms_news_direction"
    elif proposed_position == "SHORT" and trend_state == "bearish":
        trend_alignment = "confirms_news_direction"
    else:
        trend_alignment = "mixed_or_not_directional"

    entry_floor = entry_ceiling = None
    framework = None
    notes = None
    if conditional_approved:
        framework = "CONDITIONAL_MEAN_REVERSION"
        entry_floor = setup_info["entry_zone_floor"]
        entry_ceiling = setup_info["entry_zone_ceiling"]
        atr14 = setup_info["atr14"]
        notes = (
            f"STRATEGY: {framework}; ENTRY_ZONE: {entry_floor:.4f} to {entry_ceiling:.4f}; "
            f"SOURCE_ATR: {atr14:g}."
        )

    return {
        "accepted": not rejection_reasons,
        "rejection_reasons": rejection_reasons,
        "confidence": confidence,
        "base_confidence_threshold": base_threshold,
        "required_confidence": required_confidence,
        "countertrend_short_confidence_threshold": float(
            rules.get("countertrend_short_min_confidence", 0.85)
        ),
        "fundamental_confirmation_min_confidence": float(
            rules.get("fundamental_confirmation_min_confidence", 0.6)
        ),
        "fundamentals_strong": strong_fundamentals,
        "fundamentals_weak": weak_fundamentals,
        "fundamentals_supporting_metrics": sum(strong_checks if proposed_position == "LONG" else weak_checks),
        "fundamentals_known_metrics": known_fundamental_count,
        "fundamental_alignment": (
            "aligned" if fundamentals_align else "countertrend" if fundamentals_conflict else "mixed_or_unknown"
        ),
        "company_news_sentiment": sorted(company_news_sentiments),
        "company_news_supports_position": company_news_supports_position,
        "company_news_opposes_position": company_news_opposes_position,
        "fundamental_confirmation": fundamental_confirmation,
        "daily_trend_state": trend_state,
        "daily_trend_alignment": trend_alignment,
        "strategy_framework": framework,
        "conditional_mean_reversion": {
            **setup_info,
            "eligible": conditional_setup,
            "approved": conditional_approved,
            "variance_available": variance_available,
            "price": current_price if variance_available else None,
            "sma50": sma50 if variance_available else None,
            "atr14": atr14 if variance_available else None,
            "atr_multiple": conditional_atr_multiple,
            "distance_from_sma50": variance_distance,
            "required_distance": setup_info.get("required_distance"),
            "entry_zone_floor": entry_floor,
            "entry_zone_ceiling": entry_ceiling,
        },
        "notes": notes,
        "daily_trend_context": daily_trend,
        "market_quote_available": quote_available,
        "market_quote": quote,
        "fundamentals": fundamentals,
    }


def calculate_candidate_success_score(
    candidate: Dict[str, Any],
    strategy: Dict[str, Any],
    risk: Dict[str, Any],
) -> Dict[str, Any]:
    confidence = min(1.0, max(0.0, float(candidate.get("confidence", 0.0))))
    gate = strategy.get("decision_gate", {})
    position = strategy.get("position")
    trend_review = risk.get("trend_review") or {}
    trend_direction = trend_review.get("overall_direction")
    trend_agrees = (
        trend_review.get("status") == "complete"
        and trend_review.get("confidence", 0.0) >= 0.6
        and ((position == "LONG" and trend_direction == "bullish")
             or (position == "SHORT" and trend_direction == "bearish"))
    )
    confirmations = {
        "fundamental_alignment": gate.get("fundamental_alignment") == "aligned",
        "company_headline": bool(gate.get("company_news_supports_position")),
        "multi_horizon_trend": bool(trend_agrees),
    }
    score = 0.70 * confidence + sum(confirmations.values()) * 0.10
    return {
        "score": round(min(1.0, score), 4),
        "components": {
            "research_confidence": round(confidence, 4),
            **confirmations,
        },
        "method": "70% research confidence + 10% each for direction-aligned fundamentals, a supporting company headline, and a supporting multi-horizon trend; ranking heuristic, not calibrated probability.",
    }


class OverwatcherAgent:
    """Validates agent configurations are correctly applied and monitors trade health."""
    
    def __init__(self):
        self.audit_log_path = DB_PATH.parent / "audit_logs" / f"oversight_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jsonl"
        self.audit_log_path.parent.mkdir(exist_ok=True)
    
    def validate_trade_configuration(self, trade_id: int) -> Dict[str, Any]:
        """Validate that all agent configurations were correctly applied to a trade."""
        conn = get_connection()
        trade = conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        
        if not trade:
            return {"trade_id": trade_id, "status": "error", "message": "Trade not found"}
        
        # Get agent rules
        agent_rules = get_agent_rules()
        strategist_rules = agent_rules.get("strategist", DEFAULT_AGENT_RULES["strategist"])
        risk_rules = agent_rules.get("risk_manager", DEFAULT_AGENT_RULES["risk_manager"])
        
        # Get trade events to see what each agent decided
        events = conn.execute(
            "SELECT stage, agent_name, decision, details FROM trade_events WHERE trade_id = ? ORDER BY id",
            (trade_id,)
        ).fetchall()
        conn.close()
        
        checks = {
            "trade_id": trade_id,
            "ticker": trade["symbol"],
            "status": trade["status"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "configuration_checks": {},
            "issues": [],
        }
        
        # Judge historical trades against their entry-time configuration.
        snapshot_rules = json.loads(trade["config_json"] or "{}")
        strategist_rules = snapshot_rules.get("strategist", strategist_rules)
        expected_leverage = float(strategist_rules.get("default_leverage", 2.0))
        max_leverage = float(strategist_rules.get("max_leverage", 3.0))
        actual_leverage = float(trade["leverage"])
        
        checks["configuration_checks"]["leverage"] = {
            "expected": expected_leverage,
            "max_allowed": max_leverage,
            "actual": actual_leverage,
            "valid": abs(actual_leverage - min(expected_leverage, max_leverage)) < 0.01
        }
        
        if not checks["configuration_checks"]["leverage"]["valid"]:
            checks["issues"].append(f"Leverage mismatch: expected {expected_leverage}, got {actual_leverage}")
        
        side = trade["side"]
        entry = trade["broker_fill_price"] or trade["entry_price"]
        snapshot_rules = json.loads(trade["config_json"] or "{}")
        if snapshot_rules:
            strategist_rules = snapshot_rules.get("strategist", strategist_rules)
            approved = min(float(strategist_rules.get("default_leverage", 2)), float(strategist_rules.get("max_leverage", 3)))
            checks["configuration_checks"]["leverage"] = {"expected": approved, "actual": actual_leverage, "valid": actual_leverage == approved}
        for field, key, sign in (("stop_loss_price", "stop_loss", -1), ("take_profit_price", "take_profit", 1)):
            level = trade[field]
            direction = 1 if side == "LONG" else -1
            valid = bool(entry and level and direction*sign*(float(level)-float(entry)) > 0)
            checks["configuration_checks"][key] = {"actual": level, "entry": entry, "valid": valid,
                "basis": "Side-aware protective/target levels; strategy policy governs ATR distances."}
            if level is not None and not valid:
                checks["issues"].append(f"{key} is on the wrong side of the entry price.")
        if trade["strategy_version"] == WORKFLOW_VERSION:
            execution_rules = snapshot_rules.get("execution", DEFAULT_AGENT_RULES["execution"])
            spent = float(trade["quantity"])*float(entry or 0)/actual_leverage
            size_valid = float(trade["quantity"]) <= float(execution_rules["max_order_units"]) and spent <= float(execution_rules["max_order_spend_usd"])
            checks["configuration_checks"]["size"] = {"units": trade["quantity"], "margin": spent, "valid": size_valid}
            if not size_valid:
                checks["issues"].append("Actual fill exceeds the entry-time units or spending cap.")
        # Get strategist and research decisions from events
        strategist_decision = None
        researcher_decision = None
        for event in events:
            if event["stage"] in {"strategy", "strategist"}:
                strategist_decision = json.loads(event["details"]) if event["details"] and event["details"] != '{}' else {}
            elif event["stage"] in {"research", "researcher"}:
                researcher_decision = json.loads(event["details"]) if event["details"] and event["details"] != '{}' else {}
        
        checks["agent_decisions"] = {
            "researcher": "approved" if researcher_decision else "unknown",
            "strategist": "approved" if strategist_decision and strategist_decision.get("leverage") else "unknown",
        }
        
        return checks
    
    def audit_trade(self, trade_id: int) -> None:
        """Log trade validation results to audit log."""
        result = self.validate_trade_configuration(trade_id)
        with open(self.audit_log_path, "a") as f:
            f.write(json.dumps(result) + "\n")
    
    def audit_all_trades(self) -> Dict[str, Any]:
        """Audit all OPEN and recently CLOSED trades."""
        conn = get_connection()
        trades = conn.execute(
            "SELECT id FROM trades WHERE status IN ('OPEN', 'CLOSED', 'RECONCILIATION_REQUIRED', 'EXIT_PENDING') ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        conn.close()
        
        summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_trades_audited": len(trades),
            "audit_log_file": str(self.audit_log_path),
            "trades_audited": []
        }
        
        for trade in trades:
            self.audit_trade(trade["id"])
            summary["trades_audited"].append(trade["id"])
        
        # Log summary
        with open(self.audit_log_path, "a") as f:
            f.write(json.dumps({"summary": summary}) + "\n")
        
        logger.info(f"Audit complete: {len(trades)} trades audited. Log: {self.audit_log_path}")
        return summary


def _parse_risk_setting(name: str, default: float) -> float:
    try:
        number = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return number if math.isfinite(number) and number > 0 else default


def group_news_candidates(articles: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group ticker-tagged news without requiring publication timestamps."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for article in articles:
        ticker = str(article.get("ticker") or "").strip().upper()
        published_at = str(article.get("published_at") or "").strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
            continue
        if published_at:
            try:
                published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
                article = {**article, "published_at": published.astimezone(timezone.utc).isoformat()}
            except ValueError:
                article = {**article, "published_at": None}
        else:
            article = {**article, "published_at": None}
        tickers = list(dict.fromkeys([ticker, *(article.get("related_tickers") or [])]))
        for related in tickers:
            related = str(related).upper()
            if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", related):
                grouped.setdefault(related, []).append({**article, "ticker": related})
    return grouped


def newest_known_article_time(articles: List[Dict[str, Any]]) -> float:
    timestamps = []
    for article in articles:
        try:
            published = datetime.fromisoformat(str(article.get("published_at") or "").replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            timestamps.append(published.timestamp())
        except ValueError:
            continue
    return max(timestamps, default=0.0)


def _instrument_id_from_trade_events(conn: sqlite3.Connection, trade_id: int) -> Optional[str]:
    row = conn.execute(
        "SELECT details FROM trade_events WHERE trade_id = ? AND stage = 'strategy' ORDER BY id DESC LIMIT 1",
        (trade_id,),
    ).fetchone()
    if not row:
        return None
    try:
        details = json.loads(row["details"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None

    def find(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            for key in ("instrument_id", "instrumentId", "instrumentID", "internalInstrumentId"):
                candidate = value.get(key)
                if candidate is not None and str(candidate).strip():
                    return str(candidate).strip()
            for nested in value.values():
                found = find(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = find(nested)
                if found:
                    return found
        return None

    return find(details)


def reconciliation_blocks_new_order(trade: Dict[str, Any]) -> bool:
    """Block uncertain submissions, but not known positions with a recorded broker mismatch."""
    status = str(trade.get("status") or "")
    if status in {"SUBMITTING", "SUBMITTED_PENDING_RECONCILIATION"}:
        return True
    if status != "RECONCILIATION_REQUIRED":
        return False
    known_position_mismatch = (
        bool(trade.get("broker_position_id"))
        and trade.get("reconciliation_status") in {
            "broker_stop_differs_from_requested_stop",
            "unexpected_broker_take_profit",
        }
    )
    return not known_position_mismatch


def reconcile_news_demo_positions(positions: List[Dict[str, Any]]) -> Dict[str, int]:
    """Reconcile local demo lifecycles to a verified broker positions list."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM trades WHERE status IN ('SUBMITTED_PENDING_RECONCILIATION', 'OPEN', 'EXIT_PENDING', 'RECONCILIATION_REQUIRED') "
        "AND (broker_order_id IS NOT NULL OR broker_position_id IS NOT NULL OR status = 'RECONCILIATION_REQUIRED')"
    ).fetchall()
    counts = {"open": 0, "closed": 0, "pending": 0, "errors": 0}
    event_messages: List[tuple[int, str, str]] = []
    for row in rows:
        trade = dict(row)
        expected_instrument_id = _instrument_id_from_trade_events(conn, int(trade["id"]))
        matched = [position for position in positions if (
            trade.get("broker_position_id") and str(position.get("position_id")).removesuffix(".0") == str(trade["broker_position_id"]).removesuffix(".0")
        )]
        if not matched and not trade.get("broker_position_id"):
            by_instrument = [position for position in positions if (
                expected_instrument_id
                and position.get("instrument_id") is not None
                and str(position.get("instrument_id")) == expected_instrument_id
            )]
            if len(by_instrument) == 1:
                matched = by_instrument
        if not matched and not trade.get("broker_position_id"):
            by_symbol = [position for position in positions if str(position.get("symbol") or "").upper() == str(trade["symbol"]).upper()]
            if len(by_symbol) == 1:
                matched = by_symbol
        if len(matched) != 1:
            previously_filled = bool(trade.get("broker_position_id") and trade.get("broker_fill_price") and trade.get("broker_fill_units"))
            if trade["status"] == "EXIT_PENDING" or previously_filled:
                conn.execute(
                    "UPDATE trades SET status = 'CLOSED_PNL_UNAVAILABLE', closed_at = CURRENT_TIMESTAMP, reconciliation_status = 'broker_closed_fill_unavailable' WHERE id = ?",
                    (trade["id"],),
                )
                event_messages.append((int(trade["id"]), "accepted", "A complete eToro snapshot no longer reports the previously verified position. Close reason, fill and realized P&L await broker history."))
                counts["closed"] += 1
            elif trade["status"] in {"OPEN", "SUBMITTED_PENDING_RECONCILIATION"}:
                conn.execute(
                    "UPDATE trades SET status = 'RECONCILIATION_REQUIRED', reconciliation_status = 'position_not_confirmed_by_broker' WHERE id = ?",
                    (trade["id"],),
                )
                counts["errors"] += 1
            else:
                counts["pending"] += 1
            continue
        position = matched[0]
        fill_price = position.get("open_rate")
        fill_units = position.get("units")
        position_id = position.get("position_id")
        if not fill_price or not fill_units or not position_id:
            conn.execute(
                "UPDATE trades SET status = 'RECONCILIATION_REQUIRED', reconciliation_status = 'broker_fill_fields_missing' WHERE id = ?",
                (trade["id"],),
            )
            counts["errors"] += 1
            continue
        requested_stop = trade.get("stop_loss_price")
        broker_stop = position.get("stop_loss_price")
        broker_take_profit = position.get("take_profit_price")
        stop_matches = bool(
            requested_stop and broker_stop
            and abs(float(requested_stop) - float(broker_stop)) <= max(0.01, abs(float(requested_stop)) * 0.0001)
        )
        requested_take_profit = trade.get("take_profit_price")
        # Compare the intended target with the broker report; never infer a match.
        take_profit_matches = (
            broker_take_profit is None if requested_take_profit is None else
            broker_take_profit is not None and abs(float(requested_take_profit) - float(broker_take_profit))
            <= max(0.01, abs(float(requested_take_profit)) * 0.0001)
        )
        policy = json.loads(trade.get("policy_json") or "{}")
        protection_pending = (
            trade.get("reconciliation_status") == "protection_update_pending"
            and time.time() - float(policy.get("last_protection_request") or 0) < 300
        )
        status = "EXIT_PENDING" if trade["status"] == "EXIT_PENDING" else "OPEN" if stop_matches and take_profit_matches else "RECONCILIATION_REQUIRED"
        reconciliation_reason = (
            "close_submitted" if status == "EXIT_PENDING" else "reconciled" if status == "OPEN"
            else "protection_update_pending" if protection_pending
            else "broker_stop_differs_from_requested_stop" if not stop_matches
            else "unexpected_broker_take_profit"
        )
        conn.execute(
            "UPDATE trades SET status = ?, broker_position_id = ?, broker_fill_price = ?, broker_fill_units = ?, entry_price = ?, quantity = ?, current_price = ?, broker_fees = COALESCE(?, broker_fees), reconciliation_status = ?, stop_state = ?, allocated_capital=COALESCE(?,allocated_capital), notes=CASE WHEN ?='OPEN' THEN 'eToro demo position confirmed from the broker portfolio; fill, quantity and protection were reconciled.' ELSE notes END WHERE id = ?",
            (status, str(position_id), float(fill_price), float(fill_units), float(fill_price), float(fill_units),
             position.get("current_rate") or fill_price, position.get("fees"),
             reconciliation_reason,
             "broker_reported_matches_requested" if stop_matches else "stop_not_confirmed", position.get("amount"), status, trade["id"]),
        )
        counts["pending" if status == "EXIT_PENDING" else "open" if status == "OPEN" else "errors"] += 1
        if not take_profit_matches and trade.get("reconciliation_status") != reconciliation_reason:
            event_messages.append((int(trade["id"]), "rejected", f"Broker reports take-profit {broker_take_profit}; requested target is {requested_take_profit}. Position requires broker reconciliation."))
    conn.commit()
    conn.close()
    for trade_id, decision, message in event_messages:
        record_trade_event(trade_id, "reconciliation", "execution", "completed", decision, message, {})
    return counts


def is_fresh_etoro_quote(
    quote: Dict[str, Any], *, checked_at: Optional[datetime] = None, max_age_seconds: int = 60,
) -> bool:
    """Return whether an eToro bid/ask quote is current at the time it is checked."""
    if not quote.get("available") or quote.get("provider") != "eToro" or not quote.get("fetched_at"):
        return False
    try:
        quote_time = datetime.fromisoformat(str(quote["fetched_at"]).replace("Z", "+00:00"))
        if quote_time.tzinfo is None:
            quote_time = quote_time.replace(tzinfo=timezone.utc)
        age_seconds = ((checked_at or datetime.now(timezone.utc)) - quote_time.astimezone(timezone.utc)).total_seconds()
        return 0 <= age_seconds <= max_age_seconds
    except (TypeError, ValueError):
        return False


def news_candidate_auto_submit_ready(
    *, automation_enabled: bool, research_decision: str, strategy_result: Dict[str, Any],
    strategy: Dict[str, Any], risk_result: Dict[str, Any], execution_result: Dict[str, Any],
) -> bool:
    """Require every research, risk, and execution gate to be positively ready."""
    return bool(
        automation_enabled
        and research_decision == "accepted"
        and strategy_result.get("decision") == "accepted"
        and strategy.get("position") in {"LONG", "SHORT"}
        and risk_result.get("decision") == "accepted"
        and risk_result.get("execution_ready") is True
        and execution_result.get("decision") == "accepted"
        and execution_result.get("status") == "ready"
    )


def news_candidate_lifecycle_status(
    *, research_decision: str, strategy_result: Dict[str, Any],
    risk_result: Dict[str, Any], execution_result: Dict[str, Any],
) -> str:
    """Persist the terminal analysis/execution gate in the lifecycle status."""
    if research_decision == "rejected":
        return "REJECTED_RESEARCH"
    if strategy_result.get("decision") == "rejected":
        return "REJECTED_STRATEGY"
    if risk_result.get("decision") == "rejected":
        return "REJECTED_RISK"
    if execution_result.get("decision") == "rejected":
        return "REJECTED_EXECUTION"

    capacity_reasons = [
        *(risk_result.get("execution_reasons") or []),
        *(execution_result.get("reasons") or []),
    ]
    if any(
        phrase in str(reason).casefold()
        for reason in capacity_reasons
        for phrase in (
            "open position limit reached",
            "position in this instrument is already open",
            "no risk budget remains",
            "portfolio exposure capacity",
            "per-cycle order limit reached",
        )
    ):
        return "DEFERRED_CAPACITY"
    return "ANALYSIS_ONLY"


async def run_etoro_news_research_cycle(settings: Optional[IntegrationSettings] = None) -> Dict[str, Any]:
    """Discover news, run every agent gate, and submit eligible automated demo orders."""
    settings = settings or IntegrationSettings.from_environment()
    if settings.broker_provider != "etoro" or not settings.etoro_demo_trading:
        message = "eToro research is available only when the broker is configured for demo mode."
        return {"mode": "blocked", "status": "blocked", "reason": message, "signals": {}, "trades": []}

    automation_enabled = get_demo_automated_trading_enabled()
    evaluation = get_strategy_evaluation()  # displayed for research; never gates demo orders

    adapter = EtoroBrokerAdapter(settings)
    try:
        account, broker_snapshot = await asyncio.gather(
            asyncio.to_thread(adapter.get_account_snapshot),
            asyncio.to_thread(adapter.get_positions),
        )
    except Exception as error:
        logger.warning("eToro account snapshot unavailable; continuing news analysis: %s", type(error).__name__)
        account = {"available": False, "reason": f"{type(error).__name__}: {error}"}
        broker_snapshot = {"available": False, "positions": [], "reason": account["reason"]}
    if not account.get("available") or not broker_snapshot.get("available"):
        logger.info("eToro state is unavailable; candidates will be analyzed, while execution remains unavailable.")
    else:
        reconciliation = reconcile_news_demo_positions(broker_snapshot.get("positions") or [])
        if reconciliation["errors"]:
            logger.warning("Some eToro demo trades require reconciliation: %s", reconciliation)

    try:
        fetched_articles = await YahooFinanceNewsProvider(settings).fetch_news()
        article_counts = save_news_articles(fetched_articles) if fetched_articles else {"inserted": 0, "updated": 0}
    except Exception as error:
        fetched_articles = []
        article_counts = {"inserted": 0, "updated": 0}
        logger.warning("Dynamic Yahoo news discovery failed: %s", type(error).__name__)

    grouped = group_news_candidates(fetched_articles)

    max_candidates = max(1, min(20, int(os.getenv("FINBOT_RESEARCH_MAX_CANDIDATES", "8"))))
    candidates = sorted(grouped.items(), key=lambda pair: newest_known_article_time(pair[1]), reverse=True)[:max_candidates]
    signals: Dict[str, Dict[str, Any]] = {}
    research_results: List[Dict[str, Any]] = []
    submitted_this_cycle = 0
    lifecycle_trades: List[Dict[str, Any]] = []
    researcher_agent = ResearcherAgent()
    strategist_agent = StrategistAgent()
    risk_agent = RiskManagerAgent()
    execution_agent = ExecutionAgent()
    risk_rules = get_agent_rules().get("risk_manager", DEFAULT_AGENT_RULES["risk_manager"])
    ai_enabled = get_ai_research_enabled()
    ai = OpenAIResearchAdapter(settings, record_openai_usage) if settings.openai_api_key and ai_enabled else None
    for ticker, headlines in candidates:
        fundamentals, quote, trends = await asyncio.gather(
            fetch_company_fundamentals(ticker),
            fetch_market_quote(ticker),
            fetch_market_trends(ticker),
            return_exceptions=True,
        )
        fundamentals = fundamentals if isinstance(fundamentals, dict) else {"data_status": "unavailable", "reason": str(fundamentals)}
        quote = quote if isinstance(quote, dict) else {"available": False, "provider": "eToro", "reason": str(quote)}
        trends = trends if isinstance(trends, dict) else {"data_status": "unavailable", "reason": str(trends)}
        company_name = str(fundamentals.get("company_name") or ticker)
        instrument: Dict[str, Any] = {}
        instrument_error = None
        try:
            instrument = await asyncio.to_thread(adapter.resolve_instrument, ticker)
            if not instrument.get("tradeable") or not instrument.get("supported_asset"):
                raise ValueError("eToro did not confirm a tradeable stock or ETF.")
            if str(instrument.get("currency") or "").upper() != "USD":
                raise ValueError("The current research workflow supports USD-denominated instruments only.")
        except Exception as error:
            instrument_error = f"{type(error).__name__}: {error}"

        market_session_context = build_market_session_context(
            instrument=instrument,
            fundamentals=fundamentals,
            quote=quote,
        )

        # Compare with the time the quote was checked, not the start of the
        # whole research cycle; network and AI work can take longer than 60s.
        quote_fresh = is_fresh_etoro_quote(quote)

        reaction = await asyncio.to_thread(load_news_reaction, ticker, headlines)
        trends = {**trends, "news_reaction": reaction, "market_session_context": market_session_context}
        ai_result = await researcher_agent.analyze_news_candidate(
            ticker=ticker,
            company_name=company_name,
            sector=fundamentals.get("sector"),
            headlines=headlines,
            fundamentals=fundamentals,
            market_context=trends,
            research_adapter=ai,
        )

        signal = str(ai_result.get("signal") or "neutral").lower()
        if signal not in {"bullish", "bearish", "neutral"}:
            signal = "neutral"
        confidence = float(ai_result.get("confidence") or 0.0)
        confidence = min(1.0, max(0.0, confidence))
        reasons = []
        if not headlines:
            reasons.append("No Yahoo news articles were available for this candidate.")
        if ai_result.get("status") != "available":
            reasons.append(str(ai_result.get("reason") or "OpenAI research is unavailable."))
        if instrument_error:
            # Record missing broker instrument data, but keep it out of the
            # research verdict; instrument availability is an execution check.
            logger.info("eToro instrument unavailable for %s: %s", ticker, instrument_error)
        if not fundamentals or fundamentals.get("data_status") != "available":
            reasons.append("Yahoo company financial data is unavailable or incomplete.")
        if signal == "neutral":
            reasons.append("Research found no directional evidence.")
        related_news = [{
            **article,
            "relevance_status": "relevant",
            "relevance_tickers": [ticker],
        } for article in headlines]
        quote_price = quote.get("ask") or quote.get("price")
        try:
            bars = await asyncio.to_thread(load_daily_bars, ticker, "1y")
        except Exception as error:
            logger.info("Historical price context unavailable for %s: %s", ticker, type(error).__name__)
            bars = None
        atr14 = float(bars.iloc[-1]["atr14"]) if bars is not None and not bars.empty else None
        atr_timestamp = bars.index[-1].isoformat() if bars is not None and not bars.empty else None
        daily_trend_context = evaluate_latest_signal(ticker, bars)
        if not daily_trend_context.get("available"):
            daily_trend_context = {**daily_trend_context, "trend_state": "unknown"}
        provisional_side = "LONG" if signal == "bullish" else "SHORT"
        if ai_result.get("reaction_assessment") == "temporary" and daily_trend_context.get("trend_state") == "bullish" and signal == "bearish":
            provisional_side = "LONG"
        elif ai_result.get("reaction_assessment") == "temporary" and daily_trend_context.get("trend_state") == "bearish" and signal == "bullish":
            provisional_side = "SHORT"
        quote_price = quote.get("ask" if provisional_side == "LONG" else "bid")
        research_candidate = {
            "ticker": ticker,
            "company_name": company_name,
            "sector": fundamentals.get("sector") or "Unknown",
            "signal": signal,
            "company_news_sentiment": signal,
            "news_reaction": reaction,
            "confidence": confidence,
            "catalyst": ai_result.get("catalyst") or "news_research",
            "notes": ai_result.get("summary") or ai_result.get("reason") or "OpenAI research did not return a summary.",
            "price": quote_price or (float(bars.iloc[-1]["Close"]) if bars is not None and not bars.empty else None),
            "market_quote": {**quote, "available": bool(quote.get("available") and quote_fresh)},
            "daily_trend_context": daily_trend_context,
            "market_session_context": market_session_context,
            "fundamentals": fundamentals,
            "ai_analysis": ai_result,
            "related_news": related_news,
        }
        research_decision = (
            "unavailable" if ai_result.get("status") != "available"
            else "rejected" if signal == "neutral" or ai_result.get("company_relevance") == "irrelevant" or confidence < float(get_agent_rules()["researcher"].get("min_confidence", .6))
            else "accepted"
        )
        researcher_result = {
            "agent": "Researcher",
            "decision": research_decision,
            "status": "unavailable" if research_decision == "unavailable" else "completed",
            "source": "Yahoo Finance headlines + company financials + market context + OpenAI research",
            "signal": signal,
            "confidence": confidence,
            "headline_count": len(headlines),
            "openai": ai_result,
            "evidence": {"headlines": headlines[:8], "fundamentals": fundamentals, "market_context": trends},
            "market_session_context": market_session_context,
        }
        record_agent_event(
            "researcher", researcher_result["status"], "dynamic_news_research",
            f"{ticker}: {signal} research from {len(headlines)} recent Yahoo headlines.",
            research_decision, researcher_result,
        )

        if research_decision == "rejected":
            strategy_payload = {"position": "HOLD", "leverage": 1, "decision_gate": {
                "accepted": False, "rejection_reasons": ["Researcher rejected this candidate; later agents were skipped."],
            }}
            strategy_result = {"agent": "Strategist", "decision": "skipped", "status": "skipped",
                               "decision_gate": strategy_payload["decision_gate"], "analysis": {}}
            risk_result = {"agent": "Risk Manager", "decision": "skipped", "status": "skipped",
                           "reasons": ["Skipped because Researcher rejected this candidate."],
                           "execution_reasons": [], "execution_ready": False}
            execution_result = {"agent": "Execution", "decision": "skipped", "status": "skipped",
                                "reasons": ["Skipped because Researcher rejected this candidate."]}
        else:
            strategy_payload = strategist_agent.analyze_and_decide({
            "tech_data": research_candidate,
            "energy_data": research_candidate,
            "recent_news": related_news,
            })
            strategy_result = {
            "agent": "Strategist",
            "decision": "accepted" if strategy_payload.get("decision_gate", {}).get("accepted") else "rejected",
            "status": "completed",
            "position": strategy_payload.get("position"),
            "leverage": strategy_payload.get("leverage"),
            "decision_gate": strategy_payload.get("decision_gate"),
            "analysis": strategy_payload,
            "candidate_context": research_candidate,
            }
            if strategy_result["decision"] == "rejected":
                risk_result = {"agent": "Risk Manager", "decision": "skipped", "status": "skipped",
                               "reasons": ["Skipped because Strategist rejected this candidate."],
                               "execution_reasons": [], "execution_ready": False}
                execution_result = {"agent": "Execution", "decision": "skipped", "status": "skipped",
                                    "reasons": ["Skipped because Strategist rejected this candidate."]}
            else:
                risk_result = risk_agent.review_news_demo_candidate(
                    candidate=research_candidate,
                    strategy=strategy_payload,
                    account=account,
                    broker_snapshot=broker_snapshot,
                    instrument=instrument,
                    atr14=atr14,
                )
                if risk_result["decision"] == "rejected":
                    execution_result = {"agent": "Execution", "decision": "skipped", "status": "skipped",
                                        "reasons": ["Skipped because Risk Manager rejected this candidate."]}
                else:
                    if submitted_this_cycle >= int(get_agent_rules()["execution"].get("max_trades_per_cycle", 2)):
                        risk_result = {**risk_result, "execution_ready": False,
                                       "execution_reasons": [*risk_result.get("execution_reasons", []), "Per-cycle order limit reached."]}
                    execution_result = execution_agent.review_news_demo_candidate(
                        candidate=research_candidate,
                        strategy=strategy_payload,
                        risk=risk_result,
                        evaluation=evaluation,
                        automation_enabled=automation_enabled,
                    )
        agent_workflow = {
            "researcher": researcher_result,
            "strategist": strategy_result,
            "risk_manager": risk_result,
            "execution": execution_result,
        }
        if strategy_result["decision"] != "accepted":
            reasons.extend(strategy_result["decision_gate"].get("rejection_reasons") or [])
        if risk_result["decision"] != "accepted":
            reasons.extend(risk_result.get("reasons") or [])
        analysis_reasons = list(reasons)
        if researcher_result["status"] == "unavailable":
            analysis_reasons.append(str(ai_result.get("reason") or "OpenAI research is unavailable."))
        signal_result = {
            "ticker": ticker,
            "company_name": company_name,
            "signal": signal,
            "confidence": confidence,
            "catalyst": ai_result.get("catalyst"),
            "research_status": ai_result.get("status"),
            "research_summary": ai_result.get("summary"),
            "fundamental_analysis": ai_result.get("fundamental_analysis"),
            "research_input": {
                "provider": "Yahoo Finance News",
                "article_count": len(headlines),
                "articles": headlines[:8],
                "company_data_provider": fundamentals.get("data_source"),
                "company_data_status": fundamentals.get("data_status"),
                "market_trend_provider": trends.get("data_source"),
                "market_trend_fetched_at": trends.get("fetched_at"),
                "atr14": atr14,
                "atr_provider": "Yahoo Finance adjusted daily OHLC" if atr14 is not None else None,
                "atr_timestamp": atr_timestamp,
                "daily_trend_provider": daily_trend_context.get("signal_provider"),
                "daily_trend_timestamp": daily_trend_context.get("signal_timestamp"),
                "daily_trend_context": daily_trend_context,
                "quote_provider": quote.get("provider"),
                "quote_timestamp": quote.get("fetched_at"),
                "instrument_id": instrument.get("instrument_id"),
                "primary_exchange": market_session_context.get("primary_exchange"),
                "exchange_timezone": market_session_context.get("exchange_timezone"),
                "market_session": market_session_context.get("market_session"),
                "underlying_session_state": market_session_context.get("underlying_session_state"),
                "etoro_execution_state": market_session_context.get("etoro_execution_state"),
                "market_session_context": market_session_context,
            },
            "fundamentals": fundamentals,
            "market_trends": trends,
            "quote": quote,
            "agent_workflow": agent_workflow,
            "analysis_status": "complete" if researcher_result["status"] == "completed" else "incomplete",
            "analysis_decision": "unavailable" if research_decision == "unavailable" else "accepted" if (
                research_decision == "accepted"
                and strategy_result["decision"] == "accepted"
                and risk_result["decision"] == "accepted"
            ) else "rejected",
            "execution_status": execution_result["status"],
            "execution_reasons": execution_result.get("reasons") or [],
            "decision": "analysis_complete" if researcher_result["status"] == "completed" else "analysis_incomplete",
            "analysis_reasons": analysis_reasons,
            "reasons": analysis_reasons,
        }
        # Persist the complete analysis as a non-executed trade lifecycle so it
        # appears in the same Trade Lifecycle UI as the local agent workflow.
        lifecycle_status = news_candidate_lifecycle_status(
            research_decision=research_decision,
            strategy_result=strategy_result,
            risk_result=risk_result,
            execution_result=execution_result,
        )
        signal_timestamp = max(
            (str(article.get("published_at") or "") for article in headlines),
            default=None,
        ) or None
        notes = "Research opportunity recorded. " + "; ".join(
            execution_result.get("reasons") or ["Execution review complete."]
        )
        trade_id = save_trade(
            symbol=ticker,
            side=strategy_payload.get("position") or "HOLD",
            leverage=float(strategy_payload.get("leverage") or 1),
            exit_target=1.0,
            status=lifecycle_status,
            notes=notes,
            allocated_capital=0.0,
            entry_price=float(quote_price) if quote_price is not None else None,
            current_price=float(quote_price) if quote_price is not None else None,
            quantity=0.0,
            company_name=company_name,
            stop_loss_price=risk_result.get("proposed_stop"),
            take_profit_price=risk_result.get("proposed_take_profit"),
            signal_timestamp=signal_timestamp,
            signal_provider="Yahoo Finance News + OpenAI research",
            quote_provider=str(quote.get("provider") or "unknown"),
            quote_timestamp=quote.get("fetched_at"),
        )
        signal_result["trade_id"] = trade_id
        record_trade_event(
            trade_id, "research", "researcher", researcher_result["status"],
            researcher_result["decision"],
            f"{signal.upper()} research based on {len(headlines)} news items and company evidence.",
            researcher_result,
        )
        record_trade_event(
            trade_id, "strategy", "strategist", "skipped" if strategy_result["decision"] == "skipped" else "completed",
            strategy_result["decision"],
            "; ".join(strategy_result["decision_gate"].get("rejection_reasons") or [
                f"Strategist proposed {strategy_result.get('position')} with leverage {strategy_result.get('leverage')}x."
            ]),
            strategy_result,
        )
        record_trade_event(
            trade_id, "risk", "risk_manager", "skipped" if risk_result["decision"] == "skipped" else "completed",
            risk_result["decision"],
            "; ".join(risk_result.get("reasons") or ["Portfolio and ATR-based sizing checks passed."]),
            risk_result,
        )
        record_trade_event(
            trade_id, "execution", "execution",
            "skipped" if execution_result["decision"] == "skipped" else "completed",
            execution_result["decision"],
            "; ".join(execution_result.get("reasons") or [
                "Execution readiness approved; final broker recheck and demo submission follow."
            ]),
            {**execution_result, "trade_id": trade_id},
        )
        if news_candidate_auto_submit_ready(
            automation_enabled=automation_enabled,
            research_decision=research_decision,
            strategy_result=strategy_result,
            strategy=strategy_payload,
            risk_result=risk_result,
            execution_result=execution_result,
        ):
            try:
                submission = await place_analysis_candidate_demo_order(trade_id)
                submitted_this_cycle += 1
                lifecycle_status = submission["status"]
                execution_result = {
                    **execution_result,
                    "status": "submitted_pending_reconciliation",
                    "broker_order_submitted": True,
                    "order": submission.get("order"),
                }
                signal_result["execution_status"] = execution_result["status"]
                signal_result["execution_reasons"] = []
                conn = get_connection()
                conn.execute(
                    "UPDATE trades SET status = ?, notes = ? WHERE id = ?",
                    (lifecycle_status, "Automated eToro demo order submitted after fresh strategy, account, quote, risk, and stop checks. Broker reconciliation is pending.", trade_id),
                )
                conn.commit()
                conn.close()
            except Exception as error:
                # The order helper marks uncertain post-submit outcomes for
                # reconciliation. Pre-submit failures remain queued for a
                # future cycle and do not invalidate the analysis.
                conn = get_connection()
                current = conn.execute("SELECT status FROM trades WHERE id = ?", (trade_id,)).fetchone()
                lifecycle_status = str(current["status"]) if current else lifecycle_status
                conn.close()
                is_rejected = lifecycle_status == "REJECTED_EXECUTION"
                message = (
                    f"eToro rejected the demo order: {error}"
                    if is_rejected
                    else f"Demo execution is waiting for fresh broker data or a safe order result: {error}"
                )
                record_trade_event(trade_id, "execution", "execution", "completed" if is_rejected else "waiting",
                                   "rejected" if is_rejected else "pending", message,
                                   {"error": str(error), "broker_order_submitted": lifecycle_status == "RECONCILIATION_REQUIRED"})
                execution_result = {**execution_result, "status": "rejected" if is_rejected else "waiting", "reasons": [message]}
                signal_result["execution_status"] = execution_result["status"]
                signal_result["execution_reasons"] = [message]
                if lifecycle_status == "ANALYSIS_ONLY":
                    conn = get_connection()
                    conn.execute("UPDATE trades SET notes = ? WHERE id = ?", (message, trade_id))
                    conn.commit()
                    conn.close()
            signal_result["agent_workflow"]["execution"] = execution_result
        lifecycle_trades.append({
            "trade_id": trade_id,
            "symbol": ticker,
            "side": strategy_payload.get("position"),
            "status": lifecycle_status,
            "broker_order_submitted": bool(execution_result.get("broker_order_submitted")),
            "reason": notes if lifecycle_status == "ANALYSIS_ONLY" else execution_result.get("status"),
        })
        signals[ticker] = signal_result
        research_results.append(signal_result)
        save_signal(
            sector=str(fundamentals.get("sector") or "Unknown"),
            signal=signal,
            catalyst=str(ai_result.get("catalyst") or "news_research"),
            confidence=confidence,
            payload=signal_result,
        )

    result = {
        "mode": "eToro demo",
        "status": "analysis_complete" if research_results else "no_candidates",
        "reason": (
            "News research and agent analysis run independently of historical evaluation. Evaluation is informational only; enabled demo automation submits only after fresh broker, quote, sizing, and stop checks."
            if research_results else "No ticker-tagged news candidates were available to analyze in this cycle."
        ),
        "strategy": STRATEGY_VERSION,
        "evaluation": evaluation,
        "automation_enabled": automation_enabled,
        "account": {"equity": account.get("equity"), "cash_available": account.get("cash_available"), "fetched_at": account.get("fetched_at")},
        "broker_position_count": len(broker_snapshot.get("positions", [])),
        "news": {"fetched": len(fetched_articles), **article_counts},
        "candidate_pool_size": len(grouped),
        "ai_research_enabled": ai_enabled,
        "research_candidates": research_results,
        "signals": signals,
        "trades": lifecycle_trades,
    }
    record_agent_event("execution", "completed", "news_strategy_cycle", result["reason"], "pending", result)
    return result


@serialized_account
async def place_analysis_candidate_demo_order(trade_id: int) -> Dict[str, Any]:
    """Submit one explicitly confirmed, freshly rechecked analysis candidate to eToro demo."""
    settings = IntegrationSettings.from_environment()
    if settings.broker_provider != "etoro" or not settings.etoro_demo_trading:
        raise ValueError("Manual orders are available only for the configured eToro demo account.")

    conn = get_connection()
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (int(trade_id),)).fetchone()
    event_rows = conn.execute(
        "SELECT * FROM trade_events WHERE trade_id = ? ORDER BY id", (int(trade_id),)
    ).fetchall()
    conn.close()
    if not row or row["status"] not in {"ANALYSIS_ONLY", "DEFERRED_CAPACITY"}:
        raise ValueError("This lifecycle is no longer eligible for manual submission.")
    events = [{**dict(event), "details": json.loads(event["details"] or "{}")} for event in event_rows]
    researcher = next((event for event in events if event["stage"] == "research"), None)
    strategy_event = next((event for event in events if event["stage"] == "strategy"), None)
    if not researcher or researcher["decision"] != "accepted" or not strategy_event or strategy_event["decision"] != "accepted":
        raise ValueError("Researcher and Strategist must both have accepted this candidate.")
    candidate = strategy_event["details"].get("candidate_context")
    if not isinstance(candidate, dict):
        raise ValueError("The lifecycle does not contain the source research context needed for review.")
    symbol = str(row["symbol"]).upper()
    if symbol != str(candidate.get("ticker") or "").upper():
        raise ValueError("Stored candidate symbol does not match its lifecycle.")
    created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    max_age = float(get_agent_rules()["execution"].get("max_research_age_hours", 24))
    if (datetime.now(timezone.utc)-created).total_seconds() > max_age*3600:
        raise ValueError("This research has expired; run a fresh news analysis before submitting.")
    articles = candidate.get("related_news") or []
    if not articles:
        raise ValueError("The lifecycle has no news articles to support its research decision.")

    adapter = EtoroBrokerAdapter(settings)
    account, snapshot = await asyncio.gather(
        asyncio.to_thread(adapter.get_account_snapshot),
        asyncio.to_thread(adapter.get_positions),
    )
    if not account.get("available") or not snapshot.get("available"):
        raise ValueError(account.get("reason") or "eToro demo equity or positions could not be verified.")
    # A prior acknowledged order may now be visible; refresh its state before
    # deciding that an account dependency still exists.
    reconcile_news_demo_positions(snapshot["positions"])
    conn = get_connection()
    unresolved = [dict(r) for r in conn.execute("SELECT id,symbol,status,broker_position_id,reconciliation_status FROM trades WHERE id<>? AND status IN ('SUBMITTING','SUBMITTED_PENDING_RECONCILIATION','RECONCILIATION_REQUIRED')", (trade_id,))]
    conn.close()
    blocker = next((r for r in unresolved if reconciliation_blocks_new_order(r)), None)
    if blocker:
        raise ValueError(f"Trade #{blocker['id']} ({blocker['symbol']}) has an unresolved submission; verify its order outcome before adding account risk.")
    instrument = await asyncio.to_thread(adapter.resolve_instrument, symbol)
    if (not instrument.get("tradeable") or not instrument.get("supported_asset")
            or str(instrument.get("currency") or "").upper() != "USD"):
        raise ValueError("eToro did not confirm a tradeable USD stock or ETF.")
    if any(str(pos.get("symbol") or "").upper() == symbol
           or str(pos.get("instrument_id") or "") == str(instrument.get("instrument_id") or "")
           for pos in snapshot.get("positions", [])):
        raise ValueError("An eToro position already exists for this symbol.")
    bars, fundamentals, reaction = await asyncio.gather(
        asyncio.to_thread(load_daily_bars, symbol, "1y"),
        fetch_company_fundamentals(symbol),
        asyncio.to_thread(load_news_reaction, symbol, articles),
    )
    daily = evaluate_latest_signal(symbol, bars)
    account, snapshot = await asyncio.gather(asyncio.to_thread(adapter.get_account_snapshot), asyncio.to_thread(adapter.get_positions))
    if not account.get("available") or not snapshot.get("available"):
        raise ValueError("Fresh account equity and positions are required at submission.")
    quote = await fetch_market_quote(symbol)
    if not is_fresh_etoro_quote(quote):
        raise ValueError("A fresh eToro bid/ask quote is required before submission.")
    signal = str(candidate.get("signal"))
    trend = daily.get("trend_state")
    side = "LONG" if signal == "bullish" else "SHORT"
    if (candidate.get("ai_analysis") or {}).get("reaction_assessment") == "temporary" and trend == "bullish" and signal == "bearish":
        side = "LONG"
    elif (candidate.get("ai_analysis") or {}).get("reaction_assessment") == "temporary" and trend == "bearish" and signal == "bullish":
        side = "SHORT"
    entry = quote["ask" if side == "LONG" else "bid"]
    market_session_context = build_market_session_context(
        instrument=instrument, fundamentals=fundamentals, quote=quote,
    )
    candidate = {**candidate, "market_quote": quote, "price": entry,
                 "fundamentals": fundamentals, "news_reaction": reaction, "daily_trend_context": daily,
                 "market_session_context": market_session_context}
    strategy = StrategistAgent().analyze_and_decide({
        "tech_data": candidate, "energy_data": candidate,
        "recent_news": candidate.get("related_news") or [],
    })
    if not strategy.get("decision_gate", {}).get("accepted") or strategy.get("position") not in {"LONG", "SHORT"}:
        raise ValueError("Fresh Strategist review did not approve this demo entry.")
    side = strategy["position"]
    atr14 = float(bars.iloc[-1]["atr14"]) if bars is not None and not bars.empty else None
    risk = RiskManagerAgent().review_news_demo_candidate(
        candidate=candidate, strategy=strategy, account=account,
        broker_snapshot=snapshot, instrument=instrument, atr14=atr14,
    )
    if (risk.get("decision") != "accepted" or risk.get("execution_ready") is not True
            or risk.get("execution_reasons") or not risk.get("proposed_units")
            or not risk.get("proposed_stop") or not risk.get("proposed_take_profit")):
        raise ValueError("Fresh Risk Manager review blocked the order: " + "; ".join([*(risk.get("reasons") or []), *(risk.get("execution_reasons") or [])]))
    if not get_agent_rules()["execution"].get("enabled", True):
        raise ValueError("Execution Agent is disabled.")
    if candidate.get("test_max_spend_usd"):
        cap = min(float(candidate["test_max_spend_usd"]), float(get_agent_rules()["execution"]["max_order_spend_usd"]))
        factor = min(1, cap / float(risk["estimated_total_spend"]))
        original = risk["proposed_units"]
        risk["proposed_units"] = math.floor(original*factor*1_000_000)/1_000_000
        factor = risk["proposed_units"]/original
        for field in ("required_margin", "notional", "estimated_total_spend", "planned_stop_risk", "estimated_costs", "estimated_net_target_profit"):
            risk[field] *= factor
        risk["sizing"]["limits_units"]["test_stake"] = risk["proposed_units"]
        risk["sizing"]["binding_limit"] = "test_stake"
        if risk["proposed_units"] <= 0:
            raise ValueError("The test stake is too small for an executable order.")
    units = float(risk["proposed_units"])
    leverage = int(risk["leverage"])
    cost_evidence = {"available": False, "source": "Configured cost estimate", "estimated_costs": risk["estimated_costs"]}
    try:
        cost_payload = await asyncio.to_thread(adapter.estimate_order_costs, symbol, side, units, leverage)
        costs = cost_payload.get("costs")
        if isinstance(costs, list) and all(c.get("currency") == "USD" for c in costs):
            opening_cost = sum(max(0, float(c.get("value") or 0)) for c in costs if c.get("costType") not in {"overnightFee", "overWeekendFee"})
            max_spend = min(float(get_agent_rules()["execution"]["max_order_spend_usd"]), float(candidate.get("test_max_spend_usd") or float("inf")))
            cost_per_unit = float(risk["estimated_costs"])/units
            cash_cap = min(max_spend, float(account.get("cash_available") or 0))
            cost_capacity = max(0, (cash_cap-opening_cost)/(float(entry)/leverage+cost_per_unit))
            if units > cost_capacity:
                # A broker fixed opening fee must fit inside the spending cap.
                units = math.floor(cost_capacity*1_000_000)/1_000_000
                cost_payload = await asyncio.to_thread(adapter.estimate_order_costs, symbol, side, units, leverage)
                costs = cost_payload.get("costs", [])
                opening_cost = sum(max(0, float(c.get("value") or 0)) for c in costs if c.get("costType") not in {"overnightFee", "overWeekendFee"})
            if units <= 0 or units*(float(entry)/leverage+cost_per_unit)+opening_cost > cash_cap+.000001:
                raise ValueError("The broker's opening fees leave no order within the spending cap.")
            cost_evidence = {"available": True, "source": "eToro demo what-if costs", "response": cost_payload}
    except ValueError as error:
        # Broker/API unavailability keeps the explicit configured estimate;
        # a verified cap violation remains a rejection.
        if "spending cap" in str(error):
            raise
        cost_evidence["reason"] = str(error)
    except Exception as error:
        cost_evidence["reason"] = type(error).__name__
    original_units = float(risk["proposed_units"])
    scale = units / original_units
    for field in ("required_margin", "notional", "estimated_total_spend", "planned_stop_risk", "estimated_costs", "estimated_net_target_profit"):
        if field in risk:
            risk[field] *= scale
    risk["projected_gross_exposure"] = risk.get("gross_exposure", 0) + units * float(entry)
    risk["proposed_units"] = units
    risk["sizing"]["units"] = units
    if scale < 1:
        risk["sizing"]["binding_limit"] = "broker_opening_costs"
        risk["sizing"]["limits_units"]["broker_opening_costs"] = units
    risk["broker_cost_evidence"] = cost_evidence
    if cost_evidence.get("available"):
        risk["estimated_costs"] += opening_cost
        risk["estimated_total_spend"] += opening_cost
        risk["estimated_net_target_profit"] -= opening_cost
        if risk["estimated_net_target_profit"] <= 0:
            raise ValueError("Broker costs leave no estimated positive target payoff.")
    required_margin = units * float(entry) / leverage
    cash = account.get("cash_available")
    if cash is None or float(cash) < required_margin + risk["estimated_costs"]:
        raise ValueError("Verified demo cash is insufficient for the freshly sized order.")

    reference = str(uuid.uuid4())
    policy = {"sector": (candidate.get("fundamentals") or {}).get("sector"), "research_model": (candidate.get("ai_analysis") or {}).get("model"),
              "research_provider": (candidate.get("ai_analysis") or {}).get("provider"),
              "research_confidence": candidate.get("confidence"),
              "research_prompt_version": "news_reaction_v2",
              "test_workflow_id": candidate.get("test_workflow_id"), "framework": strategy["decision_gate"].get("strategy_framework") or "DIRECTIONAL_NEWS",
              "holding_hours": risk["holding_hours"], "sizing": risk["sizing"],
              "estimated_costs": risk["estimated_costs"], "atr14": atr14,
              "broker_cost_evidence": cost_evidence,
              "market_session_context": market_session_context,
              "requested_entry": entry, "stop_offset": risk["proposed_stop"]-entry,
              "target_offset": risk["proposed_take_profit"]-entry,
              "mean_reversion": strategy["decision_gate"].get("conditional_mean_reversion") or {}}
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE trades SET status='SUBMITTING', side=?, entry_price=?, current_price=?, quote_provider=?, quote_timestamp=?, quantity=?, leverage=?, stop_loss_price=?, take_profit_price=?, allocated_capital=?, strategy_version=?, policy_json=?, config_json=?, order_reference=? WHERE id=? AND status IN ('ANALYSIS_ONLY','DEFERRED_CAPACITY')",
        (side, float(entry), float(entry), quote["provider"], quote["fetched_at"], units, leverage,
         float(risk["proposed_stop"]), float(risk["proposed_take_profit"]), required_margin, WORKFLOW_VERSION,
         json.dumps(policy), json.dumps(get_agent_rules()), reference, int(trade_id)),
    )
    conn.commit()
    conn.close()
    if cursor.rowcount != 1:
        raise ValueError("This candidate has already been submitted or changed; refresh the lifecycle.")

    record_trade_event(trade_id, "strategy", "strategist", "completed", "accepted",
                       "Fresh manual demo-order strategy recheck passed.", {**strategy, "candidate_context": candidate})
    record_trade_event(trade_id, "risk", "risk_manager", "completed", "accepted",
                       "Fresh account, positions, quote, ATR, sizing, and stop checks passed.", risk)
    attempted_submission = False
    try:
        if not is_fresh_etoro_quote(quote):
            raise ValueError("The execution quote expired; the order was not sent.")
        attempted_submission = True
        order = await submit_broker_order({
            "symbol": symbol, "side": side, "units": units, "leverage": leverage,
            "stop_loss_price": float(risk["proposed_stop"]), "take_profit_price": float(risk["proposed_take_profit"]),
            "currency": "usd", "reference_id": reference,
        })
    except Exception as error:
        response_status = re.search(r"HTTP\s+(\d{3})", str(error), re.IGNORECASE)
        definitely_rejected = bool(response_status and int(response_status.group(1)) in {400, 401, 403, 404, 422})
        failure_status = "REJECTED_EXECUTION" if definitely_rejected else "RECONCILIATION_REQUIRED" if attempted_submission else "ANALYSIS_ONLY"
        reconciliation_status = "broker_rejected" if definitely_rejected else "submission_outcome_uncertain"
        conn = get_connection()
        conn.execute("UPDATE trades SET status = ?, reconciliation_status = ?, notes = ? WHERE id = ?",
                     (failure_status, reconciliation_status,
                      f"eToro rejected the order. {error}" if definitely_rejected else f"Order outcome requires broker reconciliation; do not resubmit blindly. {error}",
                      int(trade_id)))
        if not attempted_submission or definitely_rejected:
            conn.execute("UPDATE trades SET quantity=0,allocated_capital=0 WHERE id=?", (int(trade_id),))
        conn.commit()
        conn.close()
        record_trade_event(trade_id, "execution", "execution", "completed",
                           "rejected" if definitely_rejected else "uncertain",
                           f"eToro order rejected: {error}" if definitely_rejected else f"eToro submission outcome requires reconciliation: {error}",
                           {"api_logs": getattr(error, "api_logs", []), "symbol": symbol})
        raise
    conn = get_connection()
    conn.execute(
        "UPDATE trades SET status = 'SUBMITTED_PENDING_RECONCILIATION', broker_order_id = ?, broker_position_id = ?, reconciliation_status = 'awaiting_broker_position', stop_state = 'submitted_not_confirmed', notes = ? WHERE id = ?",
        (str(order.get("order_id") or "") or None, str(order.get("position_id") or "") or None,
         "eToro demo order submitted; waiting for broker fill and exit-level confirmation.", int(trade_id)),
    )
    conn.commit()
    conn.close()
    record_trade_event(trade_id, "execution", "execution", "completed", "submitted",
                       "eToro demo order submitted; broker reconciliation is pending.", order)
    email_result = {"status": "disabled"}
    try:
        email_result = await asyncio.to_thread(send_trade_execution_email, DB_PATH, int(trade_id))
        if email_result.get("status") == "sent":
            record_trade_event(
                trade_id, "notification", "execution", "completed", "accepted",
                "Successful demo-order lifecycle email sent to the configured recipient(s).",
                email_result,
            )
    except Exception as error:
        # Notification delivery must never change a broker-acknowledged order
        # into an execution failure or encourage a duplicate submission.
        logger.exception("Trade #%s executed but its email notification failed", trade_id)
        record_trade_event(
            trade_id, "notification", "execution", "completed", "rejected",
            f"Trade executed, but email delivery failed: {type(error).__name__}: {error}",
            {"error_type": type(error).__name__},
        )
        email_result = {"status": "failed", "error_type": type(error).__name__}
    return {
        "trade_id": int(trade_id),
        "status": "SUBMITTED_PENDING_RECONCILIATION",
        "order": order,
        "email_notification": email_result,
    }


async def _run_retired_etoro_trend_cycle(settings: Optional[IntegrationSettings] = None) -> Dict[str, Any]:
    """Keep old callers from submitting through the retired unbounded path."""
    raise ValueError("The SMA-only order path is retired; use the active news workflow.")


async def run_trading_cycle(use_openai: Optional[bool] = None) -> Dict[str, Any]:
    ai_enabled = get_ai_research_enabled() if use_openai is None else use_openai
    settings = IntegrationSettings.from_environment()
    if settings.broker_provider == "etoro":
        try:
            async with workflow_store.lease(get_connection, "news_research_cycle"):
                return await run_etoro_news_research_cycle(settings)
        except workflow_store.AccountBusy as error:
            return {"mode": "eToro demo", "status": "cycle_in_progress", "reason": str(error), "trades": []}
    agent_rules = get_agent_rules()
    researcher_rules = agent_rules.get("researcher", DEFAULT_AGENT_RULES["researcher"])
    strategist_rules = agent_rules.get("strategist", DEFAULT_AGENT_RULES["strategist"])
    risk_rules = agent_rules.get("risk_manager", DEFAULT_AGENT_RULES["risk_manager"])
    execution_rules = agent_rules.get("execution", DEFAULT_AGENT_RULES["execution"])
    trend_review_enabled = bool(risk_rules.get("openai_trend_review_enabled", True))
    if ai_enabled:
        ai_researcher = OpenAIResearchAdapter(settings, record_openai_usage)
    elif trend_review_enabled and settings.openai_api_key:
        ai_researcher = OpenAIResearchAdapter(settings, record_openai_usage)
    else:
        ai_researcher = None
    researcher = ResearcherAgent()
    strategist = StrategistAgent()
    risk_manager = RiskManagerAgent()
    execution = ExecutionAgent()

    record_agent_event("researcher", "running", "start_cycle", "Starting market research cycle.", "pending", {})
    record_agent_event("strategist", "running", "start_cycle", "Starting strategist evaluation.", "pending", {})
    record_agent_event("risk_manager", "running", "start_cycle", "Starting risk validation.", "pending", {})
    record_agent_event("execution", "running", "start_cycle", "Starting execution review.", "pending", {})

    research_limit = max(0, int(researcher_rules.get("max_fetch_per_cycle", 6)))
    market_data = await researcher.run_data_fetch(research_limit)
    recent_news = get_news_articles(limit=50)
    relevance_counts = classify_research_headlines(
        recent_news, market_data["candidates"]
    )
    recent_news = get_news_articles(limit=50)
    market_data["recent_news"] = recent_news
    market_data["headline_relevance"] = relevance_counts
    market_data["ai_research_enabled"] = ai_enabled
    candidates = market_data["candidates"]
    cycle_trades: List[Dict[str, Any]] = []
    approved_candidates: List[Dict[str, Any]] = []

    for candidate in candidates:
        fundamentals = {}
        if bool(researcher_rules.get("include_market_fundamentals", True)):
            fundamentals = await fetch_company_fundamentals(candidate["ticker"])
        market_quote = await fetch_market_quote(candidate["ticker"])
        entry_price = market_quote.get("price")
        price_source = market_quote.get("source") if entry_price is not None else None
        if entry_price is not None and fundamentals:
            fundamentals = {
                **fundamentals,
                "share_price": entry_price,
                "currency": market_quote.get("currency") or fundamentals.get("currency"),
                "quote_source": market_quote.get("source"),
                "quote_fetched_at": market_quote.get("fetched_at"),
            }
        if entry_price is None and not bool(strategist_rules.get("require_current_market_quote", True)):
            entry_price = candidate.get("price")
            price_source = "Research feed" if entry_price is not None else None
        candidate = {
            **candidate,
            "fundamentals": fundamentals,
            "market_quote": market_quote,
            "price": entry_price,
            "price_source": price_source,
        }
        related_news = [
            article for article in recent_news
            if article["relevance_status"] == "relevant"
            and candidate["ticker"] in article["relevance_tickers"]
        ]
        ai_analysis = None
        if ai_researcher:
            ai_analysis = await ai_researcher.research(
                {
                    "company_name": candidate["company_name"],
                    "ticker": candidate["ticker"],
                    "sector": candidate["sector"],
                    "dummy_signal": candidate["signal"],
                    "dummy_confidence": candidate["confidence"],
                    "background": candidate["notes"],
                    "headlines": related_news,
                    "fundamentals": candidate["fundamentals"],
                }
            )
            candidate = {
                **candidate,
                "signal": ai_analysis["signal"],
                "confidence": ai_analysis["confidence"],
                "catalyst": ai_analysis["catalyst"],
                "notes": ai_analysis["summary"],
                "ai_analysis": ai_analysis,
            }

        sector_allowed = candidate["sector"] in researcher_rules.get("allowed_sectors", ["Technology", "Energy"])
        signal_decision = "accepted" if bool(researcher_rules.get("enabled", True)) and sector_allowed and candidate["signal"] != "neutral" and candidate["confidence"] >= float(
            researcher_rules.get("min_confidence", 0.6)
        ) else "rejected"
        save_signal(
            sector=candidate["sector"],
            signal=candidate["signal"],
            catalyst=candidate["catalyst"],
            confidence=candidate["confidence"],
            payload=candidate,
        )
        cycle_timestamp = datetime.now(timezone.utc).isoformat()
        record_research_cycle_company(cycle_timestamp, candidate["ticker"], candidate["signal"], candidate["confidence"])
        
        if ai_analysis:
            record_agent_event(
                "researcher", "completed", "openai_research",
                f"OpenAI returned {candidate['signal']} at {candidate['confidence']:.0%} confidence for {candidate['ticker']}.",
                signal_decision, ai_analysis,
            )
        inferred_side = "LONG" if candidate["signal"] == "bullish" else "SHORT" if candidate["signal"] == "bearish" else "HOLD"
        entry_price = float(candidate["price"]) if candidate.get("price") is not None else None
        protection_pct = float(risk_rules.get("stop_loss_pct", 0.02))
        initial_exit_target = (
            1 + BASE_TAKE_PROFIT_PCT
            if inferred_side == "LONG"
            else 1 - BASE_TAKE_PROFIT_PCT
            if inferred_side == "SHORT"
            else 1.0
        )
        # Use default leverage from researcher rules
        default_leverage = float(researcher_rules.get("default_leverage", 2.0))
        stop_loss_price, take_profit_price, initial_exit_target = calculate_leveraged_exit_prices(
            entry_price=entry_price,
            side=inferred_side,
            leverage=default_leverage,
            stop_loss_pct=protection_pct,
            base_exit_target=initial_exit_target,
        )
        trade_id = save_trade(
            symbol=candidate["ticker"], side=inferred_side, leverage=default_leverage, exit_target=initial_exit_target,
            status="RESEARCHED", notes=candidate["notes"], entry_price=entry_price,
            company_name=candidate["company_name"], stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
        )
        price_note = (
            f"Quote {entry_price:.4f} from {candidate['price_source']}. Stop loss {stop_loss_price:.4f}; take-profit {take_profit_price:.4f}."
            if entry_price is not None
            else "No usable market quote; price-based stop-loss and take-profit were not generated."
        )
        record_trade_event(
            trade_id, "research", "researcher", "completed", signal_decision,
            f"{candidate['company_name']} ({candidate['ticker']}): {candidate['signal']} research at {candidate['confidence']:.0%} confidence. {price_note}",
            {**candidate, "related_news": related_news, "ai_analysis": ai_analysis},
        )

        trade_result: Dict[str, Any] = {
            "trade_id": trade_id,
            "symbol": candidate["ticker"],
            "status": "REJECTED_RESEARCH",
            "leverage": default_leverage,
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
        }
        if signal_decision != "accepted":
            conn = get_connection()
            conn.execute("UPDATE trades SET status = 'REJECTED_RESEARCH' WHERE id = ?", (trade_id,))
            conn.commit()
            conn.close()
            update_company_analytics(
                ticker=candidate["ticker"],
                company_name=candidate["company_name"],
                sector=candidate["sector"],
                signal=candidate["signal"],
                confidence=candidate["confidence"],
                accepted=False
            )
            cycle_trades.append(trade_result)
            continue

        candidate_market = {
            "tech_data": {**candidate, "related_news": related_news},
            "energy_data": {**candidate, "related_news": related_news},
            "recent_news": related_news,
        }
        strategy = strategist.analyze_and_decide(candidate_market)
        strategy_leverage = float(strategy["leverage"])
        if strategy_leverage > 0:
            stop_loss_price, take_profit_price, strategy["exit_target"] = calculate_leveraged_exit_prices(
                entry_price=entry_price,
                side=strategy["position"],
                leverage=strategy_leverage,
                stop_loss_pct=protection_pct,
                base_exit_target=strategy["exit_target"],
            )
        else:
            stop_loss_price = take_profit_price = None
            strategy["exit_target"] = 1.0
        price_note = (
            f"Quote {entry_price:.4f} from {candidate['price_source']}. "
            f"Stop loss {stop_loss_price:.4f}; take-profit {take_profit_price:.4f}."
            if entry_price is not None and stop_loss_price is not None and take_profit_price is not None
            else "No usable market quote or positive leverage; price-based exits were not generated."
        )
        conn = get_connection()
        conn.execute(
            "UPDATE trades SET side = ?, leverage = ?, exit_target = ?, stop_loss_price = ?, take_profit_price = ? WHERE id = ?",
            (
                strategy["position"],
                strategy_leverage,
                strategy["exit_target"],
                stop_loss_price,
                take_profit_price,
                trade_id,
            ),
        )
        conn.commit()
        conn.close()
        decision_gate = strategy["decision_gate"]
        strategy_decision = "accepted" if decision_gate["accepted"] else "rejected"
        strategy_message = (
            f"Strategist approved {strategy['position']} {candidate['company_name']} ({candidate['ticker']}) at {candidate['confidence']:.0%} confidence. {price_note}"
            if strategy_decision == "accepted"
            else f"Strategist rejected {strategy['position']} {candidate['company_name']} ({candidate['ticker']}): {' '.join(decision_gate['rejection_reasons'])} {price_note}"
        )
        record_trade_event(
            trade_id, "strategy", "strategist", "completed", strategy_decision,
            strategy_message,
            {**strategy, "stop_loss_price": stop_loss_price, "take_profit_price": take_profit_price},
        )
        trade_result["side"] = strategy["position"]
        trade_result.update({
            "leverage": strategy_leverage,
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
        })
        if strategy_decision != "accepted":
            conn = get_connection()
            conn.execute("UPDATE trades SET side = ?, status = 'REJECTED_STRATEGY' WHERE id = ?", (strategy["position"], trade_id))
            conn.commit()
            conn.close()
            update_company_analytics(
                ticker=candidate["ticker"],
                company_name=candidate["company_name"],
                sector=candidate["sector"],
                signal=candidate["signal"],
                confidence=candidate["confidence"],
                accepted=False
            )
            record_trade_event(trade_id, "risk", "risk_manager", "skipped", "pending", "Skipped because the strategist rejected this setup.")
            record_trade_event(trade_id, "execution", "execution", "skipped", "pending", "No order submitted after strategy rejection.")
            record_agent_event("risk_manager", "skipped", "risk_check", "Skipped after strategy rejection.", "pending", {})
            record_agent_event("execution", "skipped", "trade_execution", "No order submitted after strategy rejection.", "pending", {})
            trade_result["status"] = "REJECTED_STRATEGY"
            cycle_trades.append(trade_result)
            continue

        trend_review = None
        if trend_review_enabled:
            if ai_researcher is None:
                trend_review = {
                    "status": "unavailable",
                    "reason": "OpenAI API key is not configured for the risk trend review.",
                }
            else:
                market_trends = await fetch_market_trends(candidate["ticker"])
                if not market_trends.get("available"):
                    trend_review = {
                        "status": "unavailable",
                        "reason": market_trends.get("unavailable_reason", "No usable price history was returned."),
                        "market_trends": market_trends,
                    }
                else:
                    try:
                        trend_review = await ai_researcher.review_market_trends(
                            ticker=candidate["ticker"],
                            company_name=candidate["company_name"],
                            proposed_position=strategy["position"],
                            fundamentals=candidate.get("fundamentals", {}),
                            market_trends=market_trends,
                        )
                    except Exception as error:
                        logger.warning(
                            "OpenAI risk trend review failed for %s: %s",
                            candidate["ticker"], type(error).__name__,
                        )
                        trend_review = {
                            "status": "unavailable",
                            "reason": "OpenAI trend review request failed; deterministic risk checks remain active.",
                            "market_trends": market_trends,
                        }

        risk = risk_manager.check_risk(
            pnl=0.0,
            volatility=0.018,
            strategy_signal=strategy,
            trend_review=trend_review,
        )
        record_trade_event(trade_id, "risk", "risk_manager", "completed", risk["decision"], risk["details"], risk)
        trade_result["risk"] = risk
        if risk["decision"] != "accepted":
            conn = get_connection()
            conn.execute("UPDATE trades SET side = ?, status = 'REJECTED_RISK' WHERE id = ?", (strategy["position"], trade_id))
            conn.commit()
            conn.close()
            record_trade_event(trade_id, "execution", "execution", "skipped", "rejected", "Risk manager blocked execution.")
            record_agent_event("execution", "skipped", "trade_execution", "Risk manager blocked execution.", "rejected", {})
            trade_result["status"] = "REJECTED_RISK"
            cycle_trades.append(trade_result)
            continue

        if not bool(execution_rules.get("enabled", True)):
            execution_readiness_error = "Execution agent is disabled by its rule set."
        elif not bool(execution_rules.get("paper_trading_only", True)) and settings.broker_provider != "etoro":
            execution_readiness_error = "Non-eToro live execution is not implemented; trade blocked."
        elif strategy["position"] not in execution_rules.get("allowed_side", ["LONG", "SHORT"]):
            execution_readiness_error = "Execution side is disabled by the current rules."
        else:
            execution_readiness_error = None

        if execution_readiness_error:
            conn = get_connection()
            conn.execute(
                "UPDATE trades SET side = ?, status = 'REJECTED_EXECUTION', notes = ? WHERE id = ?",
                (strategy["position"], execution_readiness_error, trade_id),
            )
            conn.commit()
            conn.close()
            record_trade_event(
                trade_id, "execution", "execution", "completed", "rejected",
                execution_readiness_error, {"paper_only": True, "decision_gate": strategy["decision_gate"]},
            )
            record_agent_event(
                "execution", "completed", "trade_execution", execution_readiness_error,
                "rejected", {"trade_id": trade_id, "symbol": candidate["ticker"]},
            )
            trade_result.update({"status": "REJECTED_EXECUTION", "reason": execution_readiness_error})
            cycle_trades.append(trade_result)
            continue

        success_ranking = calculate_candidate_success_score(candidate, strategy, risk)
        approved_candidates.append({
            "trade_id": trade_id,
            "candidate": candidate,
            "strategy": strategy,
            "risk": risk,
            "trade_result": trade_result,
            "entry_price": entry_price,
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
            "price_note": price_note,
            "success_ranking": success_ranking,
        })

    ranked_candidates = sorted(
        approved_candidates,
        key=lambda item: (
            -item["success_ranking"]["score"],
            -item["candidate"]["confidence"],
            item["candidate"]["ticker"],
        ),
    )
    max_trades = max(0, int(execution_rules.get("max_trades_per_cycle", 2)))
    opened_count = 0
    for rank, item in enumerate(ranked_candidates, start=1):
        trade_id = item["trade_id"]
        candidate = item["candidate"]
        strategy = item["strategy"]
        trade_result = item["trade_result"]
        selection_score = item["success_ranking"]["score"]
        selection_details = {
            "selection_rank": rank,
            "selection_score": selection_score,
            "success_ranking": item["success_ranking"],
            "execution_limit": max_trades,
            "risk_review": item["risk"].get("trend_review"),
        }

        if opened_count >= max_trades:
            deferred_message = (
                f"Risk-approved but not opened: rank {rank} with direction-neutral score "
                f"{selection_score:.3f}; the cycle limit of {max_trades} trades was reached."
            )
            conn = get_connection()
            conn.execute(
                "UPDATE trades SET side = ?, status = 'DEFERRED_CAPACITY', notes = ? WHERE id = ?",
                (strategy["position"], deferred_message, trade_id),
            )
            conn.commit()
            conn.close()
            record_trade_event(
                trade_id, "execution", "execution", "skipped", "pending",
                deferred_message, selection_details,
            )
            record_agent_event(
                "execution", "skipped", "trade_execution", deferred_message,
                "pending", {"trade_id": trade_id, "symbol": candidate["ticker"], **selection_details},
            )
            trade_result.update({
                "status": "DEFERRED_CAPACITY",
                "selection_rank": rank,
                "selection_score": selection_score,
            })
            cycle_trades.append(trade_result)
            continue

        portfolio = get_portfolio_summary()
        allocation = min(
            float(execution_rules.get("max_trade_value", 2500)),
            portfolio["cash_available"],
            portfolio["starting_capital"] * float(strategist_rules.get("position_limit", 0.35)),
        )
        if allocation <= 0:
            execution_message = "Insufficient available cash for another paper position."
            conn = get_connection()
            conn.execute(
                "UPDATE trades SET side = ?, status = 'REJECTED_EXECUTION', notes = ? WHERE id = ?",
                (strategy["position"], execution_message, trade_id),
            )
            conn.commit()
            conn.close()
            record_trade_event(
                trade_id, "execution", "execution", "completed", "rejected",
                execution_message, selection_details,
            )
            record_agent_event(
                "execution", "completed", "trade_execution", execution_message,
                "rejected", {"trade_id": trade_id, "symbol": candidate["ticker"], **selection_details},
            )
            trade_result.update({
                "status": "REJECTED_EXECUTION",
                "reason": execution_message,
                "selection_rank": rank,
                "selection_score": selection_score,
            })
            cycle_trades.append(trade_result)
            continue

        entry_price = item["entry_price"]
        stop_loss_price = item["stop_loss_price"]
        take_profit_price = item["take_profit_price"]
        broker_order = None
        paper_only = True
        if settings.broker_provider == "etoro":
            try:
                broker_order = await submit_broker_order(
                    {
                        "symbol": candidate["ticker"],
                        "side": strategy["position"],
                        "amount": allocation,
                        "leverage": strategy["leverage"],
                        "stop_loss_price": stop_loss_price,
                        "take_profit_price": take_profit_price,
                        "currency": candidate.get("market_quote", {}).get("currency") or candidate.get("fundamentals", {}).get("currency") or "usd",
                    }
                )
                paper_only = False
                execution_message = (
                    f"eToro {broker_order['mode']} order opened with {allocation:.2f} allocated to {candidate['ticker']}."
                )
            except Exception as error:
                execution_message = f"eToro order failed for {candidate['ticker']}: {error}"
                conn = get_connection()
                conn.execute(
                    "UPDATE trades SET side = ?, status = 'REJECTED_EXECUTION', notes = ? WHERE id = ?",
                    (strategy["position"], execution_message, trade_id),
                )
                conn.commit()
                conn.close()
                record_trade_event(
                    trade_id, "execution", "execution", "completed", "rejected",
                    execution_message,
                    {
                        **selection_details,
                        "paper_only": False,
                        **({"broker_api_logs": getattr(error, "api_logs", [])} if getattr(error, "api_logs", None) else {}),
                    },
                )
                record_agent_event(
                    "execution", "completed", "trade_execution", execution_message,
                    "rejected", {"trade_id": trade_id, "symbol": candidate["ticker"], **selection_details},
                )
                trade_result.update({
                    "status": "REJECTED_EXECUTION",
                    "reason": execution_message,
                    "selection_rank": rank,
                    "selection_score": selection_score,
                })
                cycle_trades.append(trade_result)
                continue
        else:
            execution_message = f"Paper position opened with {allocation:.2f} allocated to {candidate['ticker']}."
        conn = get_connection()
        conn.execute(
            "UPDATE trades SET side = ?, leverage = ?, exit_target = ?, status = 'OPEN', allocated_capital = ?, entry_price = ?, current_price = ?, quantity = ?, take_profit_price = ?, stop_loss_price = ?, notes = ? WHERE id = ?",
            (strategy["position"], strategy["leverage"], strategy["exit_target"], allocation, entry_price, entry_price, allocation * strategy["leverage"] / entry_price, take_profit_price, stop_loss_price, execution_message, trade_id),
        )
        conn.commit()
        conn.close()
        update_company_analytics(
            ticker=candidate["ticker"],
            company_name=candidate["company_name"],
            sector=candidate["sector"],
            signal=candidate["signal"],
            confidence=candidate["confidence"],
            accepted=True
        )
        record_trade_event(
            trade_id, "execution", "execution", "completed", "accepted",
            f"{execution_message} Stop loss {stop_loss_price:.4f}; take-profit {take_profit_price:.4f}.",
            {
                "allocated_capital": allocation,
                "paper_only": paper_only,
                "broker_order": broker_order,
                "stop_loss_price": stop_loss_price,
                "take_profit_price": take_profit_price,
                "side": strategy["position"],
                "leverage": strategy["leverage"],
                "research_input": strategy.get("research_input"),
                "news_context": strategy.get("news_context", []),
                "ai_analysis": strategy.get("ai_analysis"),
                **selection_details,
            },
        )
        record_agent_event(
            "execution", "completed", "trade_execution", execution_message,
            "accepted", {"trade_id": trade_id, "symbol": candidate["ticker"], **selection_details},
        )
        trade_result.update({
            "status": "OPEN",
            "allocated_capital": allocation,
            "selection_rank": rank,
            "selection_score": selection_score,
            "broker_order": broker_order,
        })
        cycle_trades.append(trade_result)
        opened_count += 1

    record_agent_event(
        "execution", "completed", "cycle_trade_selection",
        f"Opened {opened_count} of {len(approved_candidates)} risk-approved candidates; limit {max_trades}.",
        "accepted" if opened_count else "rejected",
        {
            "approved_candidate_count": len(approved_candidates),
            "opened_count": opened_count,
            "max_trades_per_cycle": max_trades,
            "ranking_method": approved_candidates[0]["success_ranking"]["method"] if approved_candidates else "No candidates passed all gates.",
        },
    )

    return {
        "market_data": market_data,
        "trades": cycle_trades,
        "agent_summary": get_agent_summary(),
        "ai_research_enabled": ai_enabled,
        "research_candidate_count": len(candidates),
        "risk_approved_candidate_count": len(approved_candidates),
        "max_trades_per_cycle": max_trades,
    }


async def maintenance_loop() -> None:
    last_cleanup = float("-inf")
    while True:
        try:
            if time.monotonic()-last_cleanup >= 3600:
                async with workflow_store.lease(get_connection, "analysis_cleanup"):
                    cleanup_stale_analysis()
                last_cleanup = time.monotonic()
            if IntegrationSettings.from_environment().broker_provider == "etoro":
                await manage_etoro_demo_positions()
        except workflow_store.AccountBusy:
            logger.info("Account maintenance will retry after the current operation.")
        except Exception:
            logger.exception("Maintenance failed; retrying next minute")
        await asyncio.sleep(60)


async def heartbeat_loop() -> None:
    while True:
        conn = get_connection()
        try:
            conn.execute("INSERT INTO app_settings(key,value) VALUES ('worker_heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (datetime.now(timezone.utc).isoformat(),))
            conn.commit()
        finally:
            conn.close()
        await asyncio.sleep(10)


async def main() -> None:
    ensure_db()
    settings = IntegrationSettings.from_environment()
    maintenance = asyncio.create_task(maintenance_loop())
    heartbeat = asyncio.create_task(heartbeat_loop())
    last_news_fetch = 0.0
    try:
        while True:
            if heartbeat.done() or maintenance.done():
                raise RuntimeError("A required worker task exited; supervisor must restart the worker.")
            now = time.monotonic()
            if now-last_news_fetch >= settings.news_interval_seconds:
                last_news_fetch = now
                try:
                    await run_hourly_news_fetch()
                except Exception:
                    logger.exception("Scheduled news fetch failed")
            if claim_due_research_cycle():
                logger.info("Scheduled research cycle starting")
                try:
                    await run_trading_cycle()
                except Exception:
                    logger.exception("Scheduled research cycle failed")
            await asyncio.sleep(min(10, settings.news_interval_seconds))
    finally:
        maintenance.cancel()
        heartbeat.cancel()
        await asyncio.gather(maintenance, heartbeat, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
