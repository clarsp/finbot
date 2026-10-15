"""Exchange-aware market-session context for research and trade decisions.

Session context is evidence, not an execution authorization.  The broker must
still confirm that it accepts an order at submission time.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


EXCHANGES: Dict[str, Dict[str, Any]] = {
    "NASDAQ": {"timezone": "America/New_York", "open": "09:30", "close": "16:00"},
    "NYSE": {"timezone": "America/New_York", "open": "09:30", "close": "16:00"},
    "NYSE ARCA": {"timezone": "America/New_York", "open": "09:30", "close": "16:00"},
    "NYSE AMERICAN": {"timezone": "America/New_York", "open": "09:30", "close": "16:00"},
    "LSE": {"timezone": "Europe/London", "open": "08:00", "close": "16:30"},
    "LSE AIM": {"timezone": "Europe/London", "open": "08:00", "close": "16:30"},
    "XETRA": {"timezone": "Europe/Berlin", "open": "09:00", "close": "17:30"},
    "FRANKFURT": {"timezone": "Europe/Berlin", "open": "09:00", "close": "17:30"},
    "EURONEXT PARIS": {"timezone": "Europe/Paris", "open": "09:00", "close": "17:30"},
    "EURONEXT AMSTERDAM": {"timezone": "Europe/Amsterdam", "open": "09:00", "close": "17:30"},
    "EURONEXT BRUSSELS": {"timezone": "Europe/Brussels", "open": "09:00", "close": "17:30"},
    "EURONEXT LISBON": {"timezone": "Europe/Lisbon", "open": "08:00", "close": "16:30"},
    "STOCKHOLM": {"timezone": "Europe/Stockholm", "open": "09:00", "close": "17:30"},
    "COPENHAGEN": {"timezone": "Europe/Copenhagen", "open": "09:00", "close": "17:00"},
    "OSLO": {"timezone": "Europe/Oslo", "open": "09:00", "close": "16:20"},
    "SIX": {"timezone": "Europe/Zurich", "open": "09:00", "close": "17:30"},
    "TOKYO": {"timezone": "Asia/Tokyo", "open": "09:00", "close": "15:30", "break": ("11:30", "12:30")},
    "HONG KONG": {"timezone": "Asia/Hong_Kong", "open": "09:30", "close": "16:00", "break": ("12:00", "13:00")},
    "ASX": {"timezone": "Australia/Sydney", "open": "10:00", "close": "16:00"},
    "TORONTO": {"timezone": "America/Toronto", "open": "09:30", "close": "16:00"},
}

ALIASES = {
    "NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NAS": "NASDAQ",
    "NYQ": "NYSE", "PCX": "NYSE ARCA", "ASE": "NYSE AMERICAN",
    "LONDON STOCK EXCHANGE": "LSE", "LONDON": "LSE", "LON": "LSE",
    "GER": "XETRA", "FRA": "FRANKFURT", "PAR": "EURONEXT PARIS",
    "AMS": "EURONEXT AMSTERDAM", "STO": "STOCKHOLM", "CPH": "COPENHAGEN",
    "JPX": "TOKYO", "TSE": "TOKYO", "TOKYO STOCK EXCHANGE": "TOKYO",
    "HKG": "HONG KONG", "HKSE": "HONG KONG", "AUSTRALIAN STOCK EXCHANGE": "ASX",
    "TOR": "TORONTO", "TSX": "TORONTO",
}


def _clock(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)


def _canonical_exchange(*values: Any) -> Optional[str]:
    for value in values:
        label = " ".join(str(value or "").strip().upper().replace("_", " ").split())
        if not label:
            continue
        if label in EXCHANGES:
            return label
        if label in ALIASES:
            return ALIASES[label]
        for known in EXCHANGES:
            if known in label or label in known:
                return known
    return None


def build_market_session_context(
    *,
    instrument: Optional[Dict[str, Any]] = None,
    fundamentals: Optional[Dict[str, Any]] = None,
    quote: Optional[Dict[str, Any]] = None,
    observed_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Describe the underlying exchange session and separately observed broker state."""
    instrument = instrument or {}
    fundamentals = fundamentals or {}
    quote = quote or {}
    raw = instrument.get("instrument") if isinstance(instrument.get("instrument"), dict) else {}
    exchange = _canonical_exchange(
        instrument.get("exchange_name"), raw.get("internalExchangeName"), raw.get("exchangeName"),
        fundamentals.get("full_exchange_name"), fundamentals.get("exchange"),
    )
    timezone_name = fundamentals.get("exchange_timezone")
    schedule = EXCHANGES.get(exchange or "", {})
    timezone_name = timezone_name or schedule.get("timezone")
    now_utc = observed_at or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)
    broker_quote_state = "QUOTE_AVAILABLE" if quote.get("available") else "QUOTE_UNAVAILABLE"
    if instrument.get("currently_tradeable") is False:
        broker_quote_state = "BROKER_MARKED_UNAVAILABLE"
    result: Dict[str, Any] = {
        "available": False,
        "primary_exchange": exchange,
        "exchange_timezone": timezone_name,
        "observed_at": now_utc.isoformat(),
        "underlying_session_state": "UNKNOWN",
        "market_session": "UNKNOWN",
        "etoro_execution_state": broker_quote_state,
        "etoro_quote_available": bool(quote.get("available")),
        "etoro_quote_timestamp": quote.get("fetched_at"),
        "is_regular_session": False,
        "calendar_status": "weekday_schedule_only",
        "calendar_warning": "Holiday and exceptional-closure status was not confirmed by the available provider metadata.",
    }
    if not exchange or not timezone_name or not schedule:
        result["reason"] = "The instrument's primary exchange or timezone could not be resolved."
        return result
    try:
        local_now = now_utc.astimezone(ZoneInfo(str(timezone_name)))
    except ZoneInfoNotFoundError:
        result["reason"] = f"Timezone {timezone_name} is unavailable."
        return result

    opened = _clock(schedule["open"])
    closed = _clock(schedule["close"])
    provider_start = fundamentals.get("exchange_session_start_epoch")
    provider_end = fundamentals.get("exchange_session_end_epoch")
    provider_session_date = None
    try:
        provider_open = datetime.fromtimestamp(float(provider_start), timezone.utc).astimezone(local_now.tzinfo)
        provider_close = datetime.fromtimestamp(float(provider_end), timezone.utc).astimezone(local_now.tzinfo)
        provider_session_date = provider_open.date()
        if provider_session_date == local_now.date() and provider_close > provider_open:
            opened = provider_open.timetz().replace(tzinfo=None)
            closed = provider_close.timetz().replace(tzinfo=None)
            result["calendar_status"] = "provider_session_confirmed"
            result["calendar_warning"] = None
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    current = local_now.timetz().replace(tzinfo=None)
    weekday_open = local_now.weekday() < 5
    state = "CLOSED"
    session = "CLOSED"
    progress = None
    provider_reports_closed_day = bool(
        weekday_open and provider_session_date is not None and provider_session_date != local_now.date()
        and str(fundamentals.get("yahoo_market_state") or "").upper() == "CLOSED"
    )
    if provider_reports_closed_day:
        state, session = "CLOSED_PROVIDER_REPORTED", "CLOSED"
        result["calendar_status"] = "provider_closed"
        result["calendar_warning"] = None
    elif weekday_open and opened <= current < closed:
        session_minutes = (closed.hour * 60 + closed.minute) - (opened.hour * 60 + opened.minute)
        elapsed = (current.hour * 60 + current.minute) - (opened.hour * 60 + opened.minute)
        progress = max(0.0, min(1.0, elapsed / session_minutes))
        market_break = schedule.get("break")
        if market_break and _clock(market_break[0]) <= current < _clock(market_break[1]):
            state, session = "REGULAR_BREAK", "MIDDAY_BREAK"
        elif elapsed < 30:
            state, session = "REGULAR_OPEN", "OPENING_DISCOVERY"
        elif (session_minutes - elapsed) <= 30:
            state, session = "REGULAR_OPEN", "CLOSING_PERIOD"
        elif progress < 0.35:
            state, session = "REGULAR_OPEN", "MORNING"
        elif progress < 0.65:
            state, session = "REGULAR_OPEN", "MIDDAY"
        else:
            state, session = "REGULAR_OPEN", "AFTERNOON"
    elif weekday_open and current < opened:
        state, session = "PRE_MARKET", "PRE_MARKET_OR_CLOSED"
    elif weekday_open:
        state, session = "AFTER_HOURS", "AFTER_HOURS_OR_CLOSED"

    result.update({
        "available": True,
        "local_timestamp": local_now.isoformat(),
        "local_weekday": local_now.strftime("%A"),
        "regular_open_local": opened.strftime("%H:%M"),
        "regular_close_local": closed.strftime("%H:%M"),
        "regular_break_local": list(schedule["break"]) if schedule.get("break") else None,
        "underlying_session_state": state,
        "market_session": session,
        "is_regular_session": state == "REGULAR_OPEN",
        "session_progress": progress,
        "minutes_from_open": max(0, (current.hour * 60 + current.minute) - (opened.hour * 60 + opened.minute)) if weekday_open and current >= opened else None,
        "minutes_to_close": max(0, (closed.hour * 60 + closed.minute) - (current.hour * 60 + current.minute)) if weekday_open and current < closed else None,
    })
    return result
