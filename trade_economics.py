"""Position-size arithmetic and explicitly sourced profit scenarios, without I/O."""
import json
import math


def number(value, positive=False):
    try:
        value=float(value)
        return value if math.isfinite(value) and (not positive or value>0) else None
    except (TypeError,ValueError):
        return None


def object_value(value):
    if isinstance(value,dict):
        return value
    try:
        parsed=json.loads(value or '{}')
        return parsed if isinstance(parsed,dict) else {}
    except (TypeError,ValueError):
        return {}


def position_economics(trade):
    policy=object_value(trade.get('policy_json'))
    observation=policy.get('broker_observation') or {}
    units=number(trade.get('broker_fill_units'),True) or number(trade.get('quantity'),True)
    entry=number(trade.get('broker_fill_price'),True) or number(trade.get('entry_price'),True)
    leverage=number(observation.get('leverage'),True) or number(trade.get('leverage'),True)
    mark=number(observation.get('current_price'),True)
    margin=number(observation.get('margin'),True) or number(trade.get('allocated_capital'),True)
    margin_source='broker reported' if number(observation.get('margin'),True) else 'recorded allocation'
    opening=units*entry if units and entry else None
    if margin is None and opening and leverage:
        margin=opening/leverage; margin_source='estimated from exposure / leverage'
    running=number(observation.get('reported_running_pnl'))
    warnings=[]
    if not observation:
        warnings.append('A broker observation is not yet available; current exposure and broker running P/L are unavailable.')
    elif mark is None:
        warnings.append('Current broker quote unavailable; opening price is not substituted for current exposure.')
    costs=number(policy.get('estimated_costs'))
    cost_units=number((policy.get('sizing') or {}).get('units'),True)
    cost_source='saved submission cost forecast'
    if costs is None:
        risk=next((e.get('details') or {} for e in reversed(trade.get('events',[]))
                   if e.get('stage')=='risk' and number((e.get('details') or {}).get('estimated_costs')) is not None),{})
        costs=number(risk.get('estimated_costs'));cost_units=number((risk.get('sizing') or {}).get('units'),True)
        cost_source='saved risk-review cost forecast'
    if costs is not None and costs<0:
        costs=None
    if costs is not None and cost_units and units:
        costs=costs*units/cost_units
    elif costs is not None and not cost_units:
        warnings.append('Cost forecast quantity was not saved; it cannot be adjusted for position-size changes.')
        costs=None
    if costs is None:
        cost_source='unavailable'
        warnings.append('No usable total cost forecast is stored; net scenarios are unavailable, not zero-cost.')
    else:
        warnings.append('Net scenarios use a saved cost forecast, not final charges. Financing duration, closing fees and slippage may differ.')
    if observation.get('reported_fees') is None and trade.get('broker_fees') is None:
        warnings.append('Broker fees are not reported.')
    sign={'LONG':1,'SHORT':-1}.get(trade.get('side'))
    scenarios=[]
    for label,key in (('Take profit','take_profit_price'),('Stop loss','stop_loss_price')):
        price=number(trade.get(key),True)
        per_unit=sign*(price-entry) if sign and entry and price else None
        gross=per_unit*units if per_unit is not None and units else None
        net=gross-costs if gross is not None and costs is not None else None
        scenarios.append({'scenario':label,'price':price,'profit_per_unit':per_unit,
                          'gross_position_pnl':gross,'estimated_costs':costs,
                          'estimated_net_position_pnl':net,
                          'estimated_return_on_margin_pct':net/margin*100 if net is not None and margin else None})
    closed = trade.get('status') in {'CLOSED', 'CLOSED_PNL_UNAVAILABLE'}
    if closed:
        running = None
        mark = None
    realized=number(trade.get('realized_pnl')) if trade.get('status')=='CLOSED' else None
    return {'units':units,'entry_price':entry,'leverage':leverage,'margin':margin,
            'margin_source':margin_source,'opening_exposure':opening,
            'current_exposure':0.0 if closed else units*mark if units and mark else None,
            'current_price':mark,'broker_running_pnl':running,
            'observation_at':observation.get('observed_at'),
            'quote_observed_at':observation.get('quote_observed_at'),
            'reported_fees':number(observation.get('reported_fees')) if observation.get('reported_fees') is not None else number(trade.get('broker_fees')),
            'realized_pnl':realized,'cost_source':cost_source,
            'cost_assumptions':policy.get('cost_assumptions') or {},
            'broker_cost_evidence':policy.get('broker_cost_evidence') or {},
            'scenarios':scenarios,'warnings':warnings}
