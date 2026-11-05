"""Batch migration for duplicated operational JSON; detailed trade events are unchanged."""
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import workflow_store


def migrate(database: Path, *, apply: bool, batch_size: int = 10):
    conn=sqlite3.connect(database,timeout=120);conn.row_factory=sqlite3.Row
    totals={'rows':0,'before_bytes':0,'after_bytes':0,'tables':{}}
    try:
        for table,column,extra in (('signals','payload',''),
                ('agent_events','details',"AND action IN ('dynamic_news_research','news_strategy_cycle')")):
            cursor=0;changed=0
            while True:
                rows=conn.execute(f'SELECT id,{column} value FROM {table} WHERE id>? {extra} ORDER BY id LIMIT ?',
                                  (cursor,int(batch_size))).fetchall()
                if not rows:break
                for row in rows:
                    cursor=int(row['id'])
                    try:value=json.loads(row['value'] or '{}')
                    except (TypeError,ValueError):continue
                    compact=json.dumps(workflow_store.compact_operational_detail(value),sort_keys=True)
                    if len(compact)>=len(row['value']):continue
                    totals['rows']+=1;changed+=1;totals['before_bytes']+=len(row['value']);totals['after_bytes']+=len(compact)
                    if apply:conn.execute(f'UPDATE {table} SET {column}=? WHERE id=?',(compact,cursor))
                if apply:conn.commit()
            totals['tables'][table]=changed
        totals['reclaimable_bytes']=totals['before_bytes']-totals['after_bytes'];totals['applied']=apply
        return totals
    finally:conn.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--db',required=True,type=Path);parser.add_argument('--apply',action='store_true');parser.add_argument('--batch-size',type=int,default=10)
    args=parser.parse_args();print(json.dumps(migrate(args.db,apply=args.apply,batch_size=args.batch_size),sort_keys=True),flush=True)
