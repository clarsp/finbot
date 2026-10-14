"""SMTP notifications for acknowledged broker submissions."""

from __future__ import annotations

import html
import json
import os
import re
import smtplib
import sqlite3
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from typing import Any


SENSITIVE_KEY_SUFFIXES = (
    "authorization", "apikey", "userkey", "password", "passwd", "secret",
    "accesstoken", "refreshtoken", "authtoken", "cookie",
)


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def parse_recipient_addresses(value: str | None) -> tuple[str, ...]:
    """Accept comma, semicolon, or line separated destination addresses."""
    normalized = re.sub(r"[;\r\n]+", ",", str(value or ""))
    addresses = []
    for _name, address in getaddresses([normalized]):
        address = address.strip()
        if address and "@" in address and address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def _runtime_configuration() -> dict[str, str]:
    """Reload local UI-managed SMTP values without restarting the scheduler."""
    path = Path(os.getenv("FINBOT_ENV_CONFIG_PATH", Path(__file__).resolve().parent / ".env.config"))
    values: dict[str, str] = {}
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key.startswith("FINBOT_SMTP_") or key.startswith("FINBOT_TRADE_EMAIL_"):
                    values[key] = value
    return values


@dataclass(frozen=True)
class SMTPSettings:
    enabled: bool
    host: str
    port: int
    security: str
    username: str
    password: str
    sender: str
    recipients: tuple[str, ...]
    timeout_seconds: float
    subject_prefix: str
    attach_json: bool

    @classmethod
    def from_environment(cls) -> "SMTPSettings":
        runtime = _runtime_configuration()
        setting = lambda name, default="": runtime.get(name, os.getenv(name, default))
        security = setting("FINBOT_SMTP_SECURITY", "starttls").strip().lower()
        if security not in {"starttls", "tls", "none"}:
            raise ValueError("FINBOT_SMTP_SECURITY must be starttls, tls, or none.")
        raw_recipients = setting("FINBOT_TRADE_EMAIL_TO")
        recipients = parse_recipient_addresses(raw_recipients)
        settings = cls(
            enabled=(
                _enabled(setting("FINBOT_TRADE_EMAIL_ENABLED"))
                and not _enabled(os.getenv("FINBOT_DISABLE_EXTERNAL_NOTIFICATIONS"))
            ),
            host=setting("FINBOT_SMTP_HOST").strip(),
            port=int(setting("FINBOT_SMTP_PORT", "587")),
            security=security,
            username=setting("FINBOT_SMTP_USERNAME").strip(),
            password=setting("FINBOT_SMTP_PASSWORD"),
            sender=setting("FINBOT_SMTP_FROM").strip(),
            recipients=recipients,
            timeout_seconds=float(setting("FINBOT_SMTP_TIMEOUT_SECONDS", "20")),
            subject_prefix=setting("FINBOT_TRADE_EMAIL_SUBJECT_PREFIX", "[Finbot]").strip(),
            attach_json=_enabled(setting("FINBOT_TRADE_EMAIL_ATTACH_JSON", "true")),
        )
        if settings.enabled:
            missing = [
                name for name, value in (
                    ("FINBOT_SMTP_HOST", settings.host),
                    ("FINBOT_SMTP_FROM", settings.sender),
                    ("FINBOT_TRADE_EMAIL_TO", settings.recipients),
                ) if not value
            ]
            if missing:
                raise ValueError("Missing enabled trade email settings: " + ", ".join(missing))
            if not 1 <= settings.port <= 65535:
                raise ValueError("FINBOT_SMTP_PORT must be between 1 and 65535.")
            if settings.timeout_seconds <= 0:
                raise ValueError("FINBOT_SMTP_TIMEOUT_SECONDS must be positive.")
        return settings


def _sanitize(value: Any, key: str = "") -> Any:
    normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
    if normalized_key == "token" or any(normalized_key.endswith(suffix) for suffix in SENSITIVE_KEY_SUFFIXES):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _sanitize(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        # Mask credential-like query values that may be embedded in URLs/logs.
        return re.sub(
            r"(?i)(api[_-]?key|user[_-]?key|token|secret|password)=([^&\s]+)",
            r"\1=[REDACTED]",
            value,
        )
    return value


def load_trade_evidence(database_path: Path, trade_id: int) -> dict[str, Any]:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        trade_row = connection.execute("SELECT * FROM trades WHERE id = ?", (int(trade_id),)).fetchone()
        if not trade_row:
            raise ValueError(f"Trade #{trade_id} does not exist.")
        event_rows = connection.execute(
            "SELECT id,created_at,stage,agent_name,status,decision,message,details "
            "FROM trade_events WHERE trade_id = ? ORDER BY id",
            (int(trade_id),),
        ).fetchall()
    finally:
        connection.close()
    events = []
    for row in event_rows:
        event = dict(row)
        try:
            event["details"] = json.loads(event.get("details") or "{}")
        except (TypeError, json.JSONDecodeError):
            event["details"] = {"unparsed_details": str(event.get("details") or "")}
        events.append(event)
    if any(event.get("stage") == "execution" and event.get("decision") == "submitted" for event in events):
        for event in events:
            if (
                event.get("stage") == "execution"
                and event.get("decision") == "pending"
                and (event.get("details") or {}).get("status") == "ready"
            ):
                event["decision"] = "accepted"
                event["message"] = (
                    "Execution readiness passed; final broker checks completed and the later execution event submitted the order."
                )
    return _sanitize({"trade": dict(trade_row), "stages": events})


def _summary_lines(evidence: dict[str, Any]) -> list[str]:
    trade = evidence["trade"]
    lines = [
        f"Trade: #{trade['id']} {trade.get('company_name') or trade['symbol']} ({trade['symbol']})",
        f"Direction: {trade['side']} | status: {trade['status']} | leverage: {trade['leverage']}x",
        f"Requested entry: {trade.get('entry_price')} | units: {trade.get('quantity')}",
        f"Stop loss: {trade.get('stop_loss_price')} | take profit: {trade.get('take_profit_price')}",
        f"Allocated capital: {trade.get('allocated_capital')} | quote: {trade.get('quote_provider')} at {trade.get('quote_timestamp')}",
        f"Broker order ID: {trade.get('broker_order_id') or 'not returned'} | broker position ID: {trade.get('broker_position_id') or 'pending reconciliation'}",
        "",
        "Decision stages:",
    ]
    for event in evidence["stages"]:
        lines.append(
            f"- {event['created_at']} | {event['stage']} / {event['agent_name']} | "
            f"{event['decision']} ({event['status']}): {event['message']}"
        )
    broker_linked = bool(trade.get("broker_order_id") or trade.get("broker_position_id"))
    if broker_linked:
        state = "This lifecycle is broker-linked. Use its current status and reconciliation fields as the execution source of truth."
    else:
        state = "No broker order or position is linked to this lifecycle."
    lines.extend(("", state))
    return lines


def build_trade_report_html(evidence: dict[str, Any]) -> str:
    trade = evidence["trade"]
    stage_sections = "".join(
        "<section>"
        f"<h3>{html.escape(str(event['stage']).title())} · {html.escape(str(event['decision']).title())}</h3>"
        f"<p><strong>{html.escape(str(event['agent_name']))}</strong> · {html.escape(str(event['created_at']))}</p>"
        f"<p>{html.escape(str(event['message']))}</p>"
        "<details><summary>Supporting data</summary>"
        f"<pre>{html.escape(json.dumps(event['details'], indent=2, sort_keys=True, default=str))}</pre>"
        "</details></section>"
        for event in evidence["stages"]
    )
    summary = "".join(f"<li>{html.escape(line)}</li>" for line in _summary_lines(evidence)[:6])
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Finbot trade #{trade['id']}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#172033}"
        "section{border:1px solid #ccd3df;border-radius:8px;padding:1rem;margin:1rem 0}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:1rem;border-radius:6px}"
        "h1,h2,h3{color:#102a56}li{margin:.35rem 0}</style></head><body>"
        f"<h1>Finbot trade lifecycle report #{trade['id']}</h1>"
        f"<p>Generated from sanitized stored evidence for {html.escape(str(trade['symbol']))}.</p>"
        f"<ul>{summary}</ul><h2>Agent stages and supporting data</h2>{stage_sections}"
        "</body></html>"
    )


def build_trade_message(
    settings: SMTPSettings, evidence: dict[str, Any], *, test_mode: bool = False
) -> EmailMessage:
    trade = evidence["trade"]
    lines = _summary_lines(evidence)
    message = EmailMessage()
    test_label = " SMTP TEST:" if test_mode else " eToro demo order submitted:"
    message["Subject"] = (
        f"{settings.subject_prefix}{test_label} "
        f"#{trade['id']} {trade['symbol']} {trade['side']}"
    )
    message["From"] = settings.sender
    message["To"] = ", ".join(settings.recipients)
    if test_mode:
        lines.insert(0, "SMTP TEST — this message did not submit or modify a trade.\n")
    message.set_content("\n".join(lines))
    stage_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(event['created_at']))}</td>"
        f"<td>{html.escape(str(event['stage']))}</td>"
        f"<td>{html.escape(str(event['agent_name']))}</td>"
        f"<td>{html.escape(str(event['decision']))}</td>"
        f"<td>{html.escape(str(event['message']))}</td>"
        "</tr>"
        for event in evidence["stages"]
    )
    summary_html = "<br>".join(html.escape(line) for line in lines[:7])
    test_notice = (
        "<p><strong>This is an SMTP delivery test. No trade was submitted or modified.</strong></p>"
        if test_mode else ""
    )
    state_html = html.escape(_summary_lines(evidence)[-1])
    message.add_alternative(
        "<html><body>"
        f"<h2>{html.escape(settings.subject_prefix)} {'SMTP test' if test_mode else 'eToro demo submission'}</h2>"
        f"{test_notice}"
        f"<p>{summary_html}</p>"
        "<h3>Decision stages</h3>"
        "<table border='1' cellpadding='6' cellspacing='0'>"
        "<tr><th>Time</th><th>Stage</th><th>Agent</th><th>Decision</th><th>Evidence summary</th></tr>"
        f"{stage_rows}</table>"
        f"<p><strong>State:</strong> {state_html}</p>"
        "</body></html>",
        subtype="html",
    )
    if settings.attach_json:
        payload = json.dumps(evidence, indent=2, sort_keys=True, default=str).encode("utf-8")
        message.add_attachment(
            payload,
            maintype="application",
            subtype="json",
            filename=f"finbot-trade-{trade['id']}-evidence.json",
        )
    return message


def send_trade_execution_email(
    database_path: Path, trade_id: int, *, test_mode: bool = False
) -> dict[str, Any]:
    settings = SMTPSettings.from_environment()
    if not settings.enabled:
        return {"status": "disabled"}
    evidence = load_trade_evidence(database_path, trade_id)
    message = build_trade_message(settings, evidence, test_mode=test_mode)
    context = ssl.create_default_context()
    if settings.security == "tls":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.host, settings.port, timeout=settings.timeout_seconds, context=context
        )
    else:
        smtp = smtplib.SMTP(settings.host, settings.port, timeout=settings.timeout_seconds)
    with smtp:
        smtp.ehlo()
        if settings.security == "starttls":
            smtp.starttls(context=context)
            smtp.ehlo()
        if settings.username:
            smtp.login(settings.username, settings.password)
        smtp.send_message(message)
    return {
        "status": "sent",
        "security": settings.security,
        "recipient_count": len(settings.recipients),
        "json_attached": settings.attach_json,
        "test_mode": test_mode,
    }


def test_smtp_connection() -> dict[str, Any]:
    """Verify transport encryption and authentication without sending mail."""
    settings = SMTPSettings.from_environment()
    if not settings.enabled:
        raise ValueError("Trade email notifications are disabled.")
    context = ssl.create_default_context()
    if settings.security == "tls":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.host, settings.port, timeout=settings.timeout_seconds, context=context
        )
    else:
        smtp = smtplib.SMTP(settings.host, settings.port, timeout=settings.timeout_seconds)
    with smtp:
        smtp.ehlo()
        if settings.security == "starttls":
            if not smtp.has_extn("starttls"):
                raise RuntimeError("The SMTP server did not advertise STARTTLS.")
            smtp.starttls(context=context)
            smtp.ehlo()
        if settings.username:
            smtp.login(settings.username, settings.password)
        supports_smtp_utf8 = smtp.has_extn("smtputf8")
    return {
        "status": "connected",
        "security": settings.security,
        "authenticated": bool(settings.username),
        "supports_smtp_utf8": supports_smtp_utf8,
    }
