"""Prospective, quote-observed exit comparisons. Never routes broker orders.

Plans are frozen by Risk Manager after normal entry gates; rows start only on
verified fills. Position Manager maintenance supplies executable bid/ask observations.
"""
from datetime import datetime, timedelta, timezone
import json
import math
from price_structure import structure_target


def timestamp(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS shadow_exits (
        trade_id INTEGER NOT NULL, variant TEXT NOT NULL, horizon_hours REAL NOT NULL,
        status TEXT NOT NULL, instrument_id TEXT NOT NULL, side TEXT NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(trade_id, variant, horizon_hours),
        FOREIGN KEY(trade_id) REFERENCES trades(id))''')
    conn.execute('CREATE INDEX IF NOT EXISTS shadow_active ON shadow_exits(status,instrument_id)')


def validate_rules(rules):
    for key, maximum in [('shadow_stop_atr_multiples', 10), ('shadow_target_atr_multiples', 10), ('shadow_horizons_hours', 720)]:
        values = rules.get(key, [])
        if not isinstance(values, list) or not 1 <= len(values) <= 4:
            raise ValueError(f'{key} requires 1–4 positive values.')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 < v <= maximum for v in values):
            raise ValueError(f'{key} values must be positive and at most {maximum}.')
    if not isinstance(rules.get('shadow_tracking_enabled'), bool):
        raise ValueError('shadow_tracking_enabled must be true or false.')


def build_plan(rules, risk, policy, instrument_id):
    if not rules.get('shadow_tracking_enabled', True) or not risk.get('execution_ready') or risk.get('decision') != 'accepted':
        return None
    if policy['framework'] != 'DIRECTIONAL_NEWS':
        return None
    validate_rules(rules)
    broker_costs = policy.get('broker_cost_evidence', {})
    opening_cost = sum(max(0, float(c.get('value') or 0))
        for c in broker_costs.get('response', {}).get('costs', [])
        if c.get('costType') not in {'overnightFee', 'overWeekendFee', 'marketSpread'}) if broker_costs.get('available') else 0
    # Deep copy preserves the approved entry-time evidence across future changes.
    return json.loads(json.dumps(dict(
        version=1, approved_at=datetime.now(timezone.utc).isoformat(),
        instrument_id=str(instrument_id), atr14=policy['atr14'],
        stop_offset=policy['stop_offset'], target_offset=policy['target_offset'],
        alternative_target_mode='nearest',
        target_selection=policy['target_selection'], approved_units=risk['proposed_units'],
        stop_multiples=rules['shadow_stop_atr_multiples'],
        target_multiples=rules['shadow_target_atr_multiples'],
        horizons=sorted(set([policy['holding_hours'], *rules['shadow_horizons_hours']])),
        minimum_reward_risk=rules['minimum_reward_risk'],
        costs=policy['cost_assumptions'],
        broker_opening_cost=opening_cost,
        opening_cost_source='Broker entry-time what-if, excluding spread and overnight fees' if broker_costs.get('available') else 'Unavailable; configured cost estimate only',
        requested_entry=policy['requested_entry'],
        limitations='Sampled executable quotes; intervening touches and fills are unknown. Alternatives are observations, not entry approvals.'
    )))


def initialize(conn, trade, position, now=None):
    policy = json.loads(trade.get('policy_json') or '{}')
    plan = policy.get('shadow_plan')
    if not plan:
        return 0  # Prospective cohort only; no invented historical observations.
    opened = position.get('opened_at') or trade.get('opened_at')
    if not opened:
        return 0  # Wait for actual broker fill time, never substitute creation time.
    entry, atr = float(position['open_rate']), float(plan['atr14'])
    actual_units = abs(float(position['units']))
    units = min(actual_units, float(plan['approved_units']))
    if not all(math.isfinite(v) and v > 0 for v in [entry, atr, units]):
        return 0
    sign = 1 if trade['side'] == 'LONG' else -1
    base_stop = abs(plan['stop_offset'])/atr
    baseline_target = entry + plan['target_offset']
    if plan['target_selection'].get('adjusted'):
        baseline_target = float(plan['target_selection']['selected_target'])
    baseline_atr = abs(float(plan['target_selection'].get('baseline_target', plan['requested_entry']+plan['target_offset']))-plan['requested_entry'])/atr
    variants = [('production', base_stop, baseline_atr, baseline_target)]
    for stop in plan['stop_multiples']:
        for target in plan['target_multiples']:
            raw = entry + sign*atr*target
            selected, _ = structure_target(trade['side'], entry, raw, atr, plan['target_selection'].get('structure'), mode=plan.get('alternative_target_mode','nearest'))
            variants.append((f'{stop:g}S/{target:g}T', stop, target, selected))
    created = 0
    for variant, stop, multiple, target in variants:
        quantity = actual_units if variant == 'production' else units * min(1, base_stop/stop)
        sl = entry - sign*atr*stop
        valid = sl > 0 and target > 0 and sign*(target-entry) > 0
        for hours in plan['horizons']:
            cost_per_unit = entry*(plan['costs'].get('roundtrip_bps', 10)/10000 + plan['costs'].get('financing_annual_pct', 8)/100*hours/8760)
            net_rr = (sign*(target-entry)-cost_per_unit-plan['broker_opening_cost']/units)/(atr*stop)
            data = dict(symbol=trade['symbol'], entry=entry, opened_at=timestamp(opened).isoformat(),
                units=quantity, actual_fill_units=actual_units, units_basis='Actual fill' if variant=='production' else 'Approved/fill quantity cap, equal initial stop risk', leverage=position.get('leverage') or trade['leverage'], atr14=atr,
                stop_atr_multiple=stop, requested_target_atr_multiple=multiple,
                target_selection_mode=plan['target_selection'].get('mode','nearest') if variant=='production' else plan.get('alternative_target_mode','nearest'),
                effective_target_atr_multiple=sign*(target-entry)/atr,
                atr_target=entry+sign*atr*multiple, target=target, stop=sl,
                deadline=(timestamp(opened)+timedelta(hours=hours)).isoformat(),
                created_at=(now or datetime.now(timezone.utc)).isoformat(),
                last_observed_at=None, observation_count=0, gap_count=0, max_gap_seconds=0,
                would_pass_net_reward_risk=net_rr >= plan['minimum_reward_risk'],
                estimated_net_reward_risk=net_rr, plan=plan)
            created += conn.execute('INSERT OR IGNORE INTO shadow_exits VALUES (?,?,?,?,?,?,?)',
                (trade['id'], variant, hours, 'ACTIVE' if valid else 'SKIPPED_INVALID_LEVELS', plan['instrument_id'], trade['side'], json.dumps(data))).rowcount
    return created


def observe(conn, instrument_id, rate, now=None):
    """One fresh API response per instrument, no cached or invented prices."""
    now = now or datetime.now(timezone.utc)
    rows = conn.execute("SELECT * FROM shadow_exits WHERE status='ACTIVE' AND instrument_id=?", (str(instrument_id),)).fetchall()
    completed = []
    for row in rows:
        price = rate.get('bid' if row['side'] == 'LONG' else 'ask')
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            continue
        data = json.loads(row['payload'])
        if now < timestamp(data['opened_at']) or (data['last_observed_at'] and now <= timestamp(data['last_observed_at'])):
            continue
        gap = (now-timestamp(data['last_observed_at'] or data['opened_at'])).total_seconds()
        data['max_gap_seconds'] = max(data['max_gap_seconds'], gap)
        data['gap_count'] += int(gap > 120)
        data.update(last_observed_at=now.isoformat(), last_price=price, observation_count=data['observation_count']+1)
        sign = 1 if row['side'] == 'LONG' else -1
        expired = now >= timestamp(data['deadline'])
        # A post-deadline quote cannot prove a target/stop touch before expiry.
        status = 'TIME_EXIT_OBSERVED' if expired else 'STOP_OBSERVED' if sign*(price-data['stop']) <= 0 else 'TARGET_OBSERVED' if sign*(price-data['target']) >= 0 else 'ACTIVE'
        if status != 'ACTIVE':
            elapsed = (now-timestamp(data['opened_at'])).total_seconds()/3600
            costs = data['plan']['costs']
            # Executable quotes already include spread: do not subtract it again.
            estimate = data['units']*data['entry']*(costs.get('roundtrip_bps', 10)/10000 + costs.get('financing_annual_pct', 8)/100*elapsed/8760)
            estimate += data['plan']['broker_opening_cost']*data['units']/data['plan']['approved_units']
            gross = sign*(price-data['entry'])*data['units']
            data.update(exit_at=now.isoformat(), exit_price=price, gross_pnl=gross,
                estimated_costs=estimate, estimated_net_pnl=gross-estimate,
                deadline_lateness_seconds=max(0, (now-timestamp(data['deadline'])).total_seconds()))
            completed.append(dict(trade_id=row['trade_id'], variant=row['variant'], horizon_hours=row['horizon_hours'], status=status))
        conn.execute('UPDATE shadow_exits SET status=?,payload=? WHERE trade_id=? AND variant=? AND horizon_hours=?',
            (status, json.dumps(data), row['trade_id'], row['variant'], row['horizon_hours']))
    return completed


def rows(conn, trade_id=None):
    where, params = ('WHERE s.trade_id=?', (trade_id,)) if trade_id is not None else ('', ())
    return [{**dict(r), **json.loads(r['payload'])} for r in conn.execute(f'SELECT s.* FROM shadow_exits s {where} ORDER BY trade_id DESC,horizon_hours,variant', params)]
