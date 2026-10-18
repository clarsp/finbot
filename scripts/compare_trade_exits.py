"""Exploratory paired exit replay on a consistent OFFLINE database snapshot.

Never imports the trading worker or submits orders. Yahoo OHLC is a price
proxy, not executable eToro bid/ask history. Output never authorizes trades.
"""
import argparse
from collections import Counter
from dataclasses import asdict
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
import yfinance as yf
from paired_exit_comparison import ATR_ONLY_POLICIES,POLICIES,timestamp,daily_before_entry,entry_levels,replay,economics


def parse(value):
    try:
        data=json.loads(value or '{}')
        return data if isinstance(data,dict) else {}
    except (ValueError,TypeError):
        return {}


def extract(database):
    connection=sqlite3.connect(f'file:{database}?mode=ro&immutable=1',uri=True)
    connection.row_factory=sqlite3.Row
    rows=connection.execute("SELECT * FROM trades WHERE broker_fill_price>0 AND broker_fill_units>0 AND (broker_order_id IS NOT NULL OR broker_position_id IS NOT NULL) ORDER BY id").fetchall()
    snapshots={}
    for row in connection.execute('SELECT payload FROM broker_snapshots ORDER BY id DESC'):
        payload=parse(row['payload'])
        for position in (payload.get('positions') or {}).get('positions',[]):
            key=str(position.get('position_id'))
            if key not in snapshots and position.get('opened_at'):
                snapshots[key]=position
    selected=[];excluded=[]
    for row in rows:
        t=dict(row);policy=parse(t['policy_json']);config=parse(t['config_json'])
        events=[dict(r) for r in connection.execute('SELECT stage,decision,details FROM trade_events WHERE trade_id=? ORDER BY id',(t['id'],))]
        risks=[parse(e['details']) for e in events if e['stage']=='risk' and e['decision']=='accepted']
        risk=next((r for r in reversed(risks) if r.get('atr14')), {})
        atr=policy.get('atr14') or risk.get('atr14')
        broker=snapshots.get(str(t.get('broker_position_id')), {})
        opened=t.get('opened_at') or broker.get('opened_at')
        reason=None
        if t['side'] not in ('LONG','SHORT'):reason='Unsupported side'
        elif policy.get('test_workflow_id'):reason='Saved test fixture excluded from strategy sample'
        elif policy.get('framework')=='CONDITIONAL_MEAN_REVERSION':reason='Mean-reversion exits are a different strategy; excluded'
        elif not opened:reason='No broker-confirmed entry timestamp'
        elif (broker.get('raw') or {}).get('isPartiallyAltered'):reason='Partially altered position cannot be replayed as one unchanged entry'
        if reason:
            excluded.append({'id':t['id'],'symbol':t['symbol'],'reason':reason});continue
        rules=config.get('risk_manager') or {}
        costs=(policy.get('broker_cost_evidence') or {}).get('response',{}).get('costs',[])
        known=sum(max(0,float(c.get('value') or 0)) for c in costs
                  if c.get('currency')=='USD' and c.get('costType') not in ('overnightFee','overWeekendFee'))
        hold=policy.get('holding_hours') or (risk.get('holding_hours'))
        selected.append({'id':t['id'],'symbol':t['symbol'],'side':t['side'],
            'entered_at':opened,'entry_timestamp_source':'trade fill record' if t.get('opened_at') else 'stored broker portfolio snapshot','entry':float(t['broker_fill_price']),
            'units':float(t['broker_fill_units']),'leverage':float(t['leverage']),
            'atr14':float(atr) if atr else None,'atr_source':'saved entry evidence' if atr else 'reconstructed from completed pre-entry daily bars',
            'holding_hours':float(hold) if hold else 48.,'holding_source':'saved entry policy' if hold else '48h assumed for missing legacy policy',
            'strategy_version':t.get('strategy_version'),'minimum_reward_risk':float(rules.get('minimum_reward_risk',1)),
            'cost_model':{'roundtrip_bps':float(rules.get('estimated_roundtrip_cost_bps',10)),
                          'financing_annual_pct':float(rules.get('estimated_financing_annual_pct',8)),
                          'known_opening_fees':known,'source':'captured entry rules' if rules else 'assumed defaults: 10bps roundtrip and 8% annual financing',
                          'missing':'historical executable bid/ask, precise holding-dependent broker fees, slippage; sensitivity adds 10bps'}})
    connection.close()
    return selected,excluded


def fetch(symbol,start,end,interval,cache):
    path=cache/f'{symbol.replace("/","_")}-{interval}.csv'
    if path.exists():
        frame=pd.read_csv(path,index_col=0,parse_dates=True)
        index=pd.DatetimeIndex(pd.to_datetime(frame.index,utc=True))
        frame.index=index.tz_convert('America/New_York')
        return frame
    ticker=yf.Ticker(symbol)
    frame=ticker.history(start=start,end=end,interval=interval,auto_adjust=False,
                         actions=True,prepost=interval=='5m',raise_errors=True)
    if frame is not None and not frame.empty:
        frame.to_csv(path)
    return frame


def aggregate(results):
    groups=[]
    for horizon in ('recorded','7d','14d'):
        for policy in POLICIES:
            sample=[r for r in results if r['horizon']==horizon and r['policy']==policy.name]
            complete=[r for r in sample if r['status']=='complete']
            high_quality=[r for r in complete if r.get('entry_coverage_ok') and not r.get('same_session_data_gaps') and not r.get('deadline_bar_ambiguity')]
            paired=[]
            for r in sample:
                baseline=next((b for b in results if b['id']==r['id'] and b['horizon']==horizon and b['policy']==POLICIES[0].name),None)
                if r['status']=='complete' and baseline and baseline['status']=='complete' and r.get('entry_coverage_ok') and baseline.get('entry_coverage_ok') and not r.get('same_session_data_gaps') and not r.get('deadline_bar_ambiguity') and not baseline.get('same_session_data_gaps') and not baseline.get('deadline_bar_ambiguity'):
                    paired.append((r,baseline))
            n=len(paired)
            groups.append({'horizon':horizon,'policy':policy.name,'counts':dict(Counter(r['status'] for r in sample)),
                'complete_with_entry_coverage':len(high_quality),'paired_complete_count':n,
                'paired_net_equal_risk':sum(r['net_pnl_equal_risk'] for r,b in paired) if n else None,
                'paired_net_actual_units':sum(r['net_pnl_actual_units'] for r,b in paired) if n else None,
                'paired_mean_net_r':sum(r['net_pnl_equal_risk']/r['equal_risk_stop_budget'] for r,b in paired)/n if n else None,
                'paired_mean_delta_r':sum((r['net_pnl_equal_risk']-b['net_pnl_equal_risk'])/r['equal_risk_stop_budget'] for r,b in paired)/n if n else None,
                'paired_delta_equal_risk':sum(r['net_pnl_equal_risk']-b['net_pnl_equal_risk'] for r,b in paired) if n else None,
                'paired_net_stressed':sum(r['net_pnl_equal_risk_extra_10bps'] for r,b in paired) if n else None,
                'paired_mean_hours':sum(r['hours_held'] for r,b in paired)/n if n else None,
                'paired_wins':sum(r['net_pnl_equal_risk']>0 for r,b in paired),
                'paired_ambiguous_stops':sum(r['exit']=='ambiguous_stop_first' for r,b in paired)})
    return groups


def write_report(out,report):
    (out/'results.json').write_text(json.dumps(report,indent=2,default=str))
    flat=[{k:v for k,v in r.items() if not isinstance(v,(dict,list))} for r in report['results']]
    if flat:
        fields=list(dict.fromkeys(k for r in flat for k in r))
        with (out/'trades.csv').open('w',newline='') as f:
            writer=csv.DictWriter(f,fields);writer.writeheader();writer.writerows(flat)
    lines=['# Historical paired exit comparison','',
       'Exploratory counterfactuals on recorded broker fills. No broker changes. No strategy is validated by this sample.',
       '',f"Observed through: {report['as_of']} UTC",f"Recorded fills considered: {len(report['trades'])}; excluded before download: {len(report['excluded'])}",
       'Design: '+report.get('design','structure')+(' — additional diagnostic controls after reviewing the structure experiment; not independent validation.' if report.get('design')=='atr-controls' else ' — primary structure experiment.'),
       '', '## Method', '',
       '- Fixed exit rules, each tested at the saved holding horizon, seven days and fourteen days.',
       '- Same recorded entries and broker units; a second calculation reduces 3-ATR-stop units to two-thirds to preserve the baseline 2-ATR dollar stop budget.',
       '- Baseline is 2-ATR stop / 3-ATR target without structure. The primary structure design caps alternatives at entry-time daily/weekly/monthly structure minus/plus 0.1 ATR. The ATR-only diagnostic design deliberately removes that cap to isolate ATR distance effects.',
       '- Entry-session daily candles are excluded; no later highs/lows are used to set levels. Missing original ATR is reconstructed and labelled.',
       '- First partial entry candle excluded. Adverse gaps fill at observed open; favorable gaps fill at target; ambiguous candles assume stop first.',
       '- Time exits require an observed quote candle at/after deadline; incomplete paths are censored. Early exits are complete but cannot be paired against an unfinished baseline.',
       '- Entry gaps over ten minutes and intraday gaps over thirty minutes are excluded from the paired summary. Yahoo pre/post-market OHLC still lacks eToro overnight executable quotes.',
       '- Costs use entry-time configuration or labelled defaults, documented opening fees, and actual simulated duration. An extra 10bps cost sensitivity is reported.',
       '- Paired totals sum independent trade scenarios, not an executable portfolio equity curve. Overlapping positions, cash constraints and compounding are not simulated.',
       '- Entry reward/risk gate violations are labelled, not enforced, to evaluate the proposed alternative exit rules without changing application gates.',
       '- No untouched validation period exists yet. Do not choose a winning policy from these data alone.',
       '- Complete-pair selection can bias results because completed and unfinished paths have different outcomes; these totals are descriptive, not an unbiased expectancy estimate.',
       '- Each row uses its own complete matched subset; compare each paired difference to zero, not raw dollar totals across rows. Legacy trade sizes differ greatly, so mean risk-normalised results are also shown.',
       '- A recorded holding limit is a counterfactual exit deadline, not proof that the broker actually executed the application close on time. Legacy entries with no saved horizon use an explicit 48h assumption.',
       '- The saved minimum reward/risk gate is an approximate comparison label only; this is not a replay of every historical portfolio gate, news invalidation or gap-stress check.',
       '', '## Paired completed paths', '',
       '| Horizon | Policy | Complete pairs | Net, equal risk | Difference vs baseline | Net +10bps stress | Mean net R | Mean delta R | Mean hours |',
       '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    fmt=lambda v:'—' if v is None else f'{v:.2f}'
    for g in report['summary']:
        lines.append(f"| {g['horizon']} | {g['policy']} | {g['paired_complete_count']} | {fmt(g['paired_net_equal_risk'])} | {fmt(g['paired_delta_equal_risk'])} | {fmt(g['paired_net_stressed'])} | {fmt(g['paired_mean_net_r'])} | {fmt(g['paired_mean_delta_r'])} | {fmt(g['paired_mean_hours'])} |")
    lines+=['','## Completion and policy blocks','',
            '| Horizon | Policy | Complete | Censored | Blocked | Paired with baseline |',
            '|---|---|---:|---:|---:|---:|']
    for g in report['summary']:
        counts=g['counts']
        lines.append(f"| {g['horizon']} | {g['policy']} | {counts.get('complete',0)} | {counts.get('censored',0)} | {counts.get('blocked',0)} | {g['paired_complete_count']} |")
    lines+=['','## Price scale (descriptive, not a trading gate)','',
            '| Entry price band | Trades with data | Median daily ATR dollars | Median ATR / entry |',
            '|---|---:|---:|---:|']
    for label,low,high in [('<50',0,50),('50–200',50,200),('200–500',200,500),('500+',500,float('inf'))]:
        sample=[t for t in report['trades'] if t.get('data_status','').startswith('downloaded') and low<=t['entry']<high]
        if sample:
            lines.append(f"| {label} | {len(sample)} | {pd.Series([t['atr14'] for t in sample]).median():.2f} | {pd.Series([t['atr_pct'] for t in sample]).median():.2f}% |")
    lines+=['','## MSFT and Mastercard','']
    for t in report['trades']:
        if t['symbol'] not in ('MSFT','MA'):continue
        lines.append(f"### #{t['id']} {t['symbol']}")
        lines+=['',f"ATR: {t.get('atr14')} ({t['atr_source']}); entry {t['entry']}; structure uses bars through {t.get('structure_as_of','unavailable')}.",'',
                '| Policy | Target | Stop | Status | Exit | Net at actual units | Hours | Entry gap minutes |',
                '|---|---:|---:|---|---|---:|---:|---:|']
        for row in report['results']:
            if row['id']==t['id'] and row['horizon']=='recorded':
                lines.append(f"| {row['policy']} | {fmt(row.get('target'))} | {fmt(row.get('stop'))} | {row['status']} | {row.get('exit','—')} | {fmt(row.get('net_pnl_actual_units'))} | {fmt(row.get('hours_held'))} | {fmt(row.get('entry_gap_minutes'))} |")
        lines+=['','Values outside the complete paired cohort are diagnostic estimates only. Blocked means no valid target before the entry-time opposing barrier; it is not a realised loss.','']
    lines+=['','## Data and exclusions','']
    for t in report['trades']:
        lines.append(f"- #{t['id']} {t['symbol']}: entry {t['entry']:.4f}, ATR {t.get('atr14')}, ATR% {t.get('atr_pct')}, {t['atr_source']}; {t.get('data_status','pending')}.")
    for e in report['excluded']:
        lines.append(f"- Excluded #{e['id']} {e['symbol']}: {e['reason']}")
    (out/'report.md').write_text('\n'.join(lines)+'\n')


def main():
    global POLICIES
    p=argparse.ArgumentParser();p.add_argument('--database',required=True,type=Path);p.add_argument('--output',required=True,type=Path);p.add_argument('--design',choices=['structure','atr-controls'],default='structure');p.add_argument('--market-cache',type=Path);p.add_argument('--as-of')
    args=p.parse_args()
    if args.design == "atr-controls":
        POLICIES = ATR_ONLY_POLICIES
    if args.database.resolve()==Path('/data/finbot.db') or 'backup' not in str(args.database) and 'experiment' not in str(args.database):
        raise ValueError('Provide an offline snapshot in a backups/experiments directory, not the live database')
    out=args.output;out.mkdir(parents=True,exist_ok=True);cache=args.market_cache or out/'market-data';cache.mkdir(parents=True,exist_ok=True)
    yf.set_tz_cache_location(str(cache/'yahoo-cache'))
    manifest_path=out/'manifest.json'
    existing=json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    now=timestamp(existing['as_of']) if existing else timestamp(args.as_of) if args.as_of else pd.Timestamp.now(tz='UTC')
    trades,excluded=extract(args.database.resolve())
    first_entries={symbol:min(timestamp(t['entered_at']) for t in trades if t['symbol']==symbol)
                   for symbol in {t['symbol'] for t in trades}}
    with args.database.open('rb') as source:
        fingerprint=hashlib.file_digest(source,'sha256').hexdigest()
    if existing and (existing.get('input_sha256')!=fingerprint or existing.get('design','structure')!=args.design):
        raise ValueError('Existing experiment uses a different input snapshot; choose another output directory')
    # Freeze the experimental design before requesting outcomes.
    manifest={'design':args.design,'input_sha256':fingerprint,'as_of':now.isoformat(),'policies':[asdict(x) for x in POLICIES],
              'horizons':['recorded','7d','14d'],'trades':trades,'excluded':excluded}
    manifest_path.write_text(json.dumps(manifest,indent=2))
    results=[]
    for trade in trades:
        try:
            entered=timestamp(trade['entered_at'])
            daily=fetch(trade['symbol'],(entered-pd.Timedelta(days=5*366)).date().isoformat(),
                        (now+pd.Timedelta(days=1)).date().isoformat(),'1d',cache)
            before=daily_before_entry(daily,entered)
            if before.empty or len(before)<20 or pd.isna(before.iloc[-1]['atr14']):
                raise ValueError('Insufficient completed pre-entry daily history')
            if trade['atr14'] is None:trade['atr14']=float(before.iloc[-1]['atr14'])
            if not math_is_positive(trade['atr14']):raise ValueError('Invalid entry ATR')
            trade['atr_pct']=100*trade['atr14']/trade['entry']
            split=daily.get('Stock Splits')
            if split is not None and (split.loc[split.index.tz_convert('UTC')>=entered]!=0).any():
                raise ValueError('Corporate split after entry makes price basis incomparable')
            bars=fetch(trade['symbol'],first_entries[trade['symbol']].date().isoformat(),(now+pd.Timedelta(days=1)).date().isoformat(),'5m',cache)
            if bars is None or bars.empty:raise ValueError('No five-minute history')
            trade['data_status']='downloaded; Yahoo pre/post-market proxy'
            trade['structure_as_of']=before.index[-1].isoformat()
            for policy in POLICIES:
                levels=entry_levels(trade,before,policy)
                for horizon,hours in [('recorded',trade['holding_hours']),('7d',168),('14d',336)]:
                    result=replay(trade,bars,levels,hours,now)
                    result=economics(trade,result,policy)
                    rr=levels.get('gross_reward_risk')
                    forecast_cost=trade['entry']*(trade['cost_model']['roundtrip_bps']/10000+trade['cost_model']['financing_annual_pct']/100*hours/(365*24))+trade['cost_model']['known_opening_fees']/trade['units']
                    net_rr=((levels['target']-trade['entry'])*(1 if trade['side']=='LONG' else -1)-forecast_cost)/(policy.stop_atr*trade['atr14']) if levels['status']=='eligible' else None
                    results.append({'id':trade['id'],'symbol':trade['symbol'],'policy':policy.name,'horizon':horizon,
                        'horizon_hours':hours,'entry':trade['entry'],'atr14':trade['atr14'],'atr_pct':trade['atr_pct'],
                        'stop':levels.get('stop'),'target':levels.get('target'),
                        'gross_reward_risk':rr,'forecast_net_reward_risk':net_rr,
                        'would_pass_saved_minimum_rr':net_rr is not None and net_rr>=trade['minimum_reward_risk'],
                        'target_selection':levels.get('target_selection'),**result})
            print(f"#{trade['id']} {trade['symbol']}: {dict(Counter(r['status'] for r in results if r['id']==trade['id']))}",flush=True)
        except Exception as error:
            trade['data_status']=f'unavailable: {type(error).__name__}: {error}'
            print(f"#{trade['id']} {trade['symbol']}: {trade['data_status']}",flush=True)
    report={**manifest,'trades':trades,'results':results,'summary':aggregate(results)}
    write_report(out,report)
    print('Report:',out/'report.md',flush=True)


def math_is_positive(value):
    import math
    return math.isfinite(value) and value>0


if __name__=='__main__':main()
