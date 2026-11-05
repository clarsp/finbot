"""Versioned, side-aware news-reaction rules and transparent position sizing."""
from __future__ import annotations

import math
from recent_trend import active_recent_trend
from price_structure import structure_target
from datetime import datetime, timedelta, timezone
from typing import Any

VERSION = "news_reaction_v5"
MANAGED_VERSIONS = frozenset({'news_reaction_v2', 'news_reaction_v3', 'news_reaction_v4', VERSION})


def continuation_setup(candidate, side, rules, risk_rules=None):
    """Two completed closes must hold beyond an observed range boundary."""
    risk_rules = risk_rules or {}
    trend = candidate.get('daily_trend_context') or {}
    reaction = candidate.get('news_reaction') or {}
    result = {'approved': False, 'framework': 'NEWS_BREAKOUT_CONTINUATION', 'reasons': []}
    if side not in {'LONG', 'SHORT'} or not rules.get('fresh_boundary_entries_enabled', True):
        result['reasons'].append('Fresh boundary continuation is disabled or direction is invalid.')
        return result
    try:
        entry = positive(candidate.get('price'), 'Entry')
        atr = positive(trend.get('atr14'), 'ATR')
        sign = 1 if side == 'LONG' else -1
        _, evidence = structure_target(side, entry, entry+sign*3*atr, atr,
            trend.get('price_structure'), mode='range_boundary',
            range_months=risk_rules.get('structure_range_months', 1),
            range_weeks=risk_rules.get('structure_range_weeks', 4))
        result['boundary_evidence'] = {k:v for k,v in evidence.items() if k not in {'structure','crossed_obstacles'}}
        if not evidence.get('entry_blocked'):
            result['reasons'].append('No crossed observed monthly/weekly boundary.')
            return result
        zone = evidence['zone']
        boundary = float(zone['low'] if side == 'LONG' else zone['high'])
        buffer = .1*atr
        if not reaction.get('available') or int(reaction.get('bars', 0)) < 2:
            result['reasons'].append('Breakout requires two completed post-event bars.')
        else:
            last, previous = positive(reaction['last'], 'Last close'), positive(reaction['previous'], 'Previous close')
            if min(sign*(last-boundary), sign*(previous-boundary), sign*(entry-boundary)) <= buffer or sign*(last-previous) <= 0:
                result['reasons'].append('Breakout has not held beyond the boundary with directional follow-through.')
        if float(candidate.get('confidence') or 0) < float(rules.get('conditional_mean_reversion_min_confidence', .7)):
            result['reasons'].append('Breakout confidence is below the setup minimum.')
        result.update(boundary=boundary, buffer=buffer, side=side)
        result['approved'] = not result['reasons']
    except (KeyError, ValueError, TypeError, OverflowError):
        result['reasons'].append('Breakout inputs are missing or invalid.')
    return result


def select_entry_side(candidate, rules, risk_rules=None):
    """One direction selector for research, strategy and submission rechecks."""
    signal = candidate.get('signal')
    side = 'LONG' if signal == 'bullish' else 'SHORT' if signal == 'bearish' else 'HOLD'
    if side == 'HOLD':
        return side
    recent = active_recent_trend(candidate, rules)
    if recent:
        bias = 'LONG' if recent['state'] == 'bullish' else 'SHORT'
        reversal_side = 'SHORT' if bias == 'LONG' else 'LONG'
        quote = candidate.get('market_quote') or {}
        reversal = {**candidate, 'price': quote.get('ask' if reversal_side == 'LONG' else 'bid') or candidate.get('price')}
        minimum = float(rules.get('countertrend_long_min_confidence' if reversal_side == 'LONG' else 'countertrend_short_min_confidence', .85))
        if (float(candidate.get('confidence') or 0) >= minimum
                and mean_reversion_setup(reversal, reversal_side, rules).get('approved')):
            return reversal_side
        return bias
    trend = str((candidate.get('daily_trend_context') or {}).get('trend_state') or 'unknown').lower()
    quote = candidate.get('market_quote') or {}
    directional = {**candidate, 'price': quote.get('ask' if side == 'LONG' else 'bid') or candidate.get('price')}
    if side != 'HOLD' and continuation_setup(directional, side, rules, risk_rules).get('approved'):
        return side
    if (candidate.get('ai_analysis') or {}).get('reaction_assessment') == 'temporary':
        mixed_allowed = trend == 'mixed' and rules.get('allow_mixed_trend_reversals', True)
        if signal == 'bearish' and (trend == 'bullish' or mixed_allowed):
            return 'LONG'
        if signal == 'bullish' and (trend == 'bearish' or mixed_allowed):
            return 'SHORT'
        opposite = 'LONG' if signal == 'bearish' else 'SHORT'
        opposite_candidate = {**candidate, 'price': quote.get('ask' if opposite == 'LONG' else 'bid') or candidate.get('price')}
        if (trend in {'bullish', 'bearish'} and float(candidate.get('confidence') or 0) >= float(rules.get('countertrend_long_min_confidence' if opposite == 'LONG' else 'countertrend_short_min_confidence', .85))
                and mean_reversion_setup(opposite_candidate, opposite, rules).get('approved')):
            return opposite
    return side


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
    if frame.empty or (pd.Timestamp(now)-frame.index[-1]-pd.Timedelta(minutes=5)).total_seconds() > 900:
        return {'available': False, 'reason': 'Completed post-news bars are unavailable or stale (over 15 minutes).'}
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
               sector: str | None = None, multiplier: float = 1, setup: dict | None = None, structure: dict | None = None) -> dict:
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
    reversion = bool(setup.get('approved') and setup.get('framework', 'CONDITIONAL_MEAN_REVERSION') == 'CONDITIONAL_MEAN_REVERSION')
    breakout = bool(setup.get('approved') and setup.get('framework') == 'NEWS_BREAKOUT_CONTINUATION')
    stop = float(setup["stop"]) if reversion else entry - sign * atr * positive(risk_rules.get("stop_atr_multiple", 2), "Stop ATR")
    distance = sign * (entry - stop)
    positive(stop, "Stop price")
    positive(distance, "Stop distance")
    target = float(setup["target"]) if reversion else entry + sign * distance * positive(risk_rules.get("target_reward_risk", 1.5), "Target R")
    if not reversion:
        # Independent ceiling, preserving the existing 2 ATR stop / 1.5R default.
        target = entry + sign*min(abs(target-entry), atr*positive(risk_rules.get('max_target_atr_multiple', 3.0), 'Max target ATR'))
    baseline = target
    target, target_evidence = structure_target(side, entry, target, atr, structure,
        mode='nearest' if reversion else risk_rules.get('structure_target_mode','range_boundary'),
        range_months=risk_rules.get('structure_range_months',1), range_weeks=risk_rules.get('structure_range_weeks',4))
    target_evidence['structure_state'] = ('unavailable' if not structure or not structure.get('available')
        else 'boundary_crossed' if target_evidence.get('entry_blocked') else
        'opposing_boundary' if target_evidence.get('zone') else 'no_opposing_boundary')
    if target_evidence.get('entry_blocked') and breakout:
        boundary = float((target_evidence.get('zone') or {}).get('low' if side == 'LONG' else 'high', float('nan')))
        if setup.get('side') == side and math.isfinite(boundary) and abs(boundary-float(setup['boundary'])) < .000001 and sign*(entry-boundary) > .1*atr:
            target, ahead = structure_target(side, entry, baseline, atr, structure, mode='nearest')
            target_evidence.update(entry_blocked=False, breakout_confirmed=True, selected_target=target,
                adjusted=target!=baseline, breakout_setup=setup, opposing_boundary=ahead.get('zone'),
                breakout_assumption='Two completed closes held beyond the observed boundary with directional follow-through.')
            target_evidence.pop('entry_block_reason', None)
    if sign*(target-entry) <= 0:
        target_evidence.update(entry_blocked=True, entry_block_reason=target_evidence.get('entry_block_reason') or
            'Opposing structure leaves no valid target beyond entry.', rejected_target=target)
        target = baseline
        target_evidence['selected_target'] = target
    if setup and not setup.get('approved') and setup.get('framework') == 'CONDITIONAL_MEAN_REVERSION':
        target_evidence.update(entry_blocked=True, entry_block_reason='Measured temporary-reaction reversal is not confirmed.')
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
    holding_hours = float(risk_rules.get("mean_reversion_max_hold_hours" if reversion else "news_max_hold_hours", 24 if reversion else 48))
    annual = max(0, float(risk_rules.get("estimated_financing_annual_pct", 8))) / 100
    cost_per_unit = spread + entry * (configured_bps/10000 + annual*holding_hours/(365*24))
    # Reserve estimated costs inside both spending and available-cash ceilings.
    limits["spend"] = max_spend / (entry/leverage + cost_per_unit)
    limits["cash"] = cash / (entry/leverage + cost_per_unit)
    units = math.floor(max(0, min(limits.values())) * 1_000_000) / 1_000_000
    errors = []
    terminal_reasons=[]
    if target_evidence.get("entry_blocked"):
        terminal_reasons.append(target_evidence["entry_block_reason"])
        errors.extend(terminal_reasons)
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
    net_reward = reward - cost_per_unit
    minimum_rr = float(risk_rules.get("minimum_reward_risk", 0))
    economic_failures = []
    if target_evidence.get('entry_blocked'):
        economic_failures.append('no_valid_structure_target')
    if net_reward <= 0:
        economic_failures.append("non_positive_net_reward")
    if net_reward / distance < minimum_rr:
        economic_failures.append("below_minimum_reward_risk")
    if economic_failures:
        reason = (
            target_evidence['entry_block_reason'] if target_evidence.get('entry_blocked') else
            f"Target does not cover estimated costs: gross reward ${reward:.4f}/unit, costs ${cost_per_unit:.4f}/unit."
            if net_reward <= 0 else
            f"Target is profitable (${units*net_reward:.2f} estimated net), but net reward/risk {net_reward/distance:.3f} is below the {minimum_rr:.3f} minimum."
        )
        if not terminal_reasons:terminal_reasons.append(reason)
        errors.append(reason)
    economics = {
        "passed": not economic_failures, "failure_codes": economic_failures,
        "gross_reward_per_unit": reward, "estimated_cost_per_unit": cost_per_unit,
        "net_reward_per_unit": net_reward, "stop_risk_per_unit": distance,
        "gross_reward_risk": reward/distance, "net_reward_risk": net_reward/distance,
        "minimum_reward_risk": minimum_rr, "actual_units": units,
        "estimated_gross_target_profit": units*reward, "estimated_net_target_profit": units*net_reward,
        "planned_stop_loss_before_costs": units*distance,
        "estimated_stop_loss_with_costs": units*(distance+cost_per_unit),
        "required_target_distance": cost_per_unit + minimum_rr*distance,
        "target_constrained_by_structure": bool(target_evidence.get("adjusted")),
        "formula": "net reward/risk = (directional target distance - estimated cost per unit) / stop distance; leverage is already included in actual units",
    }
    protection = {
        "target_selection": target_evidence,
        "framework": "CONDITIONAL_MEAN_REVERSION" if reversion else "NEWS_BREAKOUT_CONTINUATION" if breakout else "RECENT_TREND_CONTINUATION" if setup.get("framework") == "RECENT_TREND_CONTINUATION" else "DIRECTIONAL_NEWS",
        "atr14": atr,
        "entry_price": entry,
        "stop_loss_price": stop,
        "take_profit_price": target,
        "stop_distance": distance,
        "target_distance": reward,
        "stop_atr_multiple": distance / atr,
        "target_atr_multiple": reward / atr,
        "reward_risk_ratio": reward / distance,
        "net_reward_risk_ratio": net_reward / distance,
        "formula": (
            target_evidence.get("formula", "Baseline target capped before daily/weekly/monthly support/resistance; stop preserved.")
            if target_evidence["adjusted"] else
            "Mean-reversion stop and anchor target derived from the measured news reaction."
            if reversion else
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
            "cost_assumptions": {"units": units, "spread_per_unit": spread,
                                 "roundtrip_bps": configured_bps, "financing_annual_pct": annual*100,
                                 "holding_hours": holding_hours, "currency": "USD",
                                 "source": "entry-time configured forecast; broker opening fees reserved separately"},
            "estimated_net_target_profit": units*(reward-cost_per_unit),
            "economic_gate": economics,
            "unverifiable_positions": unknown, "rejection_reasons": terminal_reasons, "execution_reasons": errors, "holding_hours": holding_hours,
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
    # Holding age is a reassessment trigger, never an exit condition.
    return None


def recent_entry_requirements(candidate, side, rules):
    """Shared Strategy/Risk guard; a news score never becomes a short probability."""
    from volume_context import confirmation, adjusted_rules
    recent = active_recent_trend(candidate, rules)
    result = {'active': bool(recent), 'reasons': [], 'recent_trend': recent,
              'auto_confirm_enabled': rules.get('trend_auto_confirm_enabled', True),
              'confirmation_status': 'not_required'}
    if not recent:
        return result
    bias = 'LONG' if recent['state'] == 'bullish' else 'SHORT'
    countertrend = side != bias
    result.update(bias=bias, countertrend=countertrend,
                  confidence_role='research_quality_threshold; not a probability for the selected trade direction')
    if countertrend:
        setup = mean_reversion_setup(candidate, side, rules)
        required = float(rules.get('countertrend_long_min_confidence' if side == 'LONG' else 'countertrend_short_min_confidence', .85))
        result['setup'] = setup
        measured = bool(setup.get('approved'))
        result['confirmation_status'] = 'confirmed' if measured else 'awaiting_measurement'
        if not setup.get('approved'):
            result['reasons'].append('Entry opposes the recent price trend without a confirmed measured reversal.')
            result['reasons'].extend(setup['reasons'])
    else:
        effective, _ = adjusted_rules(candidate, side, rules)
        required = float(effective.get('min_confidence', .7))
        signal = candidate.get('signal')
        news_side = 'LONG' if signal == 'bullish' else 'SHORT' if signal == 'bearish' else 'HOLD'
        if news_side != side:
            result['entry_confirmation'] = confirmation(candidate, side, {**rules, 'volume_confirmation_enabled': True})
            result['confirmation_status'] = 'confirmed' if result['entry_confirmation']['qualified'] else 'awaiting_measurement'
            if not result['entry_confirmation']['qualified']:
                result['reasons'].append('Recent price trend opposes the research direction; fresh direction-aligned RVOL/VWAP and completed-bar follow-through are required before entry.')
    if result['confirmation_status'] != 'not_required':
        if not result['auto_confirm_enabled']:
            result['confirmation_status'] = 'automatic_confirmation_disabled'
            result['reasons'].append('Strategist auto-confirm is OFF; this entry needs confirmation and is held for review. Enable auto-confirm to allow a fresh measured reassessment.')
            result['confirmation_message'] = ' Strategist auto-confirm OFF: entry held for review; confirmation is not bypassed.'
        elif result['confirmation_status'] == 'confirmed':
            result['confirmation_message'] = ' Strategist auto-confirm ON: measured entry confirmation passed automatically; confidence, risk and execution gates still apply.'
        else:
            result['confirmation_message'] = ' Strategist auto-confirm ON: awaiting measured entry confirmation; no entry authorized by this confirmation check.'
    result['required_confidence'] = required
    if float(candidate.get('confidence') or 0) < required:
        result['reasons'].append(f'Research quality confidence is below the {required:.1%} threshold; it is not treated as a probability for the selected direction.')
    return result
