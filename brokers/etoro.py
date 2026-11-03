"""eToro HTTP endpoints and response translation. No trading-workflow imports."""
from __future__ import annotations
import json
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from market_instruments import sp500_index_supported
from .etoro_sessions import execution_session, CALENDARS
from .exchange_registry import discover_calendar, normalized_name
from .quotes import assess_rate, read_rate_with_retries, MissingQuote
from .contracts import instrument_blockers
from .etoro_symbols import search_symbol, select_instrument
from .errors import BrokerAPIError as EtoroAPIError, BrokerPreflightError
from .parsing import (_broker_identifier, _broker_time, _coerce_money, _coerce_optional_float,
                      _etoro_instrument_id, _first_numeric_by_keys, _redact_etoro_log_value, _normalize_portfolio_balance)
logger = logging.getLogger(__name__)

class EtoroBrokerAdapter:
    supports_native_trailing = True
    def __init__(self, settings: Any) -> None:
        if not settings.etoro_api_key or not settings.etoro_user_key:
            raise ValueError("ETORO_API_KEY and ETORO_USER_KEY are required when the eToro provider is selected.")
        self.settings = settings
        self.api_logs: list[Dict[str, Any]] = []

    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.settings.etoro_api_key,
            "x-user-key": self.settings.etoro_user_key,
            "x-request-id": str(uuid.uuid4()),
            "User-Agent": self.settings.etoro_user_agent.strip().strip("\"'").strip(),
        }

    def _request_json(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        base_url = self.settings.etoro_api_base_url.rstrip("/")
        url = f"{base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"
        headers = self._headers()
        if request_id:
            headers["x-request-id"] = request_id
        request_id = headers["x-request-id"]
        safe_headers = {
            key: value for key, value in headers.items()
            if key.lower() in {"x-request-id", "content-type", "user-agent"}
        }
        api_log: Dict[str, Any] = {
            "request_id": request_id,
            "request": {
                "method": method.upper(),
                "url": url,
                "headers": _redact_etoro_log_value(safe_headers),
                "body": _redact_etoro_log_value(payload) if payload is not None else None,
            },
        }
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            safe_headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        if method.upper() == "POST" and "/trading/execution/" in path and self.settings.etoro_api_debug_logging:
            logger.info(
                "eToro API debug request request_id=%s endpoint=%s payload=%s",
                request_id,
                path,
                json.dumps(api_log["request"], sort_keys=True),
            )
        elif method.upper() == "POST" and "/trading/execution/" in path:
            logger.info("eToro order request request_id=%s endpoint=%s", request_id, path)
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8")
                result = json.loads(body) if body else {}
                api_log["response"] = {
                    "status": response.status,
                    "body": _redact_etoro_log_value(result),
                }
                if self.settings.etoro_api_debug_logging:
                    self.api_logs.append(api_log)
                    logger.info("eToro API debug response request_id=%s payload=%s", request_id, json.dumps(api_log["response"], sort_keys=True))
                logger.info(
                    "eToro API request succeeded request_id=%s method=%s endpoint=%s status=%s",
                    request_id,
                    method,
                    path,
                    response.status,
                )
                return result
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace") if hasattr(error, "read") else ""
            message = body.strip() or error.reason or f"HTTP {error.code}"
            try:
                response_body: Any = json.loads(body) if body else message
            except json.JSONDecodeError:
                response_body = body or message
            api_log["response"] = {
                "status": error.code,
                "body": _redact_etoro_log_value(response_body),
            }
            if self.settings.etoro_api_debug_logging:
                self.api_logs.append(api_log)
                logger.info("eToro API debug response request_id=%s payload=%s", request_id, json.dumps(api_log["response"], sort_keys=True))
            logger.error(
                "eToro API request failed request_id=%s method=%s endpoint=%s status=%s response=%s",
                request_id,
                method,
                path,
                error.code,
                message,
            )
            retry_after = None
            raw_retry_after = (error.headers or {}).get('Retry-After')
            if raw_retry_after:
                try:
                    retry_after = max(0, float(raw_retry_after))
                except ValueError:
                    try: retry_after = max(0, (parsedate_to_datetime(raw_retry_after)-datetime.now(timezone.utc)).total_seconds())
                    except (TypeError, ValueError): pass
            raise EtoroAPIError(
                f"eToro request failed with HTTP {error.code} (request ID {request_id}): {message}",
                api_logs=self.api_logs, status_code=error.code, retry_after_seconds=retry_after,
            ) from error
        except URLError as error:
            api_log["response"] = {"status": "connection_error", "body": str(error.reason)}
            if self.settings.etoro_api_debug_logging:
                self.api_logs.append(api_log)
            logger.error(
                "eToro API connection failed request_id=%s method=%s endpoint=%s error=%s",
                request_id,
                method,
                path,
                error.reason,
            )
            raise EtoroAPIError(
                f"eToro request failed to connect (request ID {request_id}): {error.reason}",
                api_logs=self.api_logs,
            ) from error

    def _find_instrument(self, symbol: str) -> Dict[str, Any]:
        payload = self._request_json(
            "GET",
            "/api/v1/market-data/search",
            params={"internalSymbolFull": search_symbol(symbol), "fields": "instrumentId,internalInstrumentId,internalSymbolFull,internalAssetClassName,internalExchangeName,internalExchangeId,exchangeID,currency,internalInstrumentCurrency,isCurrentlyTradable,isExchangeOpen,isActiveInPlatform,isDelisted,isBuyEnabled,isSellEnabled,isShortEnabled"},
        )
        items = payload.get("items") or payload.get("results") or payload.get("data") or []
        return select_instrument(symbol,items)

    def resolve_instrument(self, symbol: str, *, verify_availability=True) -> Dict[str, Any]:
        instrument = self._find_instrument(symbol)
        normalized = self._normalize_instrument(symbol, instrument)
        if (normalized.get('supported_asset')
                and normalized_name(normalized.get('exchange_name')) not in CALENDARS):
            normalized['exchange_calendar_resolution'] = discover_calendar(normalized, self._request_json)
        if verify_availability:
            self._reconcile_exchange_flag(normalized)
        return normalized

    def _reconcile_exchange_flag(self, instrument, rate=None, *, raise_quote_errors=False):
        """Retain search metadata; reconcile conflicts using independent evidence.

        Eligibility describes account configuration, not the market clock. It
        must never authorize an out-of-session or undated/stale quote by itself.
        """
        if (instrument.get('exchange_open') is not False
                or instrument.get('currently_tradeable') is not True
                or instrument.get('platform_active') is not True
                or instrument.get('delisted') is True
                or not instrument.get('supported_asset')
                or not self.settings.etoro_demo_trading):
            return
        session = execution_session(instrument)
        evidence = {'source': 'etoro_demo_eligibility_and_quote',
                    'checked_at': datetime.now(timezone.utc).isoformat(),
                    'session': session, 'verified': False}
        instrument['execution_availability'] = evidence
        if not session.get('is_regular_session'):
            evidence['reason'] = session.get('reason') or 'Outside the primary exchange regular session.'
            return
        rate_requested = False
        try:
            result = self._request_json('POST', '/api/v2/trading/info/demo/eligibility',
                payload={'instrumentIds': [instrument['instrument_id']], 'currency': 'USD'})
            matches = [row for row in result.get('eligibilities', [])
                       if row.get('instrumentId') == instrument['instrument_id']
                       and str(row.get('symbol', '')).upper() == instrument['broker_symbol']]
            if len(matches) != 1 or instrument['instrument_id'] in result.get('notFoundInstrumentIds', []):
                raise ValueError('Broker eligibility identity is missing or ambiguous.')
            eligibility = matches[0]
            evidence['eligibility'] = eligibility
            if eligibility.get('allowOpenPosition') is not True:
                raise ValueError('Broker account does not explicitly permit opening this instrument.')
            rate_requested = rate is None
            rate = rate if rate is not None else self.get_rate(instrument['instrument_id'])
            quote_time = _broker_time(rate.get('date') or rate.get('timestamp') or rate.get('lastUpdated'))
            bid, ask = _coerce_optional_float(rate.get('bid')), _coerce_optional_float(rate.get('ask'))
            evidence['quote'] = {'bid': bid, 'ask': ask, 'quote_at': quote_time, 'quote_type': rate.get('quoteType')}
            evidence['quote_validation'] = rate.get('quote_validation') or assess_rate(rate)
            if (bid is None or ask is None or bid <= 0 or ask < bid
                    or str(rate.get('quoteType', '')).lower() != 'realtime'
                    or not quote_time
                    or not -5 <= (datetime.now(timezone.utc)-datetime.fromisoformat(quote_time)).total_seconds() <= 60):
                raise ValueError(evidence['quote_validation']['reason'])
            # The session may have ended while the broker requests were pending.
            if not execution_session(instrument).get('is_regular_session'):
                raise ValueError('Primary exchange regular session ended during verification.')
            evidence['verified'] = True
            evidence['reason'] = 'Search exchange flag conflicts with regular-session, account and quote evidence.'
            instrument['tradeable'] = True
        except Exception as error:
            evidence['reason'] = str(error)
            if rate_requested and rate is None and raise_quote_errors:
                raise
        return rate

    def _normalize_instrument(self, symbol, instrument):
        asset_class = str(
            instrument.get("assetClass")
            or instrument.get("assetType")
            or instrument.get("instrumentTypeName")
            or instrument.get("instrumentType")
            or instrument.get("internalAssetClassName")
            or ""
        ).strip().lower()
        supported_asset = "stock" in asset_class or "equity" in asset_class or "etf" in asset_class or sp500_index_supported(symbol,asset_class,_etoro_instrument_id(instrument))
        return {
            "symbol": symbol.upper(),
            "research_symbol": symbol.upper(),
            "broker_symbol": str(instrument.get("internalSymbolFull") or symbol).upper(),
            "symbol_resolution": instrument.get("_finbot_resolution") or {},
            "instrument_id": _etoro_instrument_id(instrument),
            "currency": (
                instrument.get("currency")
                or instrument.get("instrumentCurrency")
                or instrument.get("internalInstrumentCurrency")
                or (
                    "USD" if sp500_index_supported(symbol,asset_class,_etoro_instrument_id(instrument)) else
                    "USD"
                    if str(instrument.get("internalExchangeName") or "").strip().lower()
                    in {"nasdaq", "nyse", "nyse arca", "nyse american", "amex"}
                    else None
                )
            ),
            "provider": "etoro",
            "resolved": True,
            "tradeable": instrument.get("isCurrentlyTradable", instrument.get("isTradable")) is True
                         and instrument.get("isActiveInPlatform") is not False
                         and instrument.get("isDelisted") is not True
                         and instrument.get("isExchangeOpen") is not False,
            "exchange_open": instrument.get("isExchangeOpen"),
            "platform_active": instrument.get("isActiveInPlatform"),
            "delisted": instrument.get("isDelisted"),
            "asset_class": asset_class,
            "supported_asset": supported_asset,
            "buy_enabled": instrument.get("isBuyEnabled"),
            "short_enabled": instrument.get("isSellEnabled", instrument.get("isShortEnabled")),
            "exchange_name": instrument.get("internalExchangeName") or instrument.get("exchangeName"),
            "exchange_id": instrument.get("internalExchangeId") or instrument.get("exchangeID") or instrument.get("exchangeId"),
            "currently_tradeable": instrument.get("isCurrentlyTradable", instrument.get("isTradable")),
            "instrument": instrument,
        }

    def _get_rate(self, instrument_id: int) -> Dict[str, Any]:
        payload = self._request_json(
            "GET",
            "/api/v2/market-data/rates",
            params={"instrumentIds": [instrument_id]},
        )
        results = payload.get("results") or payload.get("rates") or []
        for result in results:
            result_id = _etoro_instrument_id(result)
            if result_id == instrument_id:
                return result
        raise MissingQuote(f"eToro did not return market data for instrument {instrument_id}.")

    def get_rate(self, instrument_id: int) -> Dict[str, Any]:
        return read_rate_with_retries(lambda: self._get_rate(instrument_id))

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        observed_at = datetime.now(timezone.utc).isoformat()
        instrument = None
        try:
            instrument = self.resolve_instrument(symbol, verify_availability=False)
            rate = self._reconcile_exchange_flag(instrument, raise_quote_errors=True)
            if rate is None:
                rate = self.get_rate(instrument['instrument_id'])
            bid = _coerce_optional_float(rate.get('bid'))
            ask = _coerce_optional_float(rate.get('ask'))
            if bid is None or ask is None or bid <= 0 or ask < bid:
                validation = assess_rate(rate)
                return {'available': False, 'executable': False, 'provider': 'eToro',
                        'ticker': symbol, 'fetched_at': observed_at, 'instrument': instrument,
                        'status': validation['code'], 'reason': validation['reason'], 'quote_validation': validation}
            blockers = instrument_blockers(instrument)
            validation = assess_rate(rate)
            validation['attempts'] = (rate.get('quote_validation') or {}).get('attempts', [])
            validation['retries_used'] = (rate.get('quote_validation') or {}).get('retries_used', 0)
            if validation['code'] == 'stale_quote' and validation['retries_used'] == 2:
                validation['reason'] += ' Still stale after 3 reads (2 retries).'
            if validation['code'] != 'fresh':
                blockers.append(validation['reason'])
            quote_time = _broker_time(rate.get('date') or rate.get('timestamp') or rate.get('lastUpdated') or rate.get('fetched_at'))
            reconciled = (instrument.get('execution_availability') or {}).get('verified') is True
            if ('date' in rate and not quote_time) or (reconciled and not quote_time):
                blockers.append('Broker quote timestamp is missing or invalid.')
            if reconciled and str(rate.get('quoteType', '')).lower() != 'realtime':
                blockers.append('Reconciled execution requires a realtime broker quote.')
            if quote_time:
                age = (datetime.now(timezone.utc)-datetime.fromisoformat(quote_time)).total_seconds()
                if age < -5 or age > 60:
                    blockers.append('Broker quote timestamp is stale or invalid.')
            delayed = 'delay' in str(rate.get('quoteType', '')).lower()
            if delayed:
                blockers.append('Broker identifies the quote as delayed.')
            return {'available': True, 'executable': not blockers, 'provider': 'eToro',
                    'ticker': symbol, 'quote_symbol': instrument.get('broker_symbol') or symbol, 'instrument_id': instrument['instrument_id'],
                    'price': (bid+ask)/2, 'bid': bid, 'ask': ask, 'buy_price': ask, 'sell_price': bid,
                    'fetched_at': observed_at, 'source': 'eToro Public API', 'quote_type': rate.get('quoteType'),
                    'status': 'indicative' if blockers else 'executable', 'reason': '; '.join(blockers),
                    'quote_at': quote_time,
                    'quote_validation': validation,
                    'instrument': instrument}
        except Exception as error:
            return {'available': False, 'executable': False, 'provider': 'eToro', 'ticker': symbol,
                    'price': None, 'fetched_at': observed_at, 'instrument': instrument,
                    'status': 'missing_quote' if isinstance(error, MissingQuote) else 'rate_limited' if getattr(error, 'status_code', None) == 429 else 'request_failed', 'reason': str(error),
                    'quote_validation': {'code': 'missing_quote' if isinstance(error, MissingQuote) else 'rate_limited' if getattr(error, 'status_code', None) == 429 else 'request_failed',
                        'reason': str(error), 'attempts': getattr(error, 'quote_attempts', []),
                        'retry_after_seconds': getattr(error, 'retry_after_seconds', None)}}

    def _prepare_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        if not self.settings.etoro_demo_trading:
            raise ValueError("Live eToro execution is disabled in this application.")
        symbol = str(order["symbol"]).upper()
        side = str(order.get("side", "LONG")).upper()
        amount = order.get("amount")
        units = order.get("units")
        if (amount is None) == (units is None):
            raise ValueError("eToro orders require exactly one of amount or units.")
        leverage = float(order.get("leverage", 1))
        if not math.isfinite(leverage) or leverage <= 0 or not leverage.is_integer():
            raise ValueError("eToro order leverage must be a positive whole number.")
        try:
            resolved = self.resolve_instrument(symbol)
            instrument = resolved['instrument']
        except Exception as error:
            raise BrokerPreflightError("Order not submitted: instrument lookup failed: " + str(error)) from error
        blockers = instrument_blockers(resolved, side)
        availability = resolved.get('execution_availability') or {}
        if availability.get('verified'):
            configs = availability['eligibility'].get('leverageConfigs') or []
            if not any(str(config.get('direction', '')).upper() == side
                       and config.get('isPotential') is False
                       and leverage in config.get('leverageValues', []) for config in configs):
                blockers.append('Broker account eligibility does not permit this direction and leverage.')
        if blockers:
            raise BrokerPreflightError("Order not submitted: " + "; ".join(blockers))
        instrument_id = _etoro_instrument_id(instrument)
        quantity = units if units is not None else amount
        if not math.isfinite(float(quantity)) or float(quantity) <= 0:
            raise ValueError("Order quantity or amount must be positive and finite.")
        if side == "SHORT" and instrument.get("isSellEnabled", instrument.get("isShortEnabled")) is False:
            raise ValueError("eToro has disabled shorts for this instrument.")
        if side == "LONG" and instrument.get("isBuyEnabled") is False:
            raise ValueError("eToro has disabled buys for this instrument.")
        transaction = {"LONG": "buy", "SHORT": "sellShort"}.get(side)
        if transaction is None:
            raise ValueError(f"Unsupported eToro order side: {side}")
        stop_loss_rate = _coerce_money(order.get("stop_loss_price"))
        take_profit_rate = _coerce_money(order.get("take_profit_price"))
        if (leverage > 1 or side == "SHORT") and (stop_loss_rate is None or stop_loss_rate <= 0):
            raise ValueError(
                "eToro requires a positive stop-loss price for orders with leverage greater than 1 and for short orders."
            )
        payload: Dict[str, Any] = {
            "action": "open",
            "transaction": transaction,
            "instrumentId": instrument_id,
            "orderType": "mkt",
            "leverage": int(leverage),
        }
        if amount is not None:
            payload["amount"] = float(amount)
            payload["orderCurrency"] = str(order.get("currency") or instrument.get("currency") or "usd").lower()
        if units is not None:
            payload["units"] = float(units)
        if stop_loss_rate is not None:
            payload["stopLossRate"] = stop_loss_rate
            payload["stopLossType"] = "fixed"
        # eToro's order request uses an explicit flag to disable TP. A null
        # rate alone is ignored by the demo endpoint, which can apply its own
        # distant default target.
        payload["takeProfitRate"] = take_profit_rate
        payload["isNoTakeProfit"] = take_profit_rate is None
        return payload

    def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        try:
            payload = self._prepare_order(order)
        except BrokerPreflightError:
            raise
        except Exception as error:
            raise BrokerPreflightError("Order not submitted: " + str(error)) from error
        symbol = str(order['symbol']).upper()
        instrument_id = payload['instrumentId']
        endpoint = "/api/v2/trading/execution/demo/orders"
        try:
            response = self._request_json("POST", endpoint, payload=payload, request_id=order.get("reference_id"))
        except EtoroAPIError as error:
            error.api_logs = list(self.api_logs)
            raise
        return {
            "provider": "etoro",
            "mode": "demo",
            "symbol": symbol,
            "broker_symbol": search_symbol(symbol),
            "instrument_id": instrument_id,
            "order_id": _broker_identifier(response, ("orderId", "orderID")),
            "position_id": _broker_identifier(response, ("positionId", "positionID")),
            "reference_id": order.get("reference_id"),
            "status": str(response.get("status") or (response.get("data") or {}).get("status") or "submitted"),
            "request": payload,
            "response": _redact_etoro_log_value(response),
            "api_logs": list(self.api_logs) if self.settings.etoro_api_debug_logging else [],
        }

    def get_positions(self) -> Dict[str, Any]:
        if not self.settings.etoro_demo_trading:
            raise ValueError("Live eToro account access is disabled in this application.")
        # The portfolio overview endpoint can return an aggregated snapshot
        # without a positions array. The demo P&L endpoint exposes the account
        # components (including positions) used by get_account_snapshot().
        payload = self._request_json("GET", "/api/v1/trading/info/demo/pnl")

        def find_positions(value: Any) -> Optional[list[Any]]:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if str(key).casefold() in {"positions", "openpositions", "positionlist"} and isinstance(nested, list):
                        return nested
                for nested in value.values():
                    result = find_positions(nested)
                    if result is not None:
                        return result
            elif isinstance(value, list):
                for nested in value:
                    result = find_positions(nested)
                    if result is not None:
                        return result
            return None

        positions = find_positions(payload)
        if not isinstance(positions, list):
            root_keys = sorted(str(key) for key in payload.keys()) if isinstance(payload, dict) else [type(payload).__name__]
            error = ValueError(
                "eToro demo P&L response did not contain a recognized positions list "
                f"(root fields: {', '.join(root_keys[:20]) or 'none'})."
            )
            error.api_logs = list(self.api_logs)
            raise error
        normalized = []
        for position in positions:
            if not isinstance(position, dict):
                continue
            normalized.append({
                "position_id": position.get("positionId") or position.get("positionID"),
                "opened_at": _broker_time(position.get("openTimestamp") or position.get("openDateTime")),
                "instrument_id": position.get("instrumentId") or position.get("instrumentID"),
                "symbol": str(position.get("internalSymbolFull") or position.get("symbol") or "").upper(),
                "units": _first_numeric_by_keys(position, ("units", "unitsAmount", "volume")),
                "side": ("LONG" if position.get("isBuy") is True else "SHORT" if position.get("isBuy") is False else {"buy": "LONG", "sellshort": "SHORT"}.get(str(position.get("transaction")).lower())) ,
                "amount": _first_numeric_by_keys(position, ("amount", "investedAmount")),
                "open_rate": _first_numeric_by_keys(position, ("openRate", "openPrice", "entryRate")),
                "current_rate": _first_numeric_by_keys(position, ("currentRate", "currentPrice", "rate")),
                "unrealized_pnl": _first_numeric_by_keys(position, ("unrealizedPnL", "pnL", "pnl", "PnL")),
                "leverage": _first_numeric_by_keys(position, ("leverage",)),
                "stop_loss_price": _first_numeric_by_keys(position, ("stopLossRate", "stopLossPrice", "stopLoss")),
                "take_profit_price": _first_numeric_by_keys(position, ("takeProfitRate", "takeProfitPrice", "takeProfit")),
                "no_take_profit": position.get("isNoTakeProfit"),
                "stop_loss_type": ("trailing" if position.get("isTslEnabled") is True else
                                   "fixed" if position.get("isTslEnabled") is False else
                                   position.get("stopLossType") if position.get("stopLossType") in {"fixed","trailing"} else None),
                "fees": _first_numeric_by_keys(position, ("totalFees", "totalExternalCosts", "fees", "openFee")),
                "raw": _redact_etoro_log_value(position),
            })
        return {"available": True, "provider": "eToro", "mode": "demo", "positions": normalized, "fetched_at": datetime.now(timezone.utc).isoformat()}

    def get_account_snapshot(self) -> Dict[str, Any]:
        """Compute demo equity from the documented eToro P&L response components."""
        if not self.settings.etoro_demo_trading:
            raise ValueError("Live eToro account access is disabled in this application.")
        payload = self._request_json("GET", "/api/v1/trading/info/demo/pnl")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if isinstance(data, dict) and isinstance(data.get("clientPortfolio"), dict):
            data = data["clientPortfolio"]
        required = ("credit", "positions", "ordersForOpen", "orders", "mirrors")
        if not isinstance(data, dict) or any(key not in data for key in required):
            return {"available": False, "provider": "eToro", "mode": "demo", "reason": "Demo P&L response is missing equity components."}
        if not all(isinstance(data[key], list) for key in required[1:]):
            return {"available": False, "provider": "eToro", "mode": "demo", "reason": "Demo P&L response contains invalid position/order collections."}
        credit = _coerce_money(data.get("credit"))
        if credit is None or credit < 0:
            return {"available": False, "provider": "eToro", "mode": "demo", "reason": "Demo P&L response has no valid credit balance."}

        def value(record: Dict[str, Any], key: str) -> Optional[float]:
            return _coerce_money(record.get(key))

        for item in [*data["positions"], *data["orders"], *data["ordersForOpen"]]:
            if not isinstance(item, dict) or value(item,"amount") is None or value(item,"amount") < 0:
                return {"available": False, "provider": "eToro", "mode": "demo", "reason": "A position or pending order has no verifiable capital amount."}
        manual_orders = [item for item in data["ordersForOpen"] if _coerce_money(item.get("mirrorID", item.get("mirrorId", 0))) == 0]
        pending_amount = sum(value(item, "amount") or 0 for item in manual_orders) + sum(value(item, "amount") or 0 for item in data["orders"])
        available_cash = credit - pending_amount
        invested = sum(value(item, "amount") or 0 for item in data["positions"])
        invested += sum(value(item, "amount") or 0 for item in data["orders"])
        invested += sum((value(item, "amount") or 0) + (value(item, "totalExternalCosts") or 0) for item in manual_orders)
        unrealized = 0.0
        for position in data["positions"]:
            pnl = position.get("unrealizedPnL") if position.get("unrealizedPnL") is not None else position.get("unrealizedPnl")
            pnl_value = _coerce_money(pnl.get("pnL", pnl.get("pnl", pnl.get("PnL")))) if isinstance(pnl, dict) else _coerce_money(pnl)
            if pnl_value is None:
                return {"available": False, "provider": "eToro", "mode": "demo", "reason": "A position has no verifiable unrealized P&L."}
            unrealized += pnl_value
        for mirror in data["mirrors"]:
            if not isinstance(mirror, dict) or not isinstance(mirror.get("positions", []), list):
                return {"available": False, "provider": "eToro", "mode": "demo", "reason": "Demo P&L response contains an invalid mirror portfolio."}
            invested += sum(value(item, "amount") or 0 for item in mirror.get("positions", []))
            invested += (value(mirror, "availableAmount") or 0) - (value(mirror, "closedPositionsNetProfit") or 0)
            unrealized += value(mirror, "closedPositionsNetProfit") or 0
            for position in mirror.get("positions", []):
                pnl = position.get("unrealizedPnL") if position.get("unrealizedPnL") is not None else position.get("unrealizedPnl")
                pnl_value = _coerce_money(pnl.get("pnL", pnl.get("pnl", pnl.get("PnL")))) if isinstance(pnl, dict) else _coerce_money(pnl)
                if pnl_value is None:
                    return {"available": False, "provider": "eToro", "mode": "demo", "reason": "A mirror position has no verifiable unrealized P&L."}
                unrealized += pnl_value
        equity = available_cash + invested + unrealized
        return {
            "available": math.isfinite(equity) and equity > 0,
            "provider": "eToro", "mode": "demo", "currency": "USD",
            "equity": equity, "cash_available": available_cash,
            "pending_order_count": len(data["ordersForOpen"])+len(data["orders"]),
            "unsupported_mirror_positions": sum(len(m.get("positions", [])) for m in data["mirrors"]),
            "unrealized_pnl": unrealized, "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "eToro demo P&L endpoint",
        }

    def get_closed_positions(self, min_date: Optional[str] = None) -> Dict[str, Any]:
        if not self.settings.etoro_demo_trading:
            raise ValueError("Live account history is disabled.")
        from datetime import timedelta
        min_date = min_date or (datetime.now(timezone.utc)-timedelta(days=90)).date().isoformat()
        records = []
        for page in range(1, 101):
            payload = self._request_json("GET", "/api/v1/trading/info/trade/demo/history",
                                         params={"minDate": min_date, "page": page, "pageSize": 100})
            batch = payload if isinstance(payload, list) else payload.get("items", payload.get("trades", payload.get("data")))
            if not isinstance(batch, list):
                raise ValueError("eToro history did not contain a closed-trades list.")
            for row in batch:
                records.append({"position_id": _broker_identifier(row, ("positionId", "positionID")),
                                "closed_at": _broker_time(row.get("closeTimestamp")), "close_rate": _coerce_money(row.get("closeRate")),
                                "net_profit": _coerce_optional_float(row.get("netProfit")),
                                "fees": _coerce_optional_float(row.get("fees")),
                                "units": _coerce_optional_float(row.get("units")), "raw": _redact_etoro_log_value(row)})
            if len(batch) < 100:
                return {"available": True, "positions": records, "complete": True}
        return {"available": True, "positions": records, "complete": False}

    def lookup_order(self, *, order_id=None, reference_id=None) -> Dict[str, Any]:
        if not self.settings.etoro_demo_trading:
            raise ValueError("Live order lookup is disabled.")
        params = {"orderId": str(order_id).removesuffix(".0")} if order_id else {"referenceId": reference_id}
        if not order_id and not reference_id:
            raise ValueError("An order or persisted reference ID is required.")
        return self._request_json("GET", "/api/v2/trading/info/demo/orders:lookup", params=params)

    def cancel_pending_order(self, order_id, *, request_id=None):
        if not self.settings.etoro_demo_trading:
            raise ValueError("Live order cancellation is disabled.")
        oid=str(order_id).removesuffix('.0')
        if not oid.isdigit():raise ValueError('A verified numeric demo order ID is required.')
        return self._request_json('DELETE',f'/api/v2/trading/execution/demo/orders/{oid}',
            **({'request_id':request_id} if request_id else {}))

    def modify_position(self, position_id: str, stop: float, target: Optional[float], *, request_id=None, stop_type="fixed") -> Dict[str, Any]:
        if not self.settings.etoro_demo_trading:
            raise ValueError("Live position modification is disabled.")
        if stop_type not in {'fixed','trailing'}:raise ValueError('Unsupported stop-loss type')
        if not math.isfinite(float(stop)) or float(stop)<=0:raise ValueError('Invalid stop-loss rate')
        payload = {"stopLossRate": stop, "stopLossType": stop_type}
        if target is not None:
            payload["takeProfitRate"] = target
        return self._request_json("PATCH", f"/api/v2/trading/demo/positions/{str(position_id).removesuffix('.0')}", payload=payload, **({"request_id": request_id} if request_id else {}))

    def estimate_order_costs(self, symbol: str, side: str, units: float, leverage: int) -> Dict[str, Any]:
        if not self.settings.etoro_demo_trading:
            raise ValueError("Live cost queries are disabled.")
        instrument=self._find_instrument(symbol)
        execution_symbol=str(instrument.get('internalSymbolFull') or search_symbol(symbol)).upper()
        return self._request_json("POST", "/api/v2/trading/info/demo/costs", payload={
            "action": "open", "transaction": "buy" if side == "LONG" else "sellShort",
            "symbol": execution_symbol, "settlementType": "cfd", "orderType": "mkt",
            "leverage": leverage, "units": units, "orderCurrency": "usd"})

    def lookup_close_order(self, order_id):
        if not self.settings.etoro_demo_trading:
            raise ValueError("Live close-order lookup is disabled.")
        if not str(order_id).strip():
            raise ValueError("A close order ID is required.")
        return self._request_json("GET", f"/api/v1/trading/info/demo/close-orders/{str(order_id).removesuffix('.0')}")

    def close_position(self, position_id: str, instrument_id=None, *, request_id=None) -> Dict[str, Any]:
        if not self.settings.etoro_demo_trading:
            raise ValueError("Live eToro execution is disabled in this application.")
        if not str(position_id).strip():
            raise ValueError("A broker position ID is required to close an eToro position.")
        position_id = str(position_id).removesuffix(".0")
        if instrument_id is None:
            snapshot = self.get_positions()
            matches = [p for p in snapshot.get("positions", [])
                       if str(p.get("position_id")).removesuffix(".0") == position_id]
            if not snapshot.get("available") or len(matches) != 1:
                raise ValueError("A unique open broker position is required to resolve its instrument ID.")
            instrument_id = matches[0].get("instrument_id")
        try:
            number = float(instrument_id)
            if not math.isfinite(number) or number <= 0 or not number.is_integer():
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError("A valid broker instrument ID is required to close the position.") from None
        response = self._request_json(
            "POST",
            f"/api/v1/trading/execution/demo/market-close-orders/positions/{position_id}",
            payload={"InstrumentID": int(number), "UnitsToDeduct": None},
            **({"request_id": request_id} if request_id else {}),
        )
        return {
            "provider": "eToro", "mode": "demo", "position_id": str(position_id),
            "order_id": response.get("orderId") or (response.get("orderForClose") or {}).get("orderID") or (response.get("data") or {}).get("orderId"),
            "response": _redact_etoro_log_value(response),
        }

    def validate_credentials(self, symbol: str = "AAPL") -> Dict[str, Any]:
        settings = self.settings
        if not settings.etoro_api_key or not settings.etoro_user_key:
            return {
                "available": False,
                "status": "missing_credentials",
                "message": "ETORO_API_KEY and ETORO_USER_KEY are both required.",
                "symbol": symbol.upper(),
            }
        try:
            adapter = self
            checks: Dict[str, Any] = {}

            try:
                instrument = adapter._find_instrument(symbol)
                instrument_id = _etoro_instrument_id(instrument)
                rate = adapter._get_rate(instrument_id)
                checks["market_data"] = {
                    "available": True,
                    "symbol": symbol.upper(),
                    "instrument_id": instrument_id,
                    "quote_symbol": str(instrument.get("internalSymbolFull") or symbol).upper(),
                    "quote_type": rate.get("quoteType"),
                    "bid": _coerce_optional_float(rate.get("bid")),
                    "ask": _coerce_optional_float(rate.get("ask")),
                }
            except Exception as error:
                checks["market_data"] = {
                    "available": False,
                    "symbol": symbol.upper(),
                    "error": str(error),
                }

            try:
                demo_portfolio = adapter._request_json("GET", "/api/v1/trading/info/demo/portfolio")
                checks["demo_portfolio"] = {"available": True, "payload": demo_portfolio}
            except Exception as error:
                checks["demo_portfolio"] = {"available": False, "error": str(error)}

            if settings.etoro_demo_account_id:
                try:
                    demo_balance = adapter._request_json(
                        "GET",
                        f"/api/v1/balances/demo/{settings.etoro_demo_account_id}",
                        params={"displayCurrency": "USD", "expand": "equityDetails"},
                    )
                    checks["demo_balance"] = {"available": True, "payload": demo_balance}
                except Exception as error:
                    checks["demo_balance"] = {"available": False, "error": str(error)}
            else:
                checks["demo_balance"] = {
                    "available": False,
                    "skipped": True,
                    "message": "ETORO_DEMO_ACCOUNT_ID is not configured.",
                }

            overall_available = bool(checks["market_data"]["available"] and checks["demo_portfolio"]["available"])
            return {
                "available": overall_available,
                "status": "complete" if overall_available else "partial_failure",
                "message": (
                    f"Authenticated eToro access confirmed for {symbol.upper()} and the demo portfolio."
                    if overall_available else
                    "Some eToro read-only checks failed; see endpoint details."
                ),
                "symbol": symbol.upper(),
                "checks": checks,
                "source": "eToro Public API",
            }
        except Exception as error:
            logger.warning("eToro credential validation failed for %s: %s", symbol, type(error).__name__)
            return {
                "available": False,
                "status": "error",
                "message": str(error),
                "symbol": symbol.upper(),
                "source": "eToro Public API",
            }

    def get_portfolio_balance(self, selected_source="etoro_demo"):
        if selected_source == "etoro_demo":
            payload = self._request_json("GET", "/api/v1/trading/info/demo/portfolio")
            return _normalize_portfolio_balance(payload, source=selected_source, provider="eToro")
        if selected_source == "etoro_main":
            if not self.settings.etoro_main_account_id:
                return {
                    "available": False,
                    "provider": "eToro",
                    "source": selected_source,
                    "message": "ETORO_MAIN_ACCOUNT_ID is not configured.",
                }
            payload = self._request_json(
                "GET",
                f"/api/v1/balances/trading/{self.settings.etoro_main_account_id}",
                params={"displayCurrency": "USD", "expand": "equityDetails"},
            )
            return _normalize_portfolio_balance(payload, source=selected_source, provider="eToro")
        return {
            "available": False,
            "provider": "eToro",
            "source": selected_source,
            "message": f"Unsupported eToro portfolio source: {selected_source}",
        }

