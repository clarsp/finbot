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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from integrations import IntegrationSettings, OpenAIResearchAdapter, YahooFinanceNewsProvider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent / "finbot.db"
DATASET_PATH = Path(__file__).resolve().parent / "research_feed.json"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60

DEFAULT_AGENT_RULES: Dict[str, Dict[str, Any]] = {
    "researcher": {"enabled": True, "status": "idle", "min_confidence": 0.6, "allowed_sectors": ["Technology", "Energy"], "max_fetch_per_cycle": 2},
    "strategist": {"enabled": True, "status": "idle", "min_confidence": 0.7, "max_leverage": 3.0, "position_limit": 0.35},
    "risk_manager": {"enabled": True, "status": "idle", "max_drawdown": 0.05, "max_volatility": 0.03, "exit_on_risk_breach": True, "stop_loss_pct": 0.02},
    "execution": {"enabled": True, "status": "idle", "paper_trading_only": True, "max_trade_value": 2500, "allowed_side": ["LONG", "SHORT"]},
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
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, sector TEXT NOT NULL, signal TEXT NOT NULL, catalyst TEXT NOT NULL, confidence REAL NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS news_articles (id INTEGER PRIMARY KEY AUTOINCREMENT, article_key TEXT NOT NULL UNIQUE, ticker TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', published_at TEXT, fetched_at TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', relevance_status TEXT NOT NULL DEFAULT 'unreviewed', relevance_tickers TEXT NOT NULL DEFAULT '', relevance_reason TEXT NOT NULL DEFAULT '', relevance_analyzed_at TEXT);
        CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, symbol TEXT NOT NULL, side TEXT NOT NULL, leverage REAL NOT NULL, exit_target REAL NOT NULL, status TEXT NOT NULL, notes TEXT, allocated_capital REAL NOT NULL DEFAULT 0, entry_price REAL, current_price REAL, quantity REAL NOT NULL DEFAULT 0, realized_pnl REAL NOT NULL DEFAULT 0, closed_at TEXT, company_name TEXT NOT NULL DEFAULT '', stop_loss_price REAL, take_profit_price REAL);
        CREATE TABLE IF NOT EXISTS trade_events (id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER NOT NULL REFERENCES trades(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, stage TEXT NOT NULL, agent_name TEXT NOT NULL, status TEXT NOT NULL, decision TEXT NOT NULL, message TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS risk_events (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, pnl REAL NOT NULL, volatility REAL NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT NOT NULL UNIQUE, rules_json TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS agent_events (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, agent_name TEXT NOT NULL, status TEXT NOT NULL, action TEXT NOT NULL, message TEXT NOT NULL, decision TEXT NOT NULL DEFAULT 'pending', details TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS auth_sessions (token_id TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
        """
    )

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
    user_count = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    if user_count == 0:
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (os.getenv("FINBOT_USERNAME", "admin"), hash_password(os.getenv("FINBOT_PASSWORD", "admin123"))),
        )
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


def add_user(username: str, password: str) -> bool:
    if not username or not password:
        return False

    conn = get_connection()
    try:
        existing = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            return False

        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, hash_password(password)),
        )
        conn.commit()
        return True
    finally:
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
            "price": float(selected.get("price", 100.0)),
            "notes": selected.get("notes", "No notes supplied."),
            "timestamp": selected.get("timestamp", "2026-10-03T00:00:00Z"),
        }

    async def poll_technology(self) -> Dict[str, Any]:
        rules = get_agent_rules().get("researcher", DEFAULT_AGENT_RULES["researcher"])
        logger.info("Polling technology market signals...")
        await asyncio.sleep(0.2)
        payload = self._next_payload_for_sector("Technology")
        decision = "accepted" if payload["confidence"] >= float(rules.get("min_confidence", 0.6)) else "rejected"
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
        decision = "accepted" if payload["confidence"] >= float(rules.get("min_confidence", 0.6)) else "rejected"
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

    async def run_data_fetch(self) -> Dict[str, Any]:
        results = await asyncio.gather(self.poll_technology(), self.poll_energy())
        return {"tech_data": results[0], "energy_data": results[1]}


class StrategistAgent:
    def analyze_and_decide(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        rules = get_agent_rules().get("strategist", DEFAULT_AGENT_RULES["strategist"])
        logger.info("Analyzing market data and forming strategy...")
        tech_data = market_data.get("tech_data", {})
        energy_data = market_data.get("energy_data", {})

        signal = tech_data.get("signal")
        position = "LONG" if signal == "bullish" else "SHORT" if signal == "bearish" else "HOLD"
        leverage = 2.0
        exit_target = 1.05 if position == "LONG" else 0.95 if position == "SHORT" else 1.0
        decision = "accepted" if position in {"LONG", "SHORT"} and float(tech_data.get("confidence", 0.0)) >= float(rules.get("min_confidence", 0.7)) else "rejected"
        recent_news = market_data.get("recent_news", [])

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
            "notes": f"Received {len(recent_news)} headlines; decision uses confidence and directional rules.",
        }
        record_agent_event(
            "strategist",
            "completed",
            "build_strategy",
            f"Strategy selected {position} with leverage {leverage}; received {len(recent_news)} headlines as context. Decision uses deterministic rules.",
            decision,
            payload,
        )
        return payload


class RiskManagerAgent:
    def check_risk(self, pnl: float, volatility: float, strategy_signal: Dict[str, Any]) -> Dict[str, Any]:
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
        details = f"{details} Received {len(news_context)} research headlines; risk remains threshold-rule based."

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


async def run_trading_cycle(use_openai: Optional[bool] = None) -> Dict[str, Any]:
    ai_enabled = get_ai_research_enabled() if use_openai is None else use_openai
    ai_researcher = (
        OpenAIResearchAdapter(IntegrationSettings.from_environment())
        if ai_enabled else None
    )
    researcher = ResearcherAgent()
    strategist = StrategistAgent()
    risk_manager = RiskManagerAgent()
    execution = ExecutionAgent()

    record_agent_event("researcher", "running", "start_cycle", "Starting market research cycle.", "pending", {})
    record_agent_event("strategist", "running", "start_cycle", "Starting strategist evaluation.", "pending", {})
    record_agent_event("risk_manager", "running", "start_cycle", "Starting risk validation.", "pending", {})
    record_agent_event("execution", "running", "start_cycle", "Starting execution review.", "pending", {})

    market_data = await researcher.run_data_fetch()
    recent_news = get_news_articles(limit=50)
    relevance_counts = classify_research_headlines(
        recent_news, [market_data["tech_data"], market_data["energy_data"]]
    )
    recent_news = get_news_articles(limit=50)
    market_data["recent_news"] = recent_news
    market_data["headline_relevance"] = relevance_counts
    market_data["ai_research_enabled"] = ai_enabled
    candidates = [market_data["tech_data"], market_data["energy_data"]]
    cycle_trades: List[Dict[str, Any]] = []

    for candidate in candidates:
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

        signal_decision = "accepted" if candidate["signal"] != "neutral" and candidate["confidence"] >= float(
            get_agent_rules()["researcher"].get("min_confidence", 0.6)
        ) else "rejected"
        if ai_analysis:
            record_agent_event(
                "researcher", "completed", "openai_research",
                f"OpenAI returned {candidate['signal']} at {candidate['confidence']:.0%} confidence for {candidate['ticker']}.",
                signal_decision, ai_analysis,
            )
        inferred_side = "LONG" if candidate["signal"] == "bullish" else "SHORT" if candidate["signal"] == "bearish" else "HOLD"
        entry_price = float(candidate["price"])
        protection_pct = float(get_agent_rules()["risk_manager"].get("stop_loss_pct", 0.02))
        initial_exit_target = 1.05 if inferred_side == "LONG" else 0.95 if inferred_side == "SHORT" else 1.0
        stop_loss_price = entry_price * (1 - protection_pct if inferred_side == "LONG" else 1 + protection_pct if inferred_side == "SHORT" else 1.0)
        take_profit_price = entry_price * initial_exit_target
        trade_id = save_trade(
            symbol=candidate["ticker"], side=inferred_side, leverage=1.0, exit_target=initial_exit_target,
            status="RESEARCHED", notes=candidate["notes"], entry_price=entry_price,
            company_name=candidate["company_name"], stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
        )
        record_trade_event(
            trade_id, "research", "researcher", "completed", signal_decision,
            f"{candidate['company_name']} ({candidate['ticker']}): {candidate['signal']} research at {candidate['confidence']:.0%} confidence. Initial stop loss {stop_loss_price:.4f}; take-profit {take_profit_price:.4f}.",
            {**candidate, "related_news": related_news, "ai_analysis": ai_analysis},
        )

        trade_result: Dict[str, Any] = {"trade_id": trade_id, "symbol": candidate["ticker"], "status": "REJECTED_RESEARCH"}
        if signal_decision != "accepted":
            conn = get_connection()
            conn.execute("UPDATE trades SET status = 'REJECTED_RESEARCH' WHERE id = ?", (trade_id,))
            conn.commit()
            conn.close()
            cycle_trades.append(trade_result)
            continue

        candidate_market = {
            "tech_data": {**candidate, "related_news": related_news},
            "energy_data": {**candidate, "related_news": related_news},
            "recent_news": related_news,
        }
        strategy = strategist.analyze_and_decide(candidate_market)
        strategy_decision = "accepted" if candidate["confidence"] >= float(
            get_agent_rules()["strategist"].get("min_confidence", 0.7)
        ) and strategy["position"] in {"LONG", "SHORT"} else "rejected"
        record_trade_event(
            trade_id, "strategy", "strategist", "completed", strategy_decision,
            f"Strategy recommends {strategy['position']} {candidate['company_name']} ({candidate['ticker']}) at {strategy['leverage']}x. Stop loss {stop_loss_price:.4f}; take-profit {take_profit_price:.4f}.",
            {**strategy, "stop_loss_price": stop_loss_price, "take_profit_price": take_profit_price},
        )
        trade_result["side"] = strategy["position"]
        if strategy_decision != "accepted":
            conn = get_connection()
            conn.execute("UPDATE trades SET side = ?, status = 'REJECTED_STRATEGY' WHERE id = ?", (strategy["position"], trade_id))
            conn.commit()
            conn.close()
            record_trade_event(trade_id, "risk", "risk_manager", "skipped", "pending", "Skipped because the strategist rejected this setup.")
            record_trade_event(trade_id, "execution", "execution", "skipped", "pending", "No order submitted after strategy rejection.")
            record_agent_event("risk_manager", "skipped", "risk_check", "Skipped after strategy rejection.", "pending", {})
            record_agent_event("execution", "skipped", "trade_execution", "No order submitted after strategy rejection.", "pending", {})
            trade_result["status"] = "REJECTED_STRATEGY"
            cycle_trades.append(trade_result)
            continue

        risk = risk_manager.check_risk(pnl=0.0, volatility=0.018, strategy_signal=strategy)
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

        rules = get_agent_rules().get("execution", DEFAULT_AGENT_RULES["execution"])
        portfolio = get_portfolio_summary()
        allocation = min(
            float(rules.get("max_trade_value", 2500)),
            portfolio["cash_available"],
            portfolio["starting_capital"] * float(get_agent_rules()["strategist"].get("position_limit", 0.35)),
        )
        execution_decision = "accepted"
        if strategy["position"] not in rules.get("allowed_side", ["LONG", "SHORT"]):
            execution_decision = "rejected"
            execution_message = "Execution side is disabled by the current rules."
        elif allocation <= 0:
            execution_decision = "rejected"
            execution_message = "Insufficient available cash for another paper position."
        else:
            execution_message = f"Paper position opened with {allocation:.2f} allocated to {candidate['ticker']}."

        if execution_decision == "accepted":
            conn = get_connection()
            conn.execute(
                "UPDATE trades SET side = ?, leverage = ?, exit_target = ?, status = 'OPEN', allocated_capital = ?, entry_price = ?, current_price = ?, quantity = ?, take_profit_price = ?, notes = ? WHERE id = ?",
                (strategy["position"], strategy["leverage"], strategy["exit_target"], allocation, entry_price, entry_price, allocation * strategy["leverage"] / entry_price, entry_price * strategy["exit_target"], execution_message, trade_id),
            )
            conn.commit()
            conn.close()
        else:
            conn = get_connection()
            conn.execute("UPDATE trades SET side = ?, status = 'REJECTED_EXECUTION', notes = ? WHERE id = ?", (strategy["position"], execution_message, trade_id))
            conn.commit()
            conn.close()
        record_trade_event(
            trade_id, "execution", "execution", "completed", execution_decision,
            f"{execution_message} Stop loss {stop_loss_price:.4f}; take-profit {entry_price * strategy['exit_target']:.4f}.",
            {
                "allocated_capital": allocation if execution_decision == "accepted" else 0,
                "paper_only": True,
                "stop_loss_price": stop_loss_price,
                "take_profit_price": entry_price * strategy["exit_target"],
                "side": strategy["position"],
                "leverage": strategy["leverage"],
                "research_input": strategy.get("research_input"),
                "news_context": strategy.get("news_context", []),
                "ai_analysis": strategy.get("ai_analysis"),
            },
        )
        record_agent_event("execution", "completed", "trade_execution", execution_message, execution_decision, {"trade_id": trade_id, "symbol": candidate["ticker"]})
        trade_result.update({"status": "OPEN" if execution_decision == "accepted" else "REJECTED_EXECUTION", "allocated_capital": allocation if execution_decision == "accepted" else 0})
        cycle_trades.append(trade_result)

    return {
        "market_data": market_data,
        "trades": cycle_trades,
        "agent_summary": get_agent_summary(),
        "ai_research_enabled": ai_enabled,
    }


async def main() -> None:
    ensure_db()
    settings = IntegrationSettings.from_environment()
    logger.info("Starting hourly news polling every %d seconds", settings.news_interval_seconds)
    while True:
        started = time.monotonic()
        await run_hourly_news_fetch()
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1, settings.news_interval_seconds - elapsed))


if __name__ == "__main__":
    ensure_db()
    asyncio.run(main())