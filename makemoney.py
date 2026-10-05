import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Load environment variables from .env.config if it exists
env_config_path = Path(__file__).resolve().parent / ".env.config"
if env_config_path.exists():
    with open(env_config_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key] = value

from integrations import (
    IntegrationSettings,
    OpenAIResearchAdapter,
    YahooFinanceNewsProvider,
    fetch_company_fundamentals,
    fetch_market_quote,
    fetch_market_trends,
    validate_trade_price_path,
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
    "researcher": {"enabled": True, "status": "idle", "min_confidence": 0.6, "allowed_sectors": ["Technology", "Energy"], "max_fetch_per_cycle": 6, "include_market_fundamentals": True},
    "strategist": {
        "enabled": True,
        "status": "idle",
        "min_confidence": 0.7,
        "fundamental_confirmation_min_confidence": 0.6,
        "countertrend_short_min_confidence": 0.85,
        "countertrend_long_min_confidence": 0.85,
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
        "default_leverage": 2.0,
        "max_leverage": 3.0,
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
    },
    "execution": {"enabled": True, "status": "idle", "paper_trading_only": True, "max_trade_value": 2500, "max_trades_per_cycle": 2, "allowed_side": ["LONG", "SHORT"]},
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


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
    conn = get_connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, avatar TEXT NOT NULL DEFAULT 'account_circle', is_admin INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, sector TEXT NOT NULL, signal TEXT NOT NULL, catalyst TEXT NOT NULL, confidence REAL NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS news_articles (id INTEGER PRIMARY KEY AUTOINCREMENT, article_key TEXT NOT NULL UNIQUE, ticker TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', published_at TEXT, fetched_at TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', relevance_status TEXT NOT NULL DEFAULT 'unreviewed', relevance_tickers TEXT NOT NULL DEFAULT '', relevance_reason TEXT NOT NULL DEFAULT '', relevance_analyzed_at TEXT);
        CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, symbol TEXT NOT NULL, side TEXT NOT NULL, leverage REAL NOT NULL, exit_target REAL NOT NULL, status TEXT NOT NULL, notes TEXT, allocated_capital REAL NOT NULL DEFAULT 0, entry_price REAL, current_price REAL, quantity REAL NOT NULL DEFAULT 0, realized_pnl REAL NOT NULL DEFAULT 0, closed_at TEXT, company_name TEXT NOT NULL DEFAULT '', stop_loss_price REAL, take_profit_price REAL);
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
        """
    )

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
    user_count = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    if user_count == 0:
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)",
            (os.getenv("FINBOT_USERNAME", "admin"), hash_password(os.getenv("FINBOT_PASSWORD", "admin123"))),
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
            if merged_rules != saved_rules:
                conn.execute(
                    "UPDATE agent_rules SET rules_json = ?, updated_at = CURRENT_TIMESTAMP WHERE agent_name = ?",
                    (json.dumps(merged_rules, sort_keys=True), agent_name),
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


def get_agent_rules() -> Dict[str, Dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute("SELECT agent_name, rules_json FROM agent_rules").fetchall()
    conn.close()

    data: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        data[row["agent_name"]] = json.loads(row["rules_json"])
    return data


def get_ai_research_enabled() -> bool:
    conn = get_connection()
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'ai_research_enabled'").fetchone()
    conn.close()
    return bool(row and row["value"].lower() == "true")


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


def claim_due_research_cycle(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    conn = get_connection()
    settings = {row["key"]: row["value"] for row in conn.execute(
        "SELECT key, value FROM app_settings WHERE key LIKE 'research_schedule_%'"
    )}
    if settings.get("research_schedule_mode", "manual") != "automatic":
        conn.close()
        return False
    suspended_until = settings.get("research_schedule_suspended_until", "")
    if suspended_until and datetime.fromisoformat(suspended_until) > now:
        conn.close()
        return False
    last_run = settings.get("research_schedule_last_run", "")
    due_at = datetime.fromisoformat(last_run) + timedelta(
        minutes=int(settings.get("research_schedule_interval_minutes", "60"))
    ) if last_run else now
    if due_at > now:
        conn.close()
        return False
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('research_schedule_last_run', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (now.isoformat(),),
    )
    conn.commit()
    conn.close()
    return True


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
        """SELECT id, created_at, side, status, entry_price, current_price, realized_pnl, allocated_capital, leverage
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
    rules_map = {
        row["agent_name"]: json.loads(row["rules_json"])
        for row in conn.execute("SELECT agent_name, rules_json FROM agent_rules").fetchall()
    }

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
        SELECT agent_name, status, action, message, decision, details
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
    if as_of:
        open_clause = "created_at <= ? AND (status = 'OPEN' OR (status = 'CLOSED' AND closed_at > ?))"
        position_params = (as_of, as_of)
        realized_clause = "status = 'CLOSED' AND closed_at <= ?"
        realized_params = (as_of,)
    else:
        open_clause = "status = 'OPEN'"
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


def get_trade_history(start_at: Optional[str] = None, end_at: Optional[str] = None) -> List[Dict[str, Any]]:
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
    trades = conn.execute(f"SELECT * FROM trades {where_clause} ORDER BY id DESC", parameters).fetchall()
    result = []
    for trade in trades:
        item = dict(trade)
        item["company_name"] = item.get("company_name") or INSTRUMENT_NAMES.get(item["symbol"], item["symbol"])
        event_clause = " AND created_at <= ?" if end_at else ""
        item["events"] = [
            {**dict(event), "details": json.loads(event["details"])}
            for event in conn.execute(
                f"SELECT * FROM trade_events WHERE trade_id = ?{event_clause} ORDER BY id",
                (trade["id"], end_at) if end_at else (trade["id"],),
            )
        ]
        result.append(item)
    conn.close()
    return result


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
    min_age_days: float = 4,
    max_age_days: float = 18,
    max_validation_days: int = 14,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    low_age = max(0.0, float(min_age_days))
    high_age = min(18.0, float(max_age_days))
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
        if low_age < age_days < high_age:
            eligible_ids.append(int(trade["id"]))

    results = []
    for trade_id in eligible_ids:
        result = await validate_trade_outcome(trade_id, max_days=max_validation_days)
        results.append({"trade_id": trade_id, **result})
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
    trade = conn.execute("SELECT * FROM trades WHERE id = ? AND status = 'OPEN'", (trade_id,)).fetchone()
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
        
        # Classify newly fetched headlines against current research candidates
        researcher = ResearcherAgent()
        market_data = await researcher.run_data_fetch(max_candidates=100)
        candidates = market_data.get("candidates", [])
        if candidates:
            recent_news = get_news_articles(limit=200)
            classification_results = classify_research_headlines(recent_news, candidates)
            logger.info("Headline classification after fetch: %d relevant, %d not relevant", 
                       classification_results.get("relevant", 0), 
                       classification_results.get("not_relevant", 0))
        
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
) -> int:
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO trades (symbol, side, leverage, exit_target, status, notes, allocated_capital, entry_price, current_price, quantity, company_name, stop_loss_price, take_profit_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, side, leverage, exit_target, status, notes, allocated_capital, entry_price, current_price, quantity, company_name, stop_loss_price, take_profit_price),
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


def get_dashboard_summary(start_at: Optional[str] = None, end_at: Optional[str] = None) -> Dict[str, Any]:
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
    latest_signal = conn.execute(
        f"SELECT sector, signal, catalyst, confidence, payload, created_at FROM signals {where_clause} ORDER BY id DESC LIMIT 1",
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

    return {
        "latest_signal": dict(latest_signal) if latest_signal else None,
        "latest_trade": dict(latest_trade) if latest_trade else None,
        "risk_events": [dict(row) for row in risk_events],
        "agents": agent_summary,
        "decision_timeline": get_decision_timeline(start_at, end_at),
        "portfolio": get_portfolio_summary(end_at),
        "trades": get_trade_history(start_at, end_at),
        "news": get_news_articles(start_at, end_at),
    }


class ResearcherAgent:
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
        default_leverage = float(rules.get("default_leverage", 2.0))
        max_leverage = float(rules.get("max_leverage", 3.0))
        leverage = min(default_leverage, max_leverage)
        exit_target = 1.05 if position == "LONG" else 0.95 if position == "SHORT" else 1.0
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
            "market_quote": tech_data.get("market_quote", {}),
            "decision_gate": decision_gate,
            "notes": f"Received {len(recent_news)} headlines; decision applied confidence, quote, and fundamentals checks.",
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
    def place_trade(self, signal: Dict[str, Any], pnl: float = 0.0, volatility: float = 0.01) -> Dict[str, Any]:
        rules = get_agent_rules().get("execution", DEFAULT_AGENT_RULES["execution"])
        logger.info("Placing paper trade based on strategy signal.")
        symbol = signal["assets"][0]
        side = signal["position"]
        leverage = float(signal.get("leverage", 1.0))
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
    if price is None or float(price) <= 0:
        rejection_reasons.append("No usable market price is available.")
    if bool(rules.get("require_current_market_quote", True)) and not quote_available:
        rejection_reasons.append("A current Yahoo Finance quote is required but unavailable.")

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
    for article in related_news:
        text = f"{article.get('title', '')} {article.get('summary', '')}"
        company_mentioned = (
            (ticker and re.search(rf"\b{re.escape(ticker)}\b", text, re.IGNORECASE))
            or (company_alias and company_alias in text.casefold())
            or (short_alias and re.search(rf"\b{re.escape(short_alias)}\b", text, re.IGNORECASE))
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

    if countertrend:
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
        self.audit_log_path = Path(__file__).resolve().parent / "audit_logs" / f"oversight_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jsonl"
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
        
        # Check leverage configuration
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
        
        # Check stop loss is proportional to leverage
        if trade["entry_price"] and trade["stop_loss_price"]:
            entry = float(trade["entry_price"])
            sl = float(trade["stop_loss_price"])
            sl_pct = abs(entry - sl) / entry if entry != 0 else 0
            risk_factor = float(risk_rules.get("stop_loss_pct", 0.02))
            # With higher leverage, stop loss should be tighter (proportionally smaller %)
            expected_sl_pct = risk_factor / (actual_leverage if actual_leverage > 0 else 1.0)
            
            checks["configuration_checks"]["stop_loss"] = {
                "actual_pct": round(sl_pct, 4),
                "expected_pct": round(expected_sl_pct, 4),
                "leverage_factor": actual_leverage,
                "valid": abs(sl_pct - expected_sl_pct) / expected_sl_pct < 0.1 if expected_sl_pct > 0 else True
            }
            
            if not checks["configuration_checks"]["stop_loss"]["valid"]:
                checks["issues"].append(
                    f"Stop loss not proportional to leverage: {sl_pct*100:.2f}% vs expected {expected_sl_pct*100:.2f}%"
                )
        
        # Get strategist and research decisions from events
        strategist_decision = None
        researcher_decision = None
        for event in events:
            if event["stage"] == "strategist":
                strategist_decision = json.loads(event["details"]) if event["details"] and event["details"] != '{}' else {}
            elif event["stage"] == "researcher":
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
            "SELECT id FROM trades WHERE status IN ('OPEN', 'CLOSED') ORDER BY created_at DESC LIMIT 20"
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


async def run_trading_cycle(use_openai: Optional[bool] = None) -> Dict[str, Any]:
    ai_enabled = get_ai_research_enabled() if use_openai is None else use_openai
    settings = IntegrationSettings.from_environment()
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
        protection_pct = float(get_agent_rules()["risk_manager"].get("stop_loss_pct", 0.02))
        initial_exit_target = 1.05 if inferred_side == "LONG" else 0.95 if inferred_side == "SHORT" else 1.0
        # Use default leverage from researcher rules
        default_leverage = float(researcher_rules.get("default_leverage", 2.0))
        # Stop loss is proportional to leverage: higher leverage requires tighter stop loss
        leverage_adjusted_protection = protection_pct / (default_leverage if default_leverage > 0 else 1.0)
        stop_loss_price = (
            entry_price * (1 - leverage_adjusted_protection if inferred_side == "LONG" else 1 + leverage_adjusted_protection if inferred_side == "SHORT" else 1.0)
            if entry_price is not None else None
        )
        take_profit_price = entry_price * initial_exit_target if entry_price is not None else None
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

        trade_result: Dict[str, Any] = {"trade_id": trade_id, "symbol": candidate["ticker"], "status": "REJECTED_RESEARCH"}
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
        elif not bool(execution_rules.get("paper_trading_only", True)):
            execution_readiness_error = "Non-paper execution is not implemented; trade blocked."
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

        execution_message = f"Paper position opened with {allocation:.2f} allocated to {candidate['ticker']}."
        entry_price = item["entry_price"]
        # Calculate proportional stop loss based on final leverage
        final_leverage = strategy["leverage"]
        protection_pct = float(get_agent_rules()["risk_manager"].get("stop_loss_pct", 0.02))
        leverage_adjusted_protection = protection_pct / (final_leverage if final_leverage > 0 else 1.0)
        side = strategy["position"]
        stop_loss_price = (
            entry_price * (1 - leverage_adjusted_protection if side == "LONG" else 1 + leverage_adjusted_protection if side == "SHORT" else 1.0)
            if entry_price is not None else None
        )
        conn = get_connection()
        conn.execute(
            "UPDATE trades SET side = ?, leverage = ?, exit_target = ?, status = 'OPEN', allocated_capital = ?, entry_price = ?, current_price = ?, quantity = ?, take_profit_price = ?, stop_loss_price = ?, notes = ? WHERE id = ?",
            (strategy["position"], strategy["leverage"], strategy["exit_target"], allocation, entry_price, entry_price, allocation * strategy["leverage"] / entry_price, entry_price * strategy["exit_target"], stop_loss_price, execution_message, trade_id),
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
            f"{execution_message} Stop loss {stop_loss_price:.4f}; take-profit {entry_price * strategy['exit_target']:.4f}.",
            {
                "allocated_capital": allocation,
                "paper_only": True,
                "stop_loss_price": item["stop_loss_price"],
                "take_profit_price": entry_price * strategy["exit_target"],
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


async def main() -> None:
    ensure_db()
    settings = IntegrationSettings.from_environment()
    logger.info("Starting hourly news polling every %d seconds", settings.news_interval_seconds)
    last_news_fetch = 0.0
    while True:
        now = time.monotonic()
        if now - last_news_fetch >= settings.news_interval_seconds:
            await run_hourly_news_fetch()
            last_news_fetch = time.monotonic()
        if claim_due_research_cycle():
            logger.info("Scheduled research cycle starting")
            try:
                await run_trading_cycle()
            except Exception:
                logger.exception("Scheduled research cycle failed")
        await asyncio.sleep(min(10, settings.news_interval_seconds))


if __name__ == "__main__":
    ensure_db()
    asyncio.run(main())