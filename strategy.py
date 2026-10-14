"""Deterministic, data-backed trend strategy and portfolio evaluation helpers."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import pandas as pd
import yfinance as yf


STRATEGY_VERSION = "news_reaction_v2"
DAILY_TREND_VERSION = "daily_trend_v1"


def configured_symbols() -> list[str]:
    raw = os.getenv("FINBOT_SYMBOLS", "AAPL,AMD,MSFT,NVDA,META,XOM,XLE")
    symbols = list(dict.fromkeys(part.strip().upper() for part in raw.split(",") if part.strip()))
    return [symbol for symbol in symbols if symbol.replace(".", "").isalnum()]


def load_daily_bars(symbol: str, period: str = "5y", now=None) -> pd.DataFrame:
    """Load adjusted daily bars; remove today's possibly incomplete candle."""
    ticker = yf.Ticker(symbol)
    history = ticker.history(period=period, interval="1d", auto_adjust=True, actions=False)
    if history is None or history.empty:
        return pd.DataFrame()
    frame = history.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [column[0] for column in frame.columns]
    required = {"Open", "High", "Low", "Close"}
    if not required.issubset(frame.columns):
        return pd.DataFrame()
    index = pd.DatetimeIndex(frame.index)
    checked_at = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    checked_at = checked_at.tz_localize("UTC") if checked_at.tzinfo is None else checked_at
    today = checked_at.tz_convert(index.tz or "UTC").date()
    current_session_complete = False
    try:
        regular = ticker.get_history_metadata()["currentTradingPeriod"]["regular"]
        session_end = pd.Timestamp(regular["end"], unit="s", tz="UTC")
        session_date = session_end.tz_convert(index.tz or "UTC").date()
        current_session_complete = session_date == today and checked_at >= session_end
    except (KeyError, TypeError, AttributeError, ValueError):
        pass
    frame = frame.loc[[stamp.date() < today or stamp.date() == today and current_session_complete for stamp in index]].copy()
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"]).sort_index()
    if frame.empty:
        return frame
    previous_close = frame["Close"].shift(1)
    true_range = pd.concat(
        [
            frame["High"] - frame["Low"],
            (frame["High"] - previous_close).abs(),
            (frame["Low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["sma50"] = frame["Close"].rolling(50, min_periods=50).mean()
    frame["sma200"] = frame["Close"].rolling(200, min_periods=200).mean()
    frame["atr14"] = true_range.rolling(14, min_periods=14).mean()
    return frame.dropna(subset=["sma50", "sma200", "atr14"])


def evaluate_latest_signal(symbol: str, bars: pd.DataFrame) -> Dict[str, Any]:
    if bars is None or bars.empty:
        return {"available": False, "symbol": symbol, "reason": "No complete daily bars."}
    row = bars.iloc[-1]
    close, sma50, sma200, atr14 = (float(row[key]) for key in ("Close", "sma50", "sma200", "atr14"))
    if not all(math.isfinite(value) and value > 0 for value in (close, sma50, sma200, atr14)):
        return {"available": False, "symbol": symbol, "reason": "Daily indicator data is invalid."}
    return {
        "available": True,
        "symbol": symbol,
        "strategy": DAILY_TREND_VERSION,
        "signal_timestamp": pd.Timestamp(bars.index[-1]).isoformat(),
        "signal_provider": "Yahoo Finance adjusted daily OHLC",
        "close": close,
        "sma50": sma50,
        "sma200": sma200,
        "atr14": atr14,
        "enter_long": close > sma50 > sma200,
        "exit_long": close < sma50,
        "trend_state": (
            "bullish" if close > sma50 > sma200
            else "bearish" if close < sma50 < sma200
            else "mixed"
        ),
        "side": "LONG" if close > sma50 > sma200 else "FLAT",
    }


@dataclass(frozen=True)
class BacktestCosts:
    spread_bps_per_side: float
    commission_bps_per_side: float
    financing_annual_pct: float
    fx_bps_per_side: float

    @classmethod
    def from_environment(cls) -> Optional["BacktestCosts"]:
        names = (
            "FINBOT_BACKTEST_SPREAD_BPS_PER_SIDE",
            "FINBOT_BACKTEST_COMMISSION_BPS_PER_SIDE",
            "FINBOT_BACKTEST_FINANCING_ANNUAL_PCT",
            "FINBOT_BACKTEST_FX_BPS_PER_SIDE",
        )
        raw = [os.getenv(name, "").strip() for name in names]
        if any(value == "" for value in raw):
            return None
        try:
            values = [float(value) for value in raw]
        except ValueError:
            return None
        if not all(math.isfinite(value) and value >= 0 for value in values):
            return None
        return cls(*values)


def run_portfolio_backtest(
    symbol_bars: Dict[str, pd.DataFrame],
    costs: Optional[BacktestCosts],
    *,
    leverage: int = 2,
    risk_per_trade_pct: float = 0.005,
    max_total_risk_pct: float = 0.02,
    starting_capital: float = 10000.0,
    max_positions: int = 5,
    max_gross_exposure_pct: float = 1.0,
) -> Dict[str, Any]:
    """Chronological equal-universe portfolio test with a fixed final 20% holdout."""
    if costs is None:
        return {"status": "inconclusive", "reason": "Backtest cost inputs are incomplete."}
    usable = {symbol: bars for symbol, bars in symbol_bars.items() if bars is not None and len(bars) >= 220}
    if not usable:
        return {"status": "inconclusive", "reason": "No symbol has at least 220 usable completed daily bars."}
    dates = sorted(set().union(*(set(bars.index) for bars in usable.values())))
    if len(dates) < 500:
        return {"status": "inconclusive", "reason": "At least 500 shared-calendar daily observations are required."}
    holdout_start = dates[int(len(dates) * 0.8)]
    holdout_dates = [date for date in dates if date >= holdout_start]
    if len(holdout_dates) < 100:
        return {"status": "inconclusive", "reason": "Final untouched evaluation window has fewer than 100 sessions."}

    capital = float(starting_capital)
    peak = capital
    max_drawdown = 0.0
    positions: Dict[str, Dict[str, float]] = {}
    trades = 0
    turnover = 0.0
    total_costs = 0.0
    daily_equity: list[float] = []
    previous_rows: Dict[str, pd.Series] = {}

    def marked_close(symbol: str, rows: Dict[str, pd.Series]) -> float:
        row = rows.get(symbol)
        if row is None:
            row = previous_rows[symbol]
        return float(row["Close"])

    for date in dates:
        rows = {symbol: bars.loc[date] for symbol, bars in usable.items() if date in bars.index}
        if date < holdout_start:
            previous_rows.update(rows)
            continue
        # Exits are processed before entries. A close below SMA50 is known only
        # after that session, so the executable model exits at the next open.
        # Stop gaps fill at the open; otherwise at the stop.
        for symbol, position in list(positions.items()):
            row = rows.get(symbol)
            if row is None:
                continue
            open_price, low, close = float(row["Open"]), float(row["Low"]), float(row["Close"])
            stop = position["stop"]
            exit_price = open_price if open_price <= stop else stop if low <= stop else None
            previous = previous_rows.get(symbol)
            if exit_price is None and previous is not None and float(previous["Close"]) < float(previous["sma50"]):
                exit_price = open_price
            if exit_price is not None:
                notional = position["units"] * exit_price
                side_cost_rate = (costs.spread_bps_per_side + costs.commission_bps_per_side + costs.fx_bps_per_side) / 10000
                fee = notional * side_cost_rate
                pnl = position["units"] * (exit_price - position["entry"])
                financing = position["notional"] * max(0, leverage - 1) * costs.financing_annual_pct / 100 / 252 * position["days"]
                capital += pnl - fee - financing
                total_costs += fee + financing
                turnover += notional
                trades += 1
                del positions[symbol]
            else:
                position["days"] += 1

        equity_mark = capital + sum(
            position["units"] * (marked_close(symbol, rows) - position["entry"])
            for symbol, position in positions.items()
        )
        open_risk = sum(position["risk_amount"] for position in positions.values())
        new_positions: list[tuple[str, Dict[str, float]]] = []
        for symbol, row in rows.items():
            if symbol in positions or len(positions) >= max_positions:
                continue
            previous = previous_rows.get(symbol)
            if previous is None or not (float(previous["Close"]) > float(previous["sma50"]) > float(previous["sma200"])):
                continue
            entry = float(row["Open"])
            stop = entry - 2 * float(previous["atr14"])
            if not (entry > stop > 0):
                continue
            risk_amount = equity_mark * risk_per_trade_pct
            if open_risk + risk_amount > equity_mark * max_total_risk_pct:
                continue
            units = risk_amount / (entry - stop)
            notional = units * entry
            if notional > equity_mark * max_gross_exposure_pct:
                units = equity_mark * max_gross_exposure_pct / entry
                risk_amount = units * (entry - stop)
            side_cost_rate = (costs.spread_bps_per_side + costs.commission_bps_per_side + costs.fx_bps_per_side) / 10000
            fee = units * entry * side_cost_rate
            capital -= fee
            total_costs += fee
            turnover += units * entry
            position = {"entry": entry, "stop": stop, "units": units, "notional": units * entry, "risk_amount": risk_amount, "days": 0}
            positions[symbol] = position
            new_positions.append((symbol, position))
            open_risk += risk_amount

        # A stop can be hit on the entry session. If the open gaps below it,
        # model the fill at the open; otherwise fill at the stop price.
        for symbol, position in new_positions:
            row = rows[symbol]
            low, open_price = float(row["Low"]), float(row["Open"])
            if low <= position["stop"]:
                exit_price = open_price if open_price <= position["stop"] else position["stop"]
                notional = position["units"] * exit_price
                side_cost_rate = (costs.spread_bps_per_side + costs.commission_bps_per_side + costs.fx_bps_per_side) / 10000
                fee = notional * side_cost_rate
                capital += position["units"] * (exit_price - position["entry"]) - fee
                total_costs += fee
                turnover += notional
                trades += 1
                del positions[symbol]

        equity = capital + sum(
            position["units"] * (marked_close(symbol, rows) - position["entry"])
            for symbol, position in positions.items()
        )
        daily_equity.append(equity)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak if peak else 0)
        previous_rows.update(rows)

    for symbol, position in list(positions.items()):
        available_rows = usable[symbol].loc[usable[symbol].index <= holdout_dates[-1]]
        if not available_rows.empty:
            exit_price = float(available_rows.iloc[-1]["Close"])
            fee = position["units"] * exit_price * (costs.spread_bps_per_side + costs.commission_bps_per_side + costs.fx_bps_per_side) / 10000
            financing = position["notional"] * max(0, leverage - 1) * costs.financing_annual_pct / 100 / 252 * position["days"]
            capital += position["units"] * (exit_price - position["entry"]) - fee - financing
            total_costs += fee + financing
            turnover += position["units"] * exit_price
            trades += 1
    peak = max(peak, capital)
    max_drawdown = max(max_drawdown, (peak - capital) / peak if peak else 0)

    benchmark_returns = []
    benchmark_cost_rate = (costs.spread_bps_per_side + costs.commission_bps_per_side + costs.fx_bps_per_side) / 10000
    for symbol, bars in usable.items():
        section = bars.loc[bars.index.isin(holdout_dates)]
        if len(section) >= 2:
            gross_return = float(section["Close"].iloc[-1] / section["Open"].iloc[0])
            benchmark_returns.append(gross_return * (1 - benchmark_cost_rate) ** 2 - 1)
    benchmark_return = float(sum(benchmark_returns) / len(benchmark_returns)) if benchmark_returns else None
    net_return = capital / starting_capital - 1
    try:
        max_allowed_drawdown_pct = float(os.getenv("FINBOT_BACKTEST_MAX_DRAWDOWN_PCT", "20"))
    except ValueError:
        return {"status": "inconclusive", "reason": "Maximum backtest drawdown limit must be numeric."}
    if not math.isfinite(max_allowed_drawdown_pct) or max_allowed_drawdown_pct <= 0:
        return {"status": "inconclusive", "reason": "Maximum backtest drawdown limit must be positive and finite."}
    passed = (
        net_return > 0 and benchmark_return is not None and net_return > benchmark_return
        and trades >= 10 and max_drawdown * 100 <= max_allowed_drawdown_pct
    )
    return {
        "status": "eligible_for_demo_review" if passed else "inconclusive",
        "reason": None if passed else "The fixed holdout did not clear the configured minimum evidence gate; automated orders remain disabled.",
        "strategy": STRATEGY_VERSION,
        "holdout_start": pd.Timestamp(holdout_start).isoformat(),
        "holdout_end": pd.Timestamp(holdout_dates[-1]).isoformat(),
        "net_return_pct": net_return * 100,
        "benchmark_return_pct": benchmark_return * 100 if benchmark_return is not None else None,
        "max_drawdown_pct": max_drawdown * 100,
        "trade_count": trades,
        "turnover": turnover,
        "costs": total_costs,
        "starting_capital": starting_capital,
        "ending_capital": capital,
        "cost_assumptions": costs.__dict__,
        "symbols_used": sorted(usable),
        "leverage": leverage,
        "risk_per_trade_pct": risk_per_trade_pct,
        "max_total_risk_pct": max_total_risk_pct,
        "max_positions": max_positions,
        "max_gross_exposure_pct": max_gross_exposure_pct,
        "max_allowed_drawdown_pct": max_allowed_drawdown_pct,
        "evidence_gate": "Positive net return, above equal-weight buy-and-hold, at least 10 closed trades, and max drawdown within configured limit in the untouched final 20% holdout. This is not proof of future profitability.",
    }
