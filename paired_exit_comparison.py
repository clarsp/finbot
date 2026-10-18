"""Offline paired exit experiments. No broker routing or database mutations."""
from dataclasses import dataclass
import math
import pandas as pd
from price_structure import multi_timeframe_structure, structure_target


@dataclass(frozen=True)
class ExitPolicy:
    name: str
    stop_atr: float
    target_atr: float
    structure: bool


POLICIES = (
    ExitPolicy('atr_2stop_3target', 2, 3, False),
    ExitPolicy('structure_2stop_3target', 2, 3, True),
    ExitPolicy('structure_2stop_1target', 2, 1, True),
    ExitPolicy('structure_3stop_1target', 3, 1, True),
    ExitPolicy('structure_3stop_2target', 3, 2, True),
)


ATR_ONLY_POLICIES = (
    ExitPolicy('atr_2stop_3target', 2, 3, False),
    ExitPolicy('atr_2stop_1target', 2, 1, False),
    ExitPolicy('atr_3stop_1target', 3, 1, False),
    ExitPolicy('atr_3stop_2target', 3, 2, False),
)


def timestamp(value):
    t = pd.Timestamp(value)
    return t.tz_localize('UTC') if t.tzinfo is None else t.tz_convert('UTC')


def daily_before_entry(bars, entered_at):
    """Conservatively exclude the entire entry session, including after-hours.

    The source is split-adjusted, dividend-unadjusted Yahoo OHLC. Retrospective
    vendor corrections remain a limitation; never use a later daily candle.
    """
    if bars is None or bars.empty:
        return pd.DataFrame()
    frame = bars.copy().sort_index()
    idx = pd.DatetimeIndex(frame.index)
    if idx.tz is None:
        raise ValueError('Exchange-local daily history must have a timezone')
    local_date = timestamp(entered_at).tz_convert(idx.tz).date()
    frame = frame.loc[[d.date() < local_date for d in idx]]
    frame = frame.dropna(subset=['Open','High','Low','Close'])
    previous = frame['Close'].shift()
    tr = pd.concat([frame['High']-frame['Low'], (frame['High']-previous).abs(),
                    (frame['Low']-previous).abs()],axis=1).max(axis=1)
    frame['atr14'] = tr.rolling(14,min_periods=14).mean()
    return frame


def entry_levels(trade, daily, policy):
    entry, atr = trade['entry'], trade['atr14']
    sign = 1 if trade['side']=='LONG' else -1
    stop = entry-sign*policy.stop_atr*atr
    target = entry+sign*policy.target_atr*atr
    evidence = {'adjusted':False}
    if policy.structure:
        structure = multi_timeframe_structure(daily)
        if not structure.get('available'):
            return {'status':'unavailable','reason':'Entry-time structure unavailable'}
        target, evidence = structure_target(trade['side'],entry,target,atr,structure)
    if stop <= 0 or target <= 0 or sign*(target-entry) <= 0:
        return {'status':'blocked','reason':'Entry is at/beyond opposing buffered structure or invalid price',
                'stop':stop,'target':target,'target_selection':evidence}
    return {'status':'eligible','stop':stop,'target':target,
            'gross_reward_risk':sign*(target-entry)/(policy.stop_atr*atr),
            'target_selection':evidence}


def replay(trade, bars, levels, horizon_hours, now):
    """Replay completed 5m candles; gap stops fill at open, ambiguous bars at stop.

    Exclude the partial entry candle. Time exit uses the first observed open
    at/after deadline; if none exists it is censored, even over a weekend.
    Missing off-hours quotes cannot be reconstructed from Yahoo candles.
    """
    entered, now = timestamp(trade['entered_at']), timestamp(now)
    deadline = entered+pd.Timedelta(hours=horizon_hours)
    if levels['status'] != 'eligible':
        return {'status':levels['status'],'reason':levels.get('reason')}
    if bars is None or bars.empty:
        return {'status':'unavailable','reason':'No 5-minute bars'}
    frame = bars.copy().sort_index()
    frame.index = pd.DatetimeIndex(frame.index).tz_convert('UTC')
    frame = frame.loc[(frame.index >= entered.ceil('5min')) &
                      (frame.index+pd.Timedelta(minutes=5) <= now)]
    frame=frame.dropna(subset=['Open','High','Low','Close'])
    if frame.empty:
        return {'status':'unavailable','reason':'No complete post-entry candles'}
    sign = 1 if trade['side']=='LONG' else -1
    stop,target=levels['stop'],levels['target']
    initial_gap = max(0,(frame.index[0]-entered).total_seconds()/60)
    common={'entry_gap_minutes':initial_gap,'entry_coverage_ok':initial_gap<=10,
            'deadline':deadline.isoformat(),'bars_available':len(frame)}
    previous=None
    same_session_gaps=0
    boundary_ambiguity=False
    for at,bar in frame.iterrows():
        if previous is not None:
            prevlocal=previous.tz_convert('America/New_York');local=at.tz_convert('America/New_York')
            if prevlocal.date()==local.date() and (at-previous).total_seconds()>30*60:
                same_session_gaps+=1
        previous=at
        op,high,low=(float(bar[k]) for k in ('Open','High','Low'))
        if any(not math.isfinite(v) or v<=0 for v in (op,high,low)):
            return {**common,'status':'unavailable','reason':'Invalid OHLC prices'}
        if at < deadline < at+pd.Timedelta(minutes=5):
            boundary_ambiguity |= (low<=stop or high>=target) if sign==1 else (high>=stop or low<=target)
            continue
        if sign*(op-stop)<=0:
            kind,price,exited='gap_stop',op,at
        elif sign*(op-target)>=0:
            # Conservative: no assumed favorable improvement on target gaps.
            kind,price,exited='gap_target',target,at
        elif at>=deadline:
            kind,price,exited='time_exit',op,at
        else:
            stop_hit=low<=stop if sign==1 else high>=stop
            target_hit=high>=target if sign==1 else low<=target
            if not (stop_hit or target_hit):
                continue
            kind='ambiguous_stop_first' if stop_hit and target_hit else 'stop' if stop_hit else 'target'
            price=stop if stop_hit else target
            exited=at+pd.Timedelta(minutes=5)
        hours=max(0,(exited-entered).total_seconds()/3600)
        per_unit=sign*(price-trade['entry'])
        return {**common,'status':'complete','exit':kind,'exit_price':price,
                'exit_at':exited.isoformat(),'hours_held':hours,'deadline_bar_ambiguity':boundary_ambiguity,
                'gross_per_unit':per_unit,'same_session_data_gaps':same_session_gaps}
    mark=float(frame.iloc[-1]['Close'])
    return {**common,'status':'censored','reason':'Neither barrier exited and no observed executable time-exit quote yet',
            'last_observed_at':(frame.index[-1]+pd.Timedelta(minutes=5)).isoformat(),
            'last_close':mark,'unrealized_gross_per_unit':sign*(mark-trade['entry']),
            'same_session_data_gaps':same_session_gaps}


def economics(trade, result, policy):
    if result['status']!='complete':
        return result
    entry,units=trade['entry'],trade['units']
    cost=trade['cost_model']
    per_unit=entry*(cost['roundtrip_bps']/10000 + cost['financing_annual_pct']/100*result['hours_held']/(365*24))
    # Keep documented entry-time broker fees separate; never use eventual
    # realised charges to price a different hypothetical holding period.
    fixed=cost['known_opening_fees']
    risk_units=units*min(1,2/policy.stop_atr)
    out={**result,'model_costs_actual_units':per_unit*units+fixed,
         'gross_pnl_actual_units':result['gross_per_unit']*units,
         'net_pnl_actual_units':result['gross_per_unit']*units-per_unit*units-fixed,
         'equal_risk_units':risk_units,
         'net_pnl_equal_risk':(result['gross_per_unit']-per_unit)*risk_units-fixed,
         'equal_risk_stop_budget':2*trade['atr14']*units}
    out['net_pnl_equal_risk_extra_10bps']=out['net_pnl_equal_risk']-entry*risk_units*.001
    out['net_return_on_margin_pct']=out['net_pnl_actual_units']/(units*entry/trade['leverage'])*100
    return out
