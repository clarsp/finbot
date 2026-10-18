"""Recompute linked demo targets; --apply submits eligible changes under account lease.

Run with FINBOT_DB_PATH pointing at the active mounted DB. No orders or exits.
"""
from contextlib import nullcontext
import argparse
import asyncio
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import makemoney as app
from price_structure import multi_timeframe_structure, structure_target

async def main(apply=False, trade_id=None):
    settings=app.IntegrationSettings.from_environment()
    if settings.broker_provider!='etoro' or not settings.etoro_demo_trading:
        raise ValueError('Requires eToro demo configuration')
    report={'created_at':datetime.now(timezone.utc).isoformat(),'apply':apply,'trades':[]}
    async with (app.workflow_store.lease(app.get_connection, 'etoro_demo_account') if apply else nullcontext()):
        adapter=app.EtoroBrokerAdapter(settings)
        snapshot=await asyncio.to_thread(adapter.get_positions)
        if not snapshot.get('available'):
            raise ValueError('Complete broker positions unavailable')
        c=app.get_connection()
        trades=[dict(r) for r in c.execute("SELECT * FROM trades WHERE broker_position_id IS NOT NULL AND status IN ('OPEN','RECONCILIATION_REQUIRED')")]
        c.close()
        if trade_id is not None:
            trades=[t for t in trades if t["id"] == trade_id]
            if not trades:
                raise ValueError("Requested trade has no linked open position")
        rules=app.get_agent_rules()['risk_manager']
        for trade in trades:
            row={'id':trade['id'],'symbol':trade['symbol'],'status':'unchanged'}
            report['trades'].append(row)
            try:
                matches=[p for p in snapshot['positions'] if str(p.get('position_id'))==str(trade['broker_position_id'])]
                if len(matches)!=1:
                    row.update(status='skipped',reason='No unique open broker position');continue
                pos=matches[0];side=trade['side'];sign=1 if side=='LONG' else -1
                bars=await asyncio.to_thread(app.load_daily_bars,trade['symbol'],'5y')
                quote=await app.PositionManagerAgent().get_position_quote(adapter,pos)
                review=app.RiskManagerAgent().review_open_position_target(trade,pos,quote,bars)
                row.update(review)
                if row['status']!='eligible':
                    continue
                stop=row['stop'];target=row['new_target'];evidence=row['evidence']
                if apply:
                    result=await app.update_etoro_demo_protection.__wrapped__(trade['id'],stop,target)
                    c=app.get_connection();current=c.execute('SELECT policy_json FROM trades WHERE id=?',(trade['id'],)).fetchone()
                    policy=json.loads(current['policy_json'] or '{}');policy['target_selection']=evidence;policy['fill_adjusted']=True;policy['target_offset']=target-row['entry'];policy['range_target_reviewed_at']=datetime.now(timezone.utc).isoformat()
                    c.execute('UPDATE trades SET policy_json=? WHERE id=?',(json.dumps(policy),trade['id']));c.commit();c.close()
                    app.record_trade_event(trade['id'],'protection','risk_manager','completed','accepted','Risk Manager range-boundary target recomputed; stop preserved; broker verification pending.',row)
                    row['status']='submitted_pending_verification'
                    row['broker_update_status']=result['status']
            except Exception as error:
                row.update(status='failed',reason=f'{type(error).__name__}: {error}')
            print(json.dumps({k:v for k,v in row.items() if k!='evidence'}),flush=True)
        if apply:
            fresh=await asyncio.to_thread(adapter.get_positions)
            if fresh.get('available'):
                app.reconcile_news_demo_positions(fresh['positions'])
        path=app.DB_PATH.parent/'audit_logs'/('structure-targets-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,indent=2));print('Report:',path)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--trade-id',type=int)
    args=parser.parse_args()
    asyncio.run(main(args.apply, args.trade_id))
