"""Daily swing zones, weekly/monthly boundaries, and target selection."""
import math
import pandas as pd


def daily_structure(bars):
    """Use at most 120 completed bars; pivots need two bars on either side.

    A zone requires two independent pivots, separated by at least five bars.
    Yahoo adjusted prices match the existing ATR price basis.
    """
    if bars is None or len(bars) < 20:
        return {"available": False, "reason": "Fewer than 20 completed daily bars."}
    frame = bars.tail(120)
    atr = float(frame.iloc[-1]["atr14"])
    if not math.isfinite(atr) or atr <= 0:
        return {"available": False, "reason": "Invalid daily ATR."}
    zones = {}
    for kind, column in (("resistance", "High"), ("support", "Low")):
        values = [float(v) for v in frame[column]]
        clusters = []
        for i in range(2, len(values)-2):
            window = values[i-2:i+3]
            value = values[i]
            if not math.isfinite(value) or value <= 0:
                continue
            if max(window) == min(window) or value != (max(window) if kind == "resistance" else min(window)):
                continue
            cluster = next((c for c in clusters if abs(value-c["center"]) <= .25*atr), None)
            if cluster is None:
                clusters.append({"center": value, "values": [value], "indices": [i]})
            elif i-cluster["indices"][-1] >= 5:
                cluster["values"].append(value)
                cluster["indices"].append(i)
                cluster["center"] = sum(cluster["values"])/len(cluster["values"])
        zones[kind] = [{"low": min(c["values"]), "high": max(c["values"]),
                        "touches": len(c["values"]),
                        "last_confirmed_at": frame.index[c["indices"][-1]+2].isoformat()}
                       for c in clusters if len(c["values"]) >= 2]
    return {"available": True, "method": "daily_swings_v1", "lookback_bars": len(frame),
            "pivot_confirmation_bars": 2, "cluster_atr": .25,
            "as_of": frame.index[-1].isoformat(), **zones}


def structure_target(side, entry, baseline, atr, structure, *, mode="nearest", range_months=1, range_weeks=4):
    """Cap at the near edge of the nearest opposing zone, buffered by .1 ATR.

    A zone containing entry blocks a new entry rather than being skipped.
    Reward/risk and cost validation belong to the caller.
    """
    if mode not in {'nearest', 'range_boundary'}:
        raise ValueError('Unknown structure target mode.')
    if mode == 'range_boundary':
        return range_boundary_target(side, entry, baseline, atr, structure, range_months, range_weeks)
    sign = 1 if side == "LONG" else -1
    evidence = {"baseline_target": baseline, "selected_target": baseline,
                "adjusted": False, "mode": "nearest", "structure": structure or {"available": False}}
    if not structure or not structure.get("available"):
        return baseline, evidence
    zones = structure.get("resistance" if side == "LONG" else "support", [])
    candidates = [z for z in zones if (z["high"] >= entry if sign == 1 else z["low"] <= entry)]
    if not candidates:
        return baseline, evidence
    zone = min(candidates, key=lambda z: sign*((z["low"] if sign == 1 else z["high"])-entry))
    capped = (zone["low"] if sign == 1 else zone["high"]) - sign*.1*atr
    if sign*(capped-baseline) < 0:
        evidence.update(selected_target=capped, adjusted=True, zone=zone, buffer_atr=.1)
        return capped, evidence
    return baseline, evidence


def range_boundary_target(side, entry, baseline, atr, structure, months=1, weeks=4):
    """Use the outer recent monthly boundary; nearer levels remain obstacles.

    Current month + N preceding calendar months, using completed bars only.
    Weekly fallback spans W calendar weeks through as_of. Old multi-year highs
    never become the target merely because they appear in the input history.
    The ATR baseline is never extended. Missing range metadata falls back to the
    original nearest-level rule, with an explicit reason.
    """
    sign = 1 if side == 'LONG' else -1
    key = 'resistance' if side == 'LONG' else 'support'
    nearest, evidence = structure_target(side, entry, baseline, atr, structure)
    evidence.update(mode='range_boundary', requested_mode='range_boundary',
        range_months=months, range_weeks=weeks, nearest_rule_target=nearest)
    if not structure or not structure.get('available'):
        evidence['fallback_reason']='Structure unavailable; ATR baseline retained.'
        return baseline, evidence
    try:
        as_of = pd.Timestamp(structure['as_of'])
        if as_of.tzinfo:
            as_of = as_of.tz_localize(None)
        monthly_cutoff = (as_of.to_period('M')-int(months)).start_time
        weekly_cutoff = as_of-pd.Timedelta(weeks=int(weeks))
    except (KeyError, ValueError, TypeError):
        evidence.update(mode='nearest', fallback_reason='Range dates unavailable; nearest-level fallback.')
        return nearest, evidence
    zones = structure.get(key, [])
    def recent(timeframe, cutoff):
        selected=[]
        for zone in zones:
            if zone.get('timeframe')!=timeframe or not zone.get('source_through'):
                continue
            observed=pd.Timestamp(zone['source_through'])
            if observed.tzinfo:
                observed=observed.tz_localize(None)
            if cutoff <= observed <= as_of:
                selected.append(zone)
        return selected
    monthly = recent('monthly', monthly_cutoff)
    weekly = recent('weekly', weekly_cutoff)
    candidates = monthly or weekly
    if not candidates:
        evidence.update(mode='nearest', fallback_reason='No recent monthly/weekly range; nearest-level fallback.')
        return nearest, evidence
    edge = lambda z: float(z['low'] if sign==1 else z['high'])
    boundary = max(candidates, key=lambda z:sign*edge(z))
    capped = edge(boundary)-sign*.1*atr
    target = baseline if sign*(baseline-capped)<=0 else capped
    obstacles=[z for z in zones if sign*(edge(z)-entry)>=0 and sign*(target-edge(z))>0]
    # A boundary behind entry intentionally fails normal directional/net gates;
    # the policy does not silently authorize a new monthly breakout.
    evidence.update(selected_target=target, adjusted=target!=baseline, zone=boundary,
        boundary_timeframe='monthly' if monthly else 'weekly', buffer_atr=.1,
        crossed_obstacles=obstacles, requires_nearby_breakout=bool(obstacles),
        breakout_assumption='Existing directional strategy approval; closer levels may fail to break.',
        formula='ATR target capped at the outer recent monthly range (weekly fallback); nearer levels recorded as obstacles.')
    return target, evidence


def multi_timeframe_structure(bars):
    """Daily confirmed zones plus observed weekly/monthly range boundaries.

    Period highs/lows are potential barriers, not confirmed swing zones. The
    current period uses only completed daily bars and is explicitly labelled
    evolving. Never treat a future resampling label as an observation date.
    """
    daily = daily_structure(bars)
    if not daily.get("available"):
        return daily
    result = {**daily, "method": "daily_weekly_monthly_v2",
              "resistance": [{**z, "timeframe": "daily", "kind": "confirmed_swing_zone"}
                             for z in daily["resistance"]],
              "support": [{**z, "timeframe": "daily", "kind": "confirmed_swing_zone"}
                          for z in daily["support"]]}
    as_of = pd.Timestamp(bars.index[-1])
    for timeframe, frequency, limit in (("weekly", "W-FRI", 104), ("monthly", "ME", 60)):
        for end, group in list(bars.resample(frequency))[-limit:]:
            if group.empty:
                continue
            # Current period is evolving unless its final calendar day is in
            # the supplied completed daily bars (conservative on holidays).
            complete = end.date() <= as_of.date()
            for kind, column, aggregate in (("resistance", "High", max), ("support", "Low", min)):
                value = float(aggregate(group[column]))
                if not math.isfinite(value) or value <= 0:
                    continue
                result[kind].append({"low": value, "high": value,
                                     "timeframe": timeframe, "kind": "period_extreme",
                                     "period_complete": complete,
                                     "period_end": end.isoformat(),
                                     "source_through": group.index[-1].isoformat(),
                                     "daily_bars": len(group)})
    result["timeframes"] = ["daily", "weekly", "monthly"]
    return result
