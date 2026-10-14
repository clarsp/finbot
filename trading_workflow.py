"""Versioned, side-aware news-reaction rules and transparent position sizing."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

VERSION = "news_reaction_v2"


def positive(value: Any, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be positive and finite.")
    return number


def reaction_from_bars(bars, event_at: str, now: datetime | None = None) -> dict:
    """Only completed five-minute bars; anchor immediately before the event."""
    import pandas as pd
    now = now or datetime.now(timezone.utc)
    event = pd.Timestamp(event_at)
    if event.tzinfo is None:
        event = event.tz_localize("UTC")
    frame = bars.copy()
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    frame.index = index
    frame = frame.loc[index + pd.Timedelta(minutes=5) <= pd.Timestamp(now)].sort_index()
    before = frame.loc[frame.index + pd.Timedelta(minutes=5) <= event]
    after = frame.loc[frame.index >= event]
    if before.empty or len(after) < 2:
        return {"available": False, "reason": "Need a pre-news anchor and two completed post-news bars."}
    if (pd.Timestamp(now) - event).total_seconds() < 0:
        return {"available": False, "reason": "News timestamp is in the future."}
    return {
        "available": True, "provider": "Yahoo Finance completed 5-minute OHLC",
        "event_at": event.isoformat(), "anchor": float(before.iloc[-1]["Close"]),
        "low": float(after["Low"].min()), "high": float(after["High"].max()),
        "last": float(after.iloc[-1]["Close"]), "previous": float(after.iloc[-2]["Close"]),
        "last_bar_at": (after.index[-1] + pd.Timedelta(minutes=5)).isoformat(),
        "bars": len(after),
    }


def mean_reversion_setup(candidate: dict, side: str, rules: dict) -> dict:
    reaction = candidate.get("news_reaction") or {}
    trend = candidate.get("daily_trend_context") or {}
    ai = candidate.get("ai_analysis") or {}
    result = {"approved": False, "reasons": [], "framework": "CONDITIONAL_MEAN_REVERSION"}
    reasons = result["reasons"]
    if not reaction.get("available"):
        reasons.append("Conditional mean reversion requires a measured post-news reaction and reversal confirmation.")
        return result
    if ai.get("reaction_assessment") != "temporary":
        reasons.append("Research must identify the news reaction as temporary before fading it.")
    if float(candidate.get("confidence") or 0) < float(rules.get("conditional_mean_reversion_min_confidence", 0.7)):
        reasons.append("Research confidence is below the conditional mean reversion minimum.")
    try:
        atr = positive(trend.get("atr14"), "ATR14")
        anchor = positive(reaction.get("anchor"), "Pre-news anchor")
        price = positive(candidate.get("price"), "Entry price")
        extreme = positive(reaction.get("low" if side == "LONG" else "high"), "Reaction extreme")
        sign = 1 if side == "LONG" else -1
        displacement = sign * (anchor - extreme)
        recovery = sign * (float(reaction["last"]) - extreme)
        turning = sign * (float(reaction["last"]) - float(reaction["previous"])) > 0
        required = atr * positive(rules.get("conditional_mean_reversion_min_atr_multiple", 1.25), "Reaction ATR multiple")
        cushion = atr * positive(rules.get("conditional_mean_reversion_confirmation_atr", 0.2), "Confirmation ATR")
        stop = extreme - sign * atr * float(rules.get("conditional_mean_reversion_stop_cushion_atr", 0.25))
        if displacement <= required:
            reasons.append("Asset lacks sufficient mathematical variance from the pre-news anchor to justify a countertrend entry.")
        if recovery < cushion or not turning:
            reasons.append("The completed post-news bars have not confirmed a reversal.")
        if sign * (anchor - price) <= 0 or sign * (price - stop) <= 0:
            reasons.append("The mean reversion entry no longer has a favorable target and protective stop.")
        result.update({"atr14": atr, "anchor": anchor, "extreme": extreme,
                       "distance": displacement, "required_distance": required,
                       "stop": stop, "target": anchor, "entry_zone_floor": min(extreme + sign*cushion, anchor),
                       "entry_zone_ceiling": max(extreme + sign*cushion, anchor), "reaction": reaction})
    except (TypeError, ValueError, KeyError):
        reasons.append("Conditional mean reversion price inputs are incomplete or invalid.")
    result["approved"] = not reasons
    return result


def size_order(*, side: str, entry: float, atr: float, equity: float, cash: float,
               leverage: int, positions: list[dict], risk_rules: dict, execution_rules: dict,
               sector: str | None = None, multiplier: float = 1, setup: dict | None = None) -> dict:
    """Risk sizing, then the smallest of units, margin, cash and portfolio caps."""
    entry, atr, equity = positive(entry, "Entry"), positive(atr, "ATR"), positive(equity, "Equity")
    if side not in {"LONG", "SHORT"} or leverage < 1 or int(leverage) != leverage:
        raise ValueError("Invalid trade side or leverage.")
    cash = max(0, float(cash))
    if not math.isfinite(cash):
        raise ValueError("Cash must be finite.")
    for key in ("risk_per_trade_pct", "max_total_risk_pct", "max_gross_exposure_pct"):
        if key in risk_rules:
            positive(risk_rules[key], key)
    sign = 1 if side == "LONG" else -1
    setup = setup or {}
    stop = float(setup["stop"]) if setup.get("approved") else entry - sign * atr * positive(risk_rules.get("stop_atr_multiple", 2), "Stop ATR")
    distance = sign * (entry - stop)
    positive(stop, "Stop price")
    positive(distance, "Stop distance")
    target = float(setup["target"]) if setup.get("approved") else entry + sign * distance * positive(risk_rules.get("target_reward_risk", 1.5), "Target R")
    positive(target, "Target price")
    reward = sign * (target - entry)
    gross = 0.0
    open_risk = 0.0
    unknown = []
    for p in positions:
        try:
            units = positive(abs(float(p["units"])), "Position units")
            current = positive(p.get("current_rate") or p.get("open_rate"), "Position mark")
            stop_rate = positive(p.get("stop_loss_price"), "Position stop")
            direction = p.get("side")
            if direction not in {"LONG", "SHORT"}:
                raise ValueError("Broker position direction unavailable")
            gross += units * current
            open_risk += units * max(0.0, (current-stop_rate) if direction == "LONG" else (stop_rate-current))
        except (KeyError, TypeError, ValueError):
            unknown.append(str(p.get("position_id") or "unknown"))
    risk_budget = min(equity * float(risk_rules.get("risk_per_trade_pct", .005)) * multiplier,
                      max(0, equity * float(risk_rules.get("max_total_risk_pct", .02)) - open_risk))
    capacity = max(0, equity * float(risk_rules.get("max_gross_exposure_pct", 1)) - gross)
    max_units = positive(execution_rules.get("max_order_units", 5), "Maximum units")
    max_spend = positive(execution_rules.get("max_order_spend_usd", 200), "Maximum spend")
    gap = max(0, float(risk_rules.get("stress_gap_pct", .05)))
    stress_budget = max(0, equity*positive(risk_rules.get("max_stressed_loss_pct", .10), "Stress loss limit") - open_risk - gross*gap)
    limits = {"stress_loss": stress_budget/(distance+entry*gap),
              "single_position_exposure": equity*positive(risk_rules.get("max_single_position_exposure_pct", .10), "Single position exposure")/entry,
              "risk": risk_budget/distance, "units": max_units,
              "spend": max_spend*leverage/entry, "cash": cash*leverage/entry,
              "portfolio_exposure": capacity/entry}
    units = math.floor(max(0, min(limits.values())) * 1_000_000) / 1_000_000
    spread = max(0, float(execution_rules.get("observed_spread", 0)))
    configured_bps = max(0, float(risk_rules.get("estimated_roundtrip_cost_bps", 10)))
    holding_hours = float(risk_rules.get("mean_reversion_max_hold_hours" if setup.get("approved") else "news_max_hold_hours", 24 if setup.get("approved") else 48))
    annual = max(0, float(risk_rules.get("estimated_financing_annual_pct", 8))) / 100
    cost_per_unit = spread + entry * (configured_bps/10000 + annual*holding_hours/(365*24))
    # Reserve estimated costs inside both spending and available-cash ceilings.
    limits["spend"] = max_spend / (entry/leverage + cost_per_unit)
    limits["cash"] = cash / (entry/leverage + cost_per_unit)
    units = math.floor(max(0, min(limits.values())) * 1_000_000) / 1_000_000
    errors = []
    if risk_rules.get("sector_exposure_enabled", False):
        if not sector or sector == "Unknown" or any(not p.get("sector") or p.get("sector") == "Unknown" for p in positions):
            errors.append("Sector concentration cannot be verified for the candidate and all broker holdings.")
        else:
            sector_gross = sum(abs(float(p["units"]))*float(p.get("current_rate") or p["open_rate"]) for p in positions if p.get("sector") == sector)
            limits["sector_exposure"] = max(0, equity*positive(risk_rules.get("max_sector_exposure_pct", .35), "Sector limit")-sector_gross)/entry
            units = math.floor(max(0, min(limits.values()))*1_000_000)/1_000_000
    if unknown:
        errors.append("Risk on existing broker positions cannot be verified because stop, direction, fill, or quantity is missing.")
    if not units:
        errors.append("No risk budget remains for a new position." if risk_budget <= 0 else "No cash or portfolio exposure capacity remains for a new position.")
    if reward <= cost_per_unit or reward/distance < float(risk_rules.get("minimum_reward_risk", 1)):
        errors.append("The proposed target does not cover estimated costs and the minimum reward/risk ratio.")
    protection = {
        "framework": "CONDITIONAL_MEAN_REVERSION" if setup.get("approved") else "DIRECTIONAL_NEWS",
        "atr14": atr,
        "entry_price": entry,
        "stop_loss_price": stop,
        "take_profit_price": target,
        "stop_distance": distance,
        "target_distance": reward,
        "stop_atr_multiple": distance / atr,
        "target_atr_multiple": reward / atr,
        "reward_risk_ratio": reward / distance,
        "formula": (
            "Mean-reversion stop and anchor target derived from the measured news reaction."
            if setup.get("approved") else
            "Stop = entry ± configured ATR multiple; take profit = entry ∓/± stop distance × target reward/risk."
        ),
    }
    return {"proposed_units": units, "proposed_entry": entry, "proposed_stop": stop,
            "proposed_take_profit": target, "risk_budget": risk_budget, "open_stop_risk": open_risk,
            "stress_gap_pct": gap, "stressed_loss": open_risk+gross*gap+units*(distance+entry*gap),
            "gross_exposure": gross, "projected_gross_exposure": gross + units*entry,
            "required_margin": units*entry/leverage, "notional": units*entry,
            "estimated_total_spend": units*(entry/leverage+cost_per_unit),
            "planned_stop_risk": units*distance, "estimated_costs": units*cost_per_unit,
            "estimated_net_target_profit": units*(reward-cost_per_unit),
            "unverifiable_positions": unknown, "execution_reasons": errors, "holding_hours": holding_hours,
            "protection": protection,
            "sizing": {"formula": "min(risk budget / stop distance, unit cap, spend × leverage / price, cash × leverage / price, remaining gross exposure / price, single-position exposure, stress-loss capacity, sector capacity when enabled)",
                       "limits_units": limits, "binding_limit": min(limits, key=limits.get),
                       "max_units": max_units, "max_spend_usd": max_spend,
                       "spend_basis": "account capital / margin, before final broker fees", "units": units}}


def exit_reason(trade: dict, mark: float, now: datetime | None = None) -> str | None:
    now = now or datetime.now(timezone.utc)
    sign = 1 if trade["side"] == "LONG" else -1
    if trade.get("stop_loss_price") and sign*(mark-float(trade["stop_loss_price"])) <= 0:
        return "stop_level_observed"
    if trade.get("take_profit_price") and sign*(mark-float(trade["take_profit_price"])) >= 0:
        return "take_profit_level_observed"
    policy = trade.get("policy") or {}
    opened = datetime.fromisoformat(str(trade.get("opened_at") or trade["created_at"]).replace("Z", "+00:00"))
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    if now >= opened + timedelta(hours=float(policy.get("holding_hours", 48))):
        return "thesis_time_expired"
    return None
