"""Broker payload normalization helpers; no application imports."""
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

def _coerce_optional_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None

def _first_numeric_by_keys(payload: Any, preferred_keys: tuple[str, ...]) -> Optional[float]:
    if isinstance(payload, dict):
        for key in preferred_keys:
            if key in payload:
                numeric = _coerce_money(payload.get(key))
                if numeric is not None:
                    return numeric
        for value in payload.values():
            nested = _first_numeric_by_keys(value, preferred_keys)
            if nested is not None:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _first_numeric_by_keys(item, preferred_keys)
            if nested is not None:
                return nested
    return None

def _coerce_money(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None

def _broker_identifier(payload: Any, keys: tuple[str, ...]) -> Optional[str]:
    if isinstance(payload, dict):
        for key in keys:
            if payload.get(key) is not None:
                value = str(payload[key])
                return value[:-2] if value.endswith(".0") else value
        for value in payload.values():
            found = _broker_identifier(value, keys)
            if found is not None:
                return found
    return None

def _broker_time(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            stamp = datetime.fromtimestamp(value/1000 if abs(value)>100_000_000_000 else value, timezone.utc)
        else:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None

def _etoro_instrument_id(item: Dict[str, Any]) -> int:
    instrument_id = item.get("instrumentId", item.get("instrumentID", item.get("internalInstrumentId")))
    if instrument_id is None:
        raise ValueError("eToro response did not include an instrument ID.")
    return int(instrument_id)

def _redact_etoro_log_value(value: Any, key: str = "") -> Any:
    sensitive_fragments = ("api-key", "api_key", "user-key", "user_key", "authorization", "token", "secret", "password")
    normalized_key = key.lower().replace("x-", "")
    if any(fragment in normalized_key for fragment in sensitive_fragments):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(child_key): _redact_etoro_log_value(child, str(child_key)) for child_key, child in value.items()}
    if isinstance(value, list):
        return [_redact_etoro_log_value(child) for child in value[:100]]
    if isinstance(value, str) and len(value) > 12000:
        return value[:12000] + "… [truncated]"
    return value


def _normalize_portfolio_balance(payload: Dict[str, Any], *, source: str, provider: str) -> Dict[str, Any]:
    balance = _first_numeric_by_keys(
        payload,
        (
            "displayBalance",
            "totalBalance",
            "balance",
            "portfolioValue",
            "accountValue",
            "netEquity",
            "totalAmount",
            "credit",
        ),
    )
    cash_available = _first_numeric_by_keys(
        payload,
        ("available", "availableAmount", "availableCash", "cash", "spendableBalanceInFiat"),
    )
    currency = (
        str(payload.get("displayCurrency") or payload.get("currency") or "").upper() or None
    )
    if cash_available is None and isinstance(payload.get("equityDetails"), dict):
        cash_available = _first_numeric_by_keys(
            payload["equityDetails"],
            ("available", "spendableBalanceInFiat", "cash"),
        )
        if not currency:
            nested_currency = payload["equityDetails"].get("currency")
            currency = str(nested_currency).upper() if nested_currency else None
    if balance is None and cash_available is not None:
        balance = cash_available
    if balance is None and cash_available is None:
        return {
            "available": False,
            "provider": provider,
            "source": source,
            "message": "The eToro response did not expose a recognizable balance field.",
            "payload": payload,
        }
    return {
        "available": balance is not None,
        "provider": provider,
        "source": source,
        "balance": balance,
        "cash_available": cash_available,
        "currency": currency,
        "payload": payload,
    }
