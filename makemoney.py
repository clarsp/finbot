import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent / "finbot.db"
DATASET_PATH = Path(__file__).resolve().parent / "research_feed.json"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60

DEFAULT_AGENT_RULES: Dict[str, Dict[str, Any]] = {
    "researcher": {"enabled": True, "status": "idle", "min_confidence": 0.6, "allowed_sectors": ["Technology", "Energy"], "max_fetch_per_cycle": 2},
    "strategist": {"enabled": True, "status": "idle", "min_confidence": 0.7, "max_leverage": 3.0, "position_limit": 0.35},
    "risk_manager": {"enabled": True, "status": "idle", "max_drawdown": 0.05, "max_volatility": 0.03, "exit_on_risk_breach": True},
    "execution": {"enabled": True, "status": "idle", "paper_trading_only": True, "max_trade_value": 2500, "allowed_side": ["LONG", "SHORT"]},
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
        CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, symbol TEXT NOT NULL, side TEXT NOT NULL, leverage REAL NOT NULL, exit_target REAL NOT NULL, status TEXT NOT NULL, notes TEXT, allocated_capital REAL NOT NULL DEFAULT 0, entry_price REAL, current_price REAL, quantity REAL NOT NULL DEFAULT 0, realized_pnl REAL NOT NULL DEFAULT 0, closed_at TEXT);
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
    }.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {column} {definition}")

    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('starting_capital', '10000')")
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('auth_signing_key', ?)", (secrets.token_urlsafe(48),))
    user_count = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    if user_count == 0:
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (os.getenv("FINBOT_USERNAME", "admin"), hash_password(os.getenv("FINBOT_PASSWORD", "admin123"))),
        )
    for agent_name, rules in DEFAULT_AGENT_RULES.items():
        conn.execute(
            "INSERT OR IGNORE INTO agent_rules (agent_name, rules_json) VALUES (?, ?)",
            (agent_name, json.dumps(rules, sort_keys=True)),
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


def get_decision_timeline() -> List[Dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT created_at, agent_name, decision FROM agent_events ORDER BY id ASC"
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


def get_agent_summary() -> List[Dict[str, Any]]:
    conn = get_connection()
    rules_map = {
        row["agent_name"]: json.loads(row["rules_json"])
        for row in conn.execute("SELECT agent_name, rules_json FROM agent_rules").fetchall()
    }

    counters = conn.execute(
        """
        SELECT agent_name,
               SUM(CASE WHEN decision = 'accepted' THEN 1 ELSE 0 END) AS accepted_count,
               SUM(CASE WHEN decision = 'rejected' THEN 1 ELSE 0 END) AS rejected_count
        FROM agent_events
        GROUP BY agent_name
        """
    ).fetchall()
    counts = {
        row["agent_name"]: {
            "accepted_count": row["accepted_count"],
            "rejected_count": row["rejected_count"],
        }
        for row in counters
    }

    latest_events = conn.execute(
        """
        SELECT agent_name, status, action, message, decision, details
        FROM agent_events
        WHERE id IN (
            SELECT MAX(id) FROM agent_events GROUP BY agent_name
        )
        """
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
    conn.execute("UPDATE app_settings SET value = ? WHERE key = 'starting_capital'", (str(float(amount)),))
    conn.commit()
    conn.close()


def get_portfolio_summary() -> Dict[str, float]:
    conn = get_connection()
    starting = float(conn.execute("SELECT value FROM app_settings WHERE key = 'starting_capital'").fetchone()["value"])
    open_positions = conn.execute(
        "SELECT COALESCE(SUM(allocated_capital), 0) AS invested FROM trades WHERE status = 'OPEN'"
    ).fetchone()["invested"]
    realized = conn.execute("SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM trades WHERE status = 'CLOSED'").fetchone()["pnl"]
    conn.close()
    invested = float(open_positions or 0)
    realized = float(realized or 0)
    return {
        "starting_capital": starting,
        "cash_available": starting - invested + realized,
        "invested_capital": invested,
        "realized_pnl": realized,
        "account_equity": starting + realized,
    }


def get_trade_history() -> List[Dict[str, Any]]:
    conn = get_connection()
    trades = conn.execute("SELECT * FROM trades ORDER BY id DESC").fetchall()
    result = []
    for trade in trades:
        item = dict(trade)
        item["events"] = [
            {**dict(event), "details": json.loads(event["details"])}
            for event in conn.execute("SELECT * FROM trade_events WHERE trade_id = ? ORDER BY id", (trade["id"],))
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


def save_trade(symbol: str, side: str, leverage: float, exit_target: float, status: str, notes: Optional[str] = None) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO trades (symbol, side, leverage, exit_target, status, notes) VALUES (?, ?, ?, ?, ?, ?)",
        (symbol, side, leverage, exit_target, status, notes),
    )
    conn.commit()
    conn.close()


def save_risk_event(pnl: float, volatility: float, action: str, details: str) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO risk_events (pnl, volatility, action, details) VALUES (?, ?, ?, ?)",
        (pnl, volatility, action, details),
    )
    conn.commit()
    conn.close()


def get_dashboard_summary() -> Dict[str, Any]:
    conn = get_connection()
    latest_signal = conn.execute(
        "SELECT sector, signal, catalyst, confidence, payload, created_at FROM signals ORDER BY id DESC LIMIT 1"
    ).fetchone()
    latest_trade = conn.execute(
        "SELECT symbol, side, leverage, exit_target, status, notes, created_at FROM trades ORDER BY id DESC LIMIT 1"
    ).fetchone()
    risk_events = conn.execute(
        "SELECT pnl, volatility, action, details, created_at FROM risk_events ORDER BY id DESC LIMIT 5"
    ).fetchall()
    agent_summary = get_agent_summary()
    conn.close()

    return {
        "latest_signal": dict(latest_signal) if latest_signal else None,
        "latest_trade": dict(latest_trade) if latest_trade else None,
        "risk_events": [dict(row) for row in risk_events],
        "agents": agent_summary,
    }


class ResearcherAgent:
    def __init__(self) -> None:
        self._feed_index = 0

    def _next_payload_for_sector(self, sector: str) -> Dict[str, Any]:
        feed = load_research_feed()
        candidates = [item for item in feed if item.get("sector") == sector]
        if not candidates:
            candidates = feed
        selected = candidates[self._feed_index % len(candidates)]
        self._feed_index += 1
        return {
            "sector": selected.get("sector", sector),
            "signal": selected.get("signal", "neutral"),
            "catalyst": selected.get("catalyst", "macro"),
            "confidence": float(selected.get("confidence", 0.5)),
            "ticker": selected.get("ticker", "UNKNOWN"),
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

        position = "LONG" if tech_data.get("signal") == "bullish" else "SHORT"
        leverage = 2.0
        exit_target = 1.05 if position == "LONG" else 0.95
        decision = "accepted" if float(tech_data.get("confidence", 0.0)) >= float(rules.get("min_confidence", 0.7)) else "rejected"

        payload = {
            "position": position,
            "leverage": leverage,
            "exit_target": exit_target,
            "assets": [tech_data.get("ticker", "AAPL"), energy_data.get("ticker", "USOIL")],
            "notes": "Balanced exposure across tech strength and energy macro risk.",
        }
        record_agent_event(
            "strategist",
            "completed",
            "build_strategy",
            f"Strategy selected {position} with leverage {leverage}",
            decision,
            payload,
        )
        return payload


class RiskManagerAgent:
    def check_risk(self, pnl: float, volatility: float, strategy_signal: Dict[str, Any]) -> Dict[str, Any]:
        rules = get_agent_rules().get("risk_manager", DEFAULT_AGENT_RULES["risk_manager"])
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

        save_risk_event(pnl=pnl, volatility=volatility, action=action, details=details)
        record_agent_event(
            "risk_manager",
            "completed",
            "risk_check",
            details,
            decision,
            {"pnl": pnl, "volatility": volatility, "action": action},
        )
        return {"action": action, "details": details, "decision": decision}


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


async def run_trading_cycle() -> Dict[str, Any]:
    researcher = ResearcherAgent()
    strategist = StrategistAgent()
    risk_manager = RiskManagerAgent()
    execution = ExecutionAgent()

    record_agent_event("researcher", "running", "start_cycle", "Starting market research cycle.", "pending", {})
    record_agent_event("strategist", "running", "start_cycle", "Starting strategist evaluation.", "pending", {})
    record_agent_event("risk_manager", "running", "start_cycle", "Starting risk validation.", "pending", {})
    record_agent_event("execution", "running", "start_cycle", "Starting execution review.", "pending", {})

    market_data = await researcher.run_data_fetch()
    strategy_signal = strategist.analyze_and_decide(market_data)

    pnl = 0.03
    volatility = 0.018
    risk_decision = risk_manager.check_risk(pnl, volatility, strategy_signal)
    trade = execution.place_trade(strategy_signal, pnl=pnl, volatility=volatility)

    return {
        "market_data": market_data,
        "strategy_signal": strategy_signal,
        "risk_decision": risk_decision,
        "trade": trade,
        "agent_summary": get_agent_summary(),
    }


async def main() -> None:
    ensure_db()
    result = await run_trading_cycle()
    logger.info("Trading cycle complete: %s", json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    ensure_db()
    asyncio.run(main())