"""Research and broker provider boundaries for the local paper-trading MVP."""

import asyncio
import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yfinance as yf
import yfscreen as yfs
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntegrationSettings:
    research_provider: str
    news_provider: str
    news_api_base_url: str
    news_api_key: str
    news_interval_seconds: int
    news_symbol_limit: int
    news_fallback_tickers: tuple[str, ...]
    openai_base_url: str
    openai_api_key: str
    openai_admin_key: str
    openai_organization_id: str
    openai_model: str
    broker_provider: str
    ibkr_host: str
    ibkr_port: int
    ibkr_client_id: int
    ibkr_account_id: str
    ibkr_paper_trading: bool

    @classmethod
    def from_environment(cls) -> "IntegrationSettings":
        return cls(
            research_provider=os.getenv("FINBOT_RESEARCH_PROVIDER", "yfscreen").lower(),
            news_provider=os.getenv("FINBOT_NEWS_PROVIDER", "yfinance").lower(),
            news_api_base_url=os.getenv("NEWS_API_BASE_URL", ""),
            news_api_key=os.getenv("NEWS_API_KEY", ""),
            news_interval_seconds=max(60, int(os.getenv("FINBOT_NEWS_INTERVAL_SECONDS", "3600"))),
            news_symbol_limit=max(1, int(os.getenv("FINBOT_NEWS_SYMBOL_LIMIT", "8"))),
            news_fallback_tickers=tuple(
                ticker.strip().upper()
                for ticker in os.getenv("FINBOT_NEWS_TICKERS", "AAPL,MSFT,NVDA,AMD,META,XOM").split(",")
                if ticker.strip()
            ),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_admin_key=os.getenv("OPENAI_ADMIN_KEY", ""),
            openai_organization_id=os.getenv("OPENAI_ORGANIZATION_ID", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            broker_provider=os.getenv("FINBOT_BROKER_PROVIDER", "dummy").lower(),
            ibkr_host=os.getenv("IBKR_HOST", "127.0.0.1"),
            ibkr_port=int(os.getenv("IBKR_PORT", "7497")),
            ibkr_client_id=int(os.getenv("IBKR_CLIENT_ID", "1")),
            ibkr_account_id=os.getenv("IBKR_ACCOUNT_ID", ""),
            ibkr_paper_trading=os.getenv("IBKR_PAPER_TRADING", "true").lower() in {"1", "true", "yes", "on"},
        )


async def fetch_company_fundamentals(ticker: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_fetch_company_fundamentals, ticker)


async def fetch_market_quote(ticker: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_fetch_market_quote, ticker)


async def fetch_market_trends(ticker: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_fetch_market_trends, ticker)


async def validate_trade_price_path(
    ticker: str,
    side: str,
    entry_price: float,
    stop_loss_price: float,
    take_profit_price: float,
    placed_at: str,
    max_days: int = 14,
) -> Dict[str, Any]:
    return await asyncio.to_thread(
        _validate_trade_price_path,
        ticker,
        side,
        entry_price,
        stop_loss_price,
        take_profit_price,
        placed_at,
        max_days,
    )


def _validate_trade_price_path(
    ticker: str,
    side: str,
    entry_price: float,
    stop_loss_price: float,
    take_profit_price: float,
    placed_at: str,
    max_days: int,
) -> Dict[str, Any]:
    yahoo_symbol = {"USOIL": "CL=F"}.get(ticker.upper(), ticker.upper())
    try:
        start_at = datetime.fromisoformat(placed_at.replace("Z", "+00:00"))
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=timezone.utc)
        start_at = start_at.astimezone(timezone.utc)
        days = min(14, max(1, int(max_days)))
        requested_end = start_at + timedelta(days=days)
        sampled_end = min(datetime.now(timezone.utc), requested_end)
        if sampled_end <= start_at:
            raise ValueError("The selected trade timestamp is not in the past.")
        if side not in {"LONG", "SHORT"}:
            raise ValueError("Historical validation supports LONG and SHORT trades only.")
        if not all(math.isfinite(float(price)) and float(price) > 0 for price in (
            entry_price, stop_loss_price, take_profit_price
        )):
            raise ValueError("Entry, stop-loss, and take-profit prices must be positive.")

        bars = yf.Ticker(yahoo_symbol).history(
            start=start_at,
            end=sampled_end,
            interval="5m",
            auto_adjust=False,
            actions=False,
        )
        if bars is None or bars.empty:
            return {
                "available": False,
                "ticker": ticker.upper(),
                "quote_symbol": yahoo_symbol,
                "placed_at": start_at.isoformat(),
                "sampled_through": sampled_end.isoformat(),
                "window_days": days,
                "reason": "Yahoo Finance returned no 5-minute bars for this trade window.",
            }

        bars = bars.copy()
        if bars.index.tz is None:
            bars.index = bars.index.tz_localize("UTC")
        else:
            bars.index = bars.index.tz_convert("UTC")
        bars = bars.sort_index()
        bars = bars[(bars.index > start_at) & (bars.index <= sampled_end)]
        if bars.empty:
            return {
                "available": False,
                "ticker": ticker.upper(),
                "quote_symbol": yahoo_symbol,
                "placed_at": start_at.isoformat(),
                "sampled_through": sampled_end.isoformat(),
                "window_days": days,
                "reason": "No complete 5-minute bars were available after the trade time.",
            }

        first_stop = None
        first_target = None
        ambiguous_same_bar = False
        favorable_prices = []
        adverse_prices = []
        for index, bar in bars.iterrows():
            high = float(bar["High"])
            low = float(bar["Low"])
            stop_hit = low <= stop_loss_price if side == "LONG" else high >= stop_loss_price
            target_hit = high >= take_profit_price if side == "LONG" else low <= take_profit_price
            favorable_prices.append(high if side == "LONG" else low)
            adverse_prices.append(low if side == "LONG" else high)
            if stop_hit and first_stop is None:
                first_stop = index
            if target_hit and first_target is None:
                first_target = index
            if stop_hit and target_hit:
                ambiguous_same_bar = True

        if first_stop is None and first_target is None:
            first_exit, first_exit_at, first_exit_price = "neither_hit", None, None
        elif first_stop is not None and first_target is not None and first_stop == first_target:
            first_exit, first_exit_at, first_exit_price = (
                "ambiguous_same_bar_stop_assumed", first_stop.isoformat(), float(stop_loss_price)
            )
        elif first_target is not None and (first_stop is None or first_target < first_stop):
            first_exit, first_exit_at, first_exit_price = (
                "take_profit", first_target.isoformat(), float(take_profit_price)
            )
        else:
            first_exit, first_exit_at, first_exit_price = (
                "stop_loss", first_stop.isoformat(), float(stop_loss_price)
            )

        stop_after_target = None
        if first_target is not None:
            for index, bar in bars[bars.index > first_target].iterrows():
                breached = float(bar["Low"]) <= stop_loss_price if side == "LONG" else float(bar["High"]) >= stop_loss_price
                if breached:
                    stop_after_target = index
                    break

        final_close = float(bars["Close"].dropna().iloc[-1])
        direction = 1.0 if side == "LONG" else -1.0
        target_return = (take_profit_price - entry_price) / entry_price * direction * 100
        stop_return = (stop_loss_price - entry_price) / entry_price * direction * 100
        window_return = (final_close - entry_price) / entry_price * direction * 100
        simulated_exit_return = (
            target_return
            if first_exit == "take_profit"
            else stop_return
            if first_exit in {"stop_loss", "ambiguous_same_bar_stop_assumed"}
            else window_return
        )
        favorable_return = (
            (max(favorable_prices) - entry_price) / entry_price * 100
            if side == "LONG" else (entry_price - min(favorable_prices)) / entry_price * 100
        )
        adverse_return = (
            (min(adverse_prices) - entry_price) / entry_price * 100
            if side == "LONG" else (entry_price - max(adverse_prices)) / entry_price * 100
        )
        return {
            "available": True,
            "ticker": ticker.upper(),
            "quote_symbol": yahoo_symbol,
            "side": side,
            "placed_at": start_at.isoformat(),
            "sampled_through": bars.index[-1].isoformat(),
            "window_days": days,
            "window_complete": sampled_end >= requested_end,
            "bar_interval": "5m",
            "bars_analyzed": int(len(bars)),
            "entry_price": float(entry_price),
            "stop_loss_price": float(stop_loss_price),
            "take_profit_price": float(take_profit_price),
            "first_exit": first_exit,
            "first_exit_at": first_exit_at,
            "first_exit_price": first_exit_price,
            "same_bar_stop_and_target_ambiguity": ambiguous_same_bar,
            "take_profit_return_pct": target_return,
            "stop_loss_return_pct": stop_return,
            "max_favorable_excursion_pct": favorable_return,
            "max_adverse_excursion_pct": adverse_return,
            "window_end_close": final_close,
            "return_if_held_to_window_end_pct": window_return,
            "simulated_barrier_exit_return_pct": simulated_exit_return,
            "stop_loss_breached_after_take_profit": stop_after_target is not None,
            "stop_loss_breach_after_take_profit_at": stop_after_target.isoformat() if stop_after_target is not None else None,
            "return_if_held_to_original_stop_after_target_pct": stop_return if stop_after_target is not None else None,
            "data_source": "Yahoo Finance 5-minute OHLC history",
        }
    except Exception as error:
        logger.warning("Trade path validation unavailable for %s: %s", ticker, type(error).__name__)
        return {
            "available": False,
            "ticker": ticker.upper(),
            "quote_symbol": yahoo_symbol,
            "reason": f"Could not validate the historical trade path ({type(error).__name__}).",
        }


def _fetch_market_trends(ticker: str) -> Dict[str, Any]:
    yahoo_symbol = {"USOIL": "CL=F"}.get(ticker.upper(), ticker.upper())
    now = datetime.now(timezone.utc)
    unavailable = {"available": False, "reason": "Insufficient recent price history."}
    try:
        company = yf.Ticker(yahoo_symbol)
        intraday = company.history(period="5d", interval="5m", auto_adjust=True)
        daily = company.history(period="5y", interval="1d", auto_adjust=True)
        if intraday is None or intraday.empty:
            intraday = None
        if daily is None or daily.empty:
            daily = None
    except Exception as error:
        logger.warning("Yahoo Finance trend history unavailable for %s: %s", ticker, type(error).__name__)
        return {
            "ticker": ticker.upper(),
            "quote_symbol": yahoo_symbol,
            "data_source": "Yahoo Finance adjusted historical prices",
            "fetched_at": now.isoformat(),
            "available": False,
            "windows": {},
            "unavailable_reason": "Yahoo Finance price history could not be retrieved.",
        }

    def utc_frame(frame: Any) -> Any:
        if frame is None:
            return None
        result = frame.copy()
        if result.index.tz is None:
            result.index = result.index.tz_localize("UTC")
        else:
            result.index = result.index.tz_convert("UTC")
        return result.sort_index()

    intraday = utc_frame(intraday)
    daily = utc_frame(daily)

    def summarize(frame: Any, start_at: datetime) -> Dict[str, Any]:
        if frame is None or "Close" not in frame.columns:
            return unavailable.copy()
        selected = frame.loc[frame.index >= start_at]
        close = selected["Close"].dropna()
        if len(close) < 2:
            return unavailable.copy()
        first_price = float(close.iloc[0])
        last_price = float(close.iloc[-1])
        if first_price <= 0 or not math.isfinite(first_price) or not math.isfinite(last_price):
            return unavailable.copy()
        high = float(selected["High"].max()) if "High" in selected else max(first_price, last_price)
        low = float(selected["Low"].min()) if "Low" in selected else min(first_price, last_price)
        volume = float(selected["Volume"].fillna(0).sum()) if "Volume" in selected else None
        return {
            "available": True,
            "start_at": close.index[0].isoformat(),
            "end_at": close.index[-1].isoformat(),
            "bars": int(len(close)),
            "start_price": first_price,
            "end_price": last_price,
            "change_pct": (last_price - first_price) / first_price * 100,
            "high": high,
            "low": low,
            "volume": volume,
        }

    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    windows = {
        "last_1h": summarize(intraday, now - timedelta(hours=1)),
        "last_24h": summarize(intraday, now - timedelta(hours=24)),
        "week_to_date": summarize(daily, week_start),
        "year_to_date": summarize(daily, year_start),
        "last_5_years": summarize(daily, now - timedelta(days=365 * 5)),
    }
    return {
        "ticker": ticker.upper(),
        "quote_symbol": yahoo_symbol,
        "data_source": "Yahoo Finance adjusted historical prices",
        "fetched_at": now.isoformat(),
        "available": any(window["available"] for window in windows.values()),
        "windows": windows,
    }


def _fetch_market_quote(ticker: str) -> Dict[str, Any]:
    yahoo_symbol = {"USOIL": "CL=F"}.get(ticker.upper(), ticker.upper())
    fetched_at = datetime.now(timezone.utc).isoformat()
    try:
        company = yf.Ticker(yahoo_symbol)
        fast_info = company.fast_info
        price = None
        for key in ("lastPrice", "last_price", "regularMarketPrice", "currentPrice"):
            try:
                price = fast_info.get(key)
            except (AttributeError, KeyError, TypeError):
                price = getattr(fast_info, key, None)
            if price is not None:
                break
        if price is None:
            info = company.info or {}
            price = info.get("currentPrice") or info.get("regularMarketPrice")
        price = float(price)
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Yahoo Finance returned a non-positive quote")
        return {
            "ticker": ticker.upper(),
            "quote_symbol": yahoo_symbol,
            "price": price,
            "currency": getattr(fast_info, "currency", None),
            "source": "Yahoo Finance",
            "fetched_at": fetched_at,
            "available": True,
        }
    except Exception as error:
        logger.warning("Yahoo Finance quote unavailable for %s: %s", ticker, type(error).__name__)
        return {
            "ticker": ticker.upper(),
            "quote_symbol": yahoo_symbol,
            "price": None,
            "source": "Yahoo Finance",
            "fetched_at": fetched_at,
            "available": False,
        }


def _fetch_company_fundamentals(ticker: str) -> Dict[str, Any]:
    try:
        company = yf.Ticker(ticker)
        info = company.info or {}
        quarterly_income = company.quarterly_income_stmt
        annual_income = company.income_stmt
        quarterly_cashflow = company.quarterly_cashflow
        annual_cashflow = company.cashflow
        balance_sheet = company.balance_sheet
    except Exception as error:
        logger.warning("Yahoo Finance fundamentals unavailable for %s: %s", ticker, type(error).__name__)
        return {
            "ticker": ticker,
            "data_status": "unavailable",
            "unavailable_reason": "Yahoo Finance did not return company financial statements.",
        }

    def numeric_value(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def statement_values(statement: Any, labels: tuple[str, ...]) -> list[Optional[float]]:
        if statement is None or getattr(statement, "empty", True):
            return []
        aliases = {
            "".join(character.lower() for character in label if character.isalnum())
            for label in labels
        }
        columns = sorted(statement.columns, reverse=True)
        for row_label in statement.index:
            normalized = "".join(
                character.lower() for character in str(row_label) if character.isalnum()
            )
            if normalized in aliases:
                return [numeric_value(statement.loc[row_label, column]) for column in columns]
        return []

    def latest_total(
        quarterly_values: list[Optional[float]], annual_values: list[Optional[float]],
    ) -> Optional[float]:
        latest_quarters = quarterly_values[:4]
        if len(latest_quarters) == 4 and all(value is not None for value in latest_quarters):
            return sum(value for value in latest_quarters if value is not None)
        return annual_values[0] if annual_values else None

    def ttm_and_prior(
        quarterly_values: list[Optional[float]], annual_values: list[Optional[float]],
    ) -> tuple[Optional[float], Optional[float]]:
        latest_quarters = quarterly_values[:4]
        previous_quarters = quarterly_values[4:8]
        if (
            len(latest_quarters) == 4
            and len(previous_quarters) == 4
            and all(value is not None for value in latest_quarters + previous_quarters)
        ):
            return (
                sum(value for value in latest_quarters if value is not None),
                sum(value for value in previous_quarters if value is not None),
            )
        if len(annual_values) >= 2 and annual_values[0] is not None and annual_values[1] is not None:
            return annual_values[0], annual_values[1]
        return latest_total(quarterly_values, annual_values), None

    revenue_labels = ("Total Revenue", "Operating Revenue", "Revenue")
    net_income_labels = (
        "Net Income", "Net Income Common Stockholders",
        "Net Income From Continuing Operation Net Minority Interest",
    )
    revenue_quarterly = statement_values(quarterly_income, revenue_labels)
    revenue_annual = statement_values(annual_income, revenue_labels)
    revenue_ttm, prior_revenue = ttm_and_prior(revenue_quarterly, revenue_annual)
    if revenue_ttm is None:
        revenue_ttm = numeric_value(info.get("totalRevenue"))
    revenue_growth = None
    if revenue_ttm is not None and prior_revenue not in (None, 0):
        revenue_growth = (revenue_ttm - prior_revenue) / abs(prior_revenue) * 100
    elif info.get("revenueGrowth") is not None:
        growth_value = numeric_value(info.get("revenueGrowth"))
        revenue_growth = growth_value * 100 if growth_value is not None else None

    net_income_ttm = latest_total(
        statement_values(quarterly_income, net_income_labels),
        statement_values(annual_income, net_income_labels),
    )
    if net_income_ttm is not None and revenue_ttm not in (None, 0):
        net_profit_margin = net_income_ttm / revenue_ttm * 100
    else:
        profit_margin_ratio = numeric_value(info.get("profitMargins"))
        net_profit_margin = profit_margin_ratio * 100 if profit_margin_ratio is not None else None

    cashflow_labels = ("Free Cash Flow", "FreeCashFlow")
    free_cash_flow_ttm = latest_total(
        statement_values(quarterly_cashflow, cashflow_labels),
        statement_values(annual_cashflow, cashflow_labels),
    )
    if free_cash_flow_ttm is None:
        operating_cash_flow = latest_total(
            statement_values(quarterly_cashflow, ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities")),
            statement_values(annual_cashflow, ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities")),
        )
        capital_expenditure = latest_total(
            statement_values(quarterly_cashflow, ("Capital Expenditure", "Capital Expenditures")),
            statement_values(annual_cashflow, ("Capital Expenditure", "Capital Expenditures")),
        )
        if operating_cash_flow is not None and capital_expenditure is not None:
            free_cash_flow_ttm = (
                operating_cash_flow + capital_expenditure
                if capital_expenditure <= 0
                else operating_cash_flow - capital_expenditure
            )
    if free_cash_flow_ttm is None:
        free_cash_flow_ttm = numeric_value(info.get("freeCashflow"))

    debt_values = statement_values(balance_sheet, ("Total Debt",))
    if debt_values and debt_values[0] is not None:
        total_debt = debt_values[0]
    else:
        current_debt = statement_values(balance_sheet, ("Current Debt", "Current Debt And Capital Lease Obligation"))
        long_term_debt = statement_values(balance_sheet, ("Long Term Debt", "Long Term Debt And Capital Lease Obligation"))
        debt_parts = [values[0] for values in (current_debt, long_term_debt) if values and values[0] is not None]
        total_debt = sum(debt_parts) if debt_parts else None
    equity_values = statement_values(
        balance_sheet,
        ("Stockholders Equity", "Total Stockholder Equity", "Common Stock Equity"),
    )
    equity = equity_values[0] if equity_values else None
    debt_to_equity = (
        total_debt / equity
        if total_debt is not None and equity not in (None, 0)
        else None
    )
    if debt_to_equity is None and info.get("debtToEquity") is not None:
        info_ratio = numeric_value(info.get("debtToEquity"))
        debt_to_equity = info_ratio / 100 if info_ratio is not None else None

    price = info.get("currentPrice")
    if price is None:
        price = info.get("regularMarketPrice")
    result = {
        "ticker": ticker,
        "company_name": info.get("longName") or info.get("shortName"),
        "currency": info.get("currency"),
        "share_price": numeric_value(price),
        "trailing_pe": numeric_value(info.get("trailingPE")),
        "forward_pe": numeric_value(info.get("forwardPE")),
        "trailing_eps": numeric_value(info.get("trailingEps")),
        "forward_eps": numeric_value(info.get("forwardEps")),
        "total_revenue_ttm": revenue_ttm,
        "revenue_yoy_growth_pct": revenue_growth,
        "net_profit_margin_pct": net_profit_margin,
        "free_cash_flow_ttm": free_cash_flow_ttm,
        "debt_to_equity_ratio": debt_to_equity,
        "data_source": "Yahoo Finance financial statements and quote data",
    }
    result["data_status"] = "available" if any(
        result[key] is not None
        for key in (
            "total_revenue_ttm", "revenue_yoy_growth_pct", "net_profit_margin_pct",
            "free_cash_flow_ttm", "debt_to_equity_ratio",
        )
    ) else "unavailable"
    if result["data_status"] == "unavailable":
        result["unavailable_reason"] = "No company financial statements were returned for this ticker."
    return result


async def fetch_openai_month_to_date_costs() -> Dict[str, Any]:
    settings = IntegrationSettings.from_environment()
    if not settings.openai_admin_key or not settings.openai_organization_id:
        return {
            "available": False,
            "message": "Organization spend requires OPENAI_ADMIN_KEY and OPENAI_ORGANIZATION_ID. A project API key cannot read billing costs.",
        }

    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return await asyncio.to_thread(
        _fetch_openai_costs,
        settings,
        int(start.timestamp()),
        int(now.timestamp()),
    )


def _fetch_openai_costs(
    settings: IntegrationSettings, start_time: int, end_time: int,
) -> Dict[str, Any]:
    base_url = settings.openai_base_url.rstrip("/")
    query: Dict[str, Any] = {
        "start_time": start_time,
        "end_time": end_time,
        "bucket_width": "1d",
        "limit": 31,
    }
    daily_costs: Dict[str, float] = {}
    try:
        for _ in range(10):
            url = f"{base_url}/organization/costs?{urlencode(query)}"
            request = Request(
                url,
                headers={
                    "Authorization": f"Bearer {settings.openai_admin_key}",
                    "OpenAI-Organization": settings.openai_organization_id,
                },
            )
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            for bucket in payload.get("data", []):
                date = datetime.fromtimestamp(bucket["start_time"], timezone.utc).date().isoformat()
                amount = sum(
                    float(result.get("amount", {}).get("value", 0))
                    if isinstance(result.get("amount"), dict)
                    else float(result.get("amount", 0))
                    for result in bucket.get("results", [])
                )
                daily_costs[date] = daily_costs.get(date, 0.0) + amount
            next_page = payload.get("next_page")
            if not payload.get("has_more") or not next_page:
                break
            query["page"] = next_page
    except HTTPError as error:
        logger.warning("OpenAI organization costs request failed with HTTP %s", error.code)
        return {"available": False, "message": f"OpenAI costs API returned HTTP {error.code}; verify the admin key and organization ID."}
    except (URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as error:
        logger.warning("OpenAI organization costs request failed: %s", type(error).__name__)
        return {"available": False, "message": "Could not retrieve OpenAI organization costs. Check connection and billing API permissions."}

    daily = [{"date": date, "amount_usd": amount} for date, amount in sorted(daily_costs.items())]
    return {
        "available": True,
        "period_start": start_time,
        "period_end": end_time,
        "total_usd": sum(daily_costs.values()),
        "daily": daily,
        "message": "Month-to-date organization costs. This is usage spend, not remaining prepaid balance.",
    }


class ResearchProvider(Protocol):
    async def research(self, subject: Dict[str, Any]) -> Dict[str, Any]: ...


class NewsProvider(Protocol):
    async def fetch_news(self, subject: Dict[str, Any] | None = None) -> list[Dict[str, Any]]: ...


class YahooFinanceNewsProvider:
    def __init__(self, settings: IntegrationSettings) -> None:
        self.settings = settings

    async def fetch_news(self, subject: Dict[str, Any] | None = None) -> list[Dict[str, Any]]:
        tickers = await asyncio.to_thread(self._screen_tickers)
        if not tickers:
            tickers = list(self.settings.news_fallback_tickers)
        tickers = tickers[: self.settings.news_symbol_limit]
        return await asyncio.to_thread(self._fetch_ticker_news, tickers)

    def _screen_tickers(self) -> list[str]:
        try:
            filters = [["eq", ["region", "us"]], ["gt", ["dayvolume", 1_000_000]]]
            query = yfs.create_query(filters=filters)
            payload = yfs.create_payload("equity", query, size=self.settings.news_symbol_limit)
            frame = yfs.get_data(payload)
            if frame is None or frame.empty:
                return []
            symbol_column = next((name for name in ("ticker", "symbol") if name in frame.columns), None)
            if not symbol_column:
                logger.warning("yfscreen response did not contain a ticker column")
                return []
            return list(dict.fromkeys(frame[symbol_column].dropna().astype(str).str.upper().tolist()))
        except Exception:
            logger.exception("yfscreen symbol screening failed; using configured Yahoo tickers")
            return []

    @staticmethod
    def _fetch_ticker_news(tickers: list[str]) -> list[Dict[str, Any]]:
        articles: Dict[str, Dict[str, Any]] = {}
        for ticker in tickers:
            try:
                search_results = yf.Search(
                    ticker, max_results=8, news_count=5, lists_count=0
                ).news or []
                if not search_results:
                    search_results = yf.Ticker(ticker).news or []
                for item in search_results:
                    content = item.get("content") or item
                    title = content.get("title") or item.get("title")
                    if not title:
                        continue
                    url_data = (
                        content.get("canonicalUrl")
                        or content.get("clickThroughUrl")
                        or content.get("link")
                        or item.get("link")
                        or {}
                    )
                    url = url_data.get("url", "") if isinstance(url_data, dict) else str(url_data)
                    published_at = content.get("pubDate") or content.get("displayTime")
                    if not published_at and content.get("providerPublishTime"):
                        published_at = datetime.fromtimestamp(
                            content["providerPublishTime"], timezone.utc
                        ).isoformat()
                    source_data = content.get("provider") or item.get("publisher") or "Yahoo Finance"
                    source = source_data.get("displayName", "Yahoo Finance") if isinstance(source_data, dict) else str(source_data)
                    key = url or str(content.get("id") or hashlib.sha256(f"{ticker}:{title}".encode()).hexdigest())
                    articles[key] = {
                        "article_key": key,
                        "ticker": ticker,
                        "title": str(title),
                        "summary": str(content.get("summary") or content.get("description") or ""),
                        "source": source,
                        "url": url,
                        "published_at": published_at,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    }
            except Exception:
                logger.exception("Yahoo Finance news fetch failed for %s", ticker)
        return list(articles.values())


class BrokerAdapter(Protocol):
    def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]: ...

    def get_positions(self) -> Dict[str, Any]: ...


class OpenAIResearchAdapter:
    def __init__(
        self,
        settings: IntegrationSettings,
        usage_recorder: Optional[Callable[[str, Dict[str, int], Dict[str, str]], None]] = None,
    ) -> None:
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when the OpenAI provider is selected.")
        self.settings = settings
        self.usage_recorder = usage_recorder

    async def research(self, subject: Dict[str, Any]) -> Dict[str, Any]:
        headlines = [
            {
                "ticker": str(item.get("ticker", "")),
                "title": str(item.get("title", ""))[:400],
                "summary": str(item.get("summary", ""))[:1000],
                "source": str(item.get("source", "")),
                "published_at": item.get("published_at"),
            }
            for item in subject.get("headlines", [])[:8]
        ]
        research_input = {
            "company_name": subject.get("company_name", ""),
            "ticker": subject.get("ticker", ""),
            "sector": subject.get("sector", ""),
            "dummy_signal": subject.get("dummy_signal", "neutral"),
            "dummy_confidence": subject.get("dummy_confidence", 0.0),
            "background": str(subject.get("background", ""))[:1000],
            "headlines": headlines,
            "fundamentals": subject.get("fundamentals", {}),
        }
        async with AsyncOpenAI(
            api_key=self.settings.openai_api_key,
            base_url=self.settings.openai_base_url,
            timeout=45.0,
            max_retries=1,
        ) as client:
            raw_response = await client.chat.completions.with_raw_response.create(
                model=self.settings.openai_model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Analyze only the supplied company context and headlines. Do not invent facts. "
                            "Return one JSON object with signal (bullish, bearish, or neutral), confidence "
                            "(number from 0 to 1), catalyst (short label), summary (brief evidence-based text), "
                            "fundamental_analysis (brief analysis of supplied company financials; state when values "
                            "are unavailable), fundamental_grade (one of okay, normal, not okay, bad, unknown), and "
                            "fundamental_grades (object with revenue, net_profit_margin, free_cash_flow, and "
                            "debt_to_equity keys, each using the same grade choices). Use okay for strong, normal "
                            "for typical or mixed, not okay for concerning, bad for materially weak, and unknown "
                            "when unavailable. Grade each metric in context and do not infer missing values. Do not "
                            "invent or reinterpret numeric market data; use only supplied fundamentals. "
                            "This is research context only, not an order or financial advice."
                        ),
                    },
                    {"role": "user", "content": json.dumps(research_input, ensure_ascii=True)},
                ],
            )
            response = raw_response.parse()

        usage = response.usage
        token_usage = {
            "prompt_tokens": int(usage.prompt_tokens or 0) if usage else 0,
            "completion_tokens": int(usage.completion_tokens or 0) if usage else 0,
            "total_tokens": int(usage.total_tokens or 0) if usage else 0,
        }
        rate_limit_headers = {
            name: str(raw_response.headers[name])
            for name in (
                "x-ratelimit-limit-requests",
                "x-ratelimit-remaining-requests",
                "x-ratelimit-reset-requests",
                "x-ratelimit-limit-tokens",
                "x-ratelimit-remaining-tokens",
                "x-ratelimit-reset-tokens",
            )
            if name in raw_response.headers
        }
        if self.usage_recorder:
            self.usage_recorder(self.settings.openai_model, token_usage, rate_limit_headers)

        content = response.choices[0].message.content
        if not content:
            raise ValueError("OpenAI returned an empty research response.")
        try:
            result = json.loads(content)
        except json.JSONDecodeError as error:
            raise ValueError("OpenAI research response was not valid JSON.") from error

        signal = str(result.get("signal", "")).lower()
        if signal not in {"bullish", "bearish", "neutral"}:
            raise ValueError("OpenAI returned an unsupported signal; expected bullish, bearish, or neutral.")
        confidence = float(result.get("confidence", -1))
        if not 0 <= confidence <= 1:
            raise ValueError("OpenAI confidence must be between 0 and 1.")
        valid_grades = {"okay", "normal", "not okay", "bad", "unknown"}

        def normalize_grade(value: Any) -> str:
            grade = str(value or "unknown").strip().lower().replace("_", " ").replace("-", " ")
            return grade if grade in valid_grades else "unknown"

        grade_fields = {
            "revenue": ("total_revenue_ttm", "revenue_yoy_growth_pct"),
            "net_profit_margin": ("net_profit_margin_pct",),
            "free_cash_flow": ("free_cash_flow_ttm",),
            "debt_to_equity": ("debt_to_equity_ratio",),
        }
        supplied_fundamentals = research_input["fundamentals"]
        raw_grades = result.get("fundamental_grades", {})
        fundamental_grades = {
            name: (
                normalize_grade(raw_grades.get(name))
                if any(supplied_fundamentals.get(field) is not None for field in fields)
                else "unknown"
            )
            for name, fields in grade_fields.items()
        }
        has_fundamental_data = any(
            supplied_fundamentals.get(field) is not None
            for fields in grade_fields.values()
            for field in fields
        )
        fundamental_grade = (
            normalize_grade(result.get("fundamental_grade"))
            if has_fundamental_data else "unknown"
        )
        return {
            "signal": signal,
            "confidence": confidence,
            "catalyst": str(result.get("catalyst", "news_analysis"))[:100],
            "summary": str(result.get("summary", ""))[:1000],
            "fundamentals": research_input["fundamentals"],
            "fundamental_analysis": str(result.get("fundamental_analysis", "Not provided."))[:1000],
            "fundamental_grade": fundamental_grade,
            "fundamental_grades": fundamental_grades,
            "provider": "openai",
            "model": self.settings.openai_model,
            "usage": token_usage,
            "rate_limits": rate_limit_headers,
        }

    async def review_market_trends(
        self,
        ticker: str,
        company_name: str,
        proposed_position: str,
        fundamentals: Dict[str, Any],
        market_trends: Dict[str, Any],
    ) -> Dict[str, Any]:
        review_input = {
            "ticker": ticker,
            "company_name": company_name,
            "proposed_position": proposed_position,
            "fundamentals": fundamentals,
            "market_trends": market_trends,
        }
        async with AsyncOpenAI(
            api_key=self.settings.openai_api_key,
            base_url=self.settings.openai_base_url,
            timeout=45.0,
            max_retries=1,
        ) as client:
            raw_response = await client.chat.completions.with_raw_response.create(
                model=self.settings.openai_model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an independent risk reviewer, not the researcher. Analyze only the supplied "
                            "adjusted historical price summaries and fundamentals. Do not fetch or invent current "
                            "market data, news, causes, or forecasts. Return JSON with overall_direction "
                            "(bullish, bearish, sideways, unknown), confidence (0 to 1), rationale, and "
                            "horizon_analysis containing last_1h, last_24h, week_to_date, year_to_date, and "
                            "last_5_years. Each horizon must contain direction (bullish, bearish, sideways, "
                            "unknown), confidence (0 to 1), and rationale. Mark unavailable or insufficient "
                            "history unknown with zero confidence. Historical direction is context, not a "
                            "guarantee or standalone trade signal. Explicitly note disagreement with the "
                            "proposed position."
                        ),
                    },
                    {"role": "user", "content": json.dumps(review_input, ensure_ascii=True)},
                ],
            )
            response = raw_response.parse()

        usage = response.usage
        token_usage = {
            "prompt_tokens": int(usage.prompt_tokens or 0) if usage else 0,
            "completion_tokens": int(usage.completion_tokens or 0) if usage else 0,
            "total_tokens": int(usage.total_tokens or 0) if usage else 0,
        }
        rate_limit_headers = {
            name: str(raw_response.headers[name])
            for name in (
                "x-ratelimit-limit-requests",
                "x-ratelimit-remaining-requests",
                "x-ratelimit-reset-requests",
                "x-ratelimit-limit-tokens",
                "x-ratelimit-remaining-tokens",
                "x-ratelimit-reset-tokens",
            )
            if name in raw_response.headers
        }
        if self.usage_recorder:
            self.usage_recorder(self.settings.openai_model, token_usage, rate_limit_headers)

        content = response.choices[0].message.content
        if not content:
            raise ValueError("OpenAI returned an empty market-trend review.")
        try:
            result = json.loads(content)
        except json.JSONDecodeError as error:
            raise ValueError("OpenAI market-trend response was not valid JSON.") from error

        valid_directions = {"bullish", "bearish", "sideways", "unknown"}
        horizons = ("last_1h", "last_24h", "week_to_date", "year_to_date", "last_5_years")

        def normalize_horizon(horizon: str) -> Dict[str, Any]:
            if not market_trends.get("windows", {}).get(horizon, {}).get("available"):
                return {"direction": "unknown", "confidence": 0.0, "rationale": "Insufficient price history."}
            raw = result.get("horizon_analysis", {}).get(horizon, {})
            direction = str(raw.get("direction", "unknown")).lower()
            if direction not in valid_directions:
                direction = "unknown"
            try:
                confidence = float(raw.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            confidence = min(1.0, max(0.0, confidence))
            if direction == "unknown":
                confidence = 0.0
            return {
                "direction": direction,
                "confidence": confidence,
                "rationale": str(raw.get("rationale", ""))[:500],
            }

        horizon_analysis = {horizon: normalize_horizon(horizon) for horizon in horizons}
        overall_direction = str(result.get("overall_direction", "unknown")).lower()
        if overall_direction not in valid_directions:
            overall_direction = "unknown"
        try:
            confidence = float(result.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = min(1.0, max(0.0, confidence))
        if overall_direction == "unknown":
            confidence = 0.0
        opposes = (
            (proposed_position == "LONG" and overall_direction == "bearish")
            or (proposed_position == "SHORT" and overall_direction == "bullish")
        )
        return {
            "status": "complete",
            "ticker": ticker,
            "proposed_position": proposed_position,
            "overall_direction": overall_direction,
            "confidence": confidence,
            "opposes_proposed_position": opposes,
            "rationale": str(result.get("rationale", ""))[:1000],
            "horizon_analysis": horizon_analysis,
            "market_trends": market_trends,
            "provider": "openai",
            "model": self.settings.openai_model,
            "usage": token_usage,
            "rate_limits": rate_limit_headers,
        }


class InteractiveBrokersAdapter:
    def __init__(self, settings: IntegrationSettings) -> None:
        if not settings.ibkr_paper_trading:
            raise ValueError("Live Interactive Brokers trading is deliberately disabled in this scaffold.")
        self.settings = settings

    def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("Implement and validate the IBKR paper adapter before enabling it.")

    def get_positions(self) -> Dict[str, Any]:
        raise NotImplementedError("Implement IBKR position reconciliation before enabling it.")
