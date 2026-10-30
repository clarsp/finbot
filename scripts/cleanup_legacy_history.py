"""Preview or apply a backed-up removal of pre-eToro paper history.

Never contacts a broker. Apply requires a verified dated Tigris archive of the exact removed cohort.
Operational/billing/security data, news evidence, all broker records and shadows stay.
"""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
if Path('/app/history_retention.py').exists():sys.path.insert(0,'/app')
import argparse,base64,contextlib,fcntl,gzip,hashlib,json,os,signal,sqlite3,subprocess,tempfile,time,uuid,zlib
from datetime import datetime,timezone
from history_retention import MARKER,stamp,json_object,broker_linked,execution_evidence,retain_trade

LEASES=('news_research_cycle','etoro_demo_account','analysis_cleanup','performance_reviews')

def connect(path):
    c=sqlite3.connect(path,timeout=30);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c

def archive_content(payload):
    d=zlib.decompressobj();raw=d.decompress(payload,32*1024*1024)
    if not d.eof:raise ValueError('Archived lifecycle exceeds safety limit.')
    return json.loads(raw)

def fingerprint(c,table):
    h=hashlib.sha256();n=0
    for r in c.execute('SELECT * FROM '+table+' ORDER BY rowid'):
        values=[{'blob':base64.b64encode(v).decode()} if isinstance(v,bytes) else v for v in r]
        h.update(json.dumps(values,separators=(',',':'),ensure_ascii=False).encode());n+=1
    return {'rows':n,'sha256':h.hexdigest()}

def plan(c):
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'trading_profiles' in tables and c.execute('SELECT count(*) FROM trading_profiles').fetchone()[0]>1:raise ValueError('This cleanup requires the single-profile runtime.')
    live={int(r['id']):dict(r) for r in c.execute('SELECT * FROM trades')};archives={}
    for row in c.execute('SELECT trade_id,payload FROM archived_analysis'):archives[int(row['trade_id'])]=archive_content(row['payload'])
    all_trades={**{tid:a['trade'] for tid,a in archives.items()},**live}
    ledger={str(r[0]).removesuffix('.0') for r in c.execute('SELECT position_id FROM broker_closed_positions')}
    verified=[t for t in all_trades.values() if stamp(t.get('created_at')) and (str(t.get('broker_position_id') or '').removesuffix('.0') in ledger or (t.get('broker_position_id') and float(t.get('broker_fill_units') or 0)>0))]
    if not verified:raise ValueError('No broker-confirmed anchor: no history removed.')
    anchor=min(verified,key=lambda t:stamp(t['created_at']));cutoff=stamp(anchor['created_at']).isoformat()
    protected={int(r[0]) for table in ('shadow_exits','broker_actions','position_management') if table in tables for r in c.execute('SELECT DISTINCT trade_id FROM '+table)}
    protected.update(int(t['id']) for t in all_trades.values() if broker_linked(t))
    old_events={}
    for e in c.execute("SELECT * FROM trade_events WHERE julianday(created_at)<julianday(?)",(cutoff,)):old_events.setdefault(int(e['trade_id']),[]).append(dict(e))
    for tid,a in archives.items():
        if execution_evidence(a['events']):protected.add(tid)
    for tid,events in old_events.items():
        if execution_evidence(events):protected.add(tid)
    remove={tid for tid,t in all_trades.items() if not retain_trade(t,cutoff,protected,old_events.get(tid,()))}
    removed_live=sorted(remove&live.keys());removed_archive=sorted(remove&archives.keys())
    auxiliary={}
    for table,date in [('signals','created_at'),('agent_events','created_at'),('risk_events','created_at'),('research_cycle_companies','cycle_timestamp')]:
        if table not in tables:continue
        ids=[]
        for row in c.execute('SELECT * FROM '+table+' WHERE julianday('+date+')<julianday(?)',(cutoff,)):
            row=dict(row);detail=json_object(row.get('details'))
            refs=set()
            def references(v):
                if isinstance(v,dict):
                    for key,item in v.items():
                        if key in {'trade_id','trade_ids'}:
                            for value in item if isinstance(item,list) else [item]:
                                try:refs.add(int(value))
                                except (ValueError,TypeError):pass
                        elif isinstance(item,(dict,list)):references(item)
                elif isinstance(v,list):
                    for item in v:references(item)
            references(detail)
            if refs & (set(all_trades)-remove):continue
            if table=='agent_events' and execution_evidence([{'stage':'execution' if row.get('agent_name')=='execution' else '', 'decision':row.get('decision'),'details':row.get('details')}]):continue
            ids.append(row['id'])
        auxiliary[table]=ids
    # Any external trade foreign key must be handled explicitly, never cascaded silently.
    dependencies={}
    for table in tables-{'sqlite_sequence'}:
        for fk in c.execute('PRAGMA foreign_key_list('+table+')'):
            if fk['table']=='trades':
                column=fk['from'];ids=[r[0] for r in c.execute('SELECT rowid FROM '+table+' WHERE '+column+' IN ('+','.join('?' for _ in removed_live)+')',removed_live)] if removed_live else []
                if ids and table!='trade_events':raise ValueError('Protected dependency would be removed: '+table)
                if ids:dependencies[table]=ids
    # Rebuild the legacy company cache using retained lifecycles, rather than old totals.
    research={}
    for row in c.execute("SELECT trade_id,details,decision,created_at FROM trade_events WHERE stage='research' ORDER BY id"):
        tid=int(row['trade_id'])
        if tid not in remove and tid not in research:
            d=json_object(row['details']);research[tid]={'decision':row['decision'],'date':row['created_at'],'confidence':d.get('confidence'),'signal':d.get('signal')}
    for tid,a in archives.items():
        if tid in remove or tid in live:continue
        for e in a['events']:
            if e.get('stage')=='research':
                d=json_object(e.get('details'));research[tid]={'decision':e.get('decision'),'date':e.get('created_at'),'confidence':d.get('confidence'),'signal':d.get('signal')};break
    metadata={r['ticker']:dict(r) for r in c.execute('SELECT * FROM company_analytics')};companies={}
    for tid,t in all_trades.items():
        if tid in remove:continue
        ticker=t['symbol'];r=research.get(tid,{})
        g=companies.setdefault(ticker,{'ticker':ticker,'company_name':t.get('company_name') or metadata.get(ticker,{}).get('company_name',ticker),'sector':metadata.get(ticker,{}).get('sector','Unknown'),'times_analyzed':0,'trade_count':0,'trades_accepted':0,'confidence':[],'last_analyzed_at':t['created_at'],'last_signal':t.get('side'),'last_confidence':None})
        g['times_analyzed']+=1;g['trade_count']+=int(bool(t.get('broker_order_id') or t.get('broker_position_id')));g['trades_accepted']+=int(r.get('decision')=='accepted')
        if isinstance(r.get('confidence'),(int,float)):g['confidence'].append(r['confidence'])
        if (stamp(r.get('date') or t.get('created_at')) or datetime.min.replace(tzinfo=timezone.utc))>=(stamp(g['last_analyzed_at']) or datetime.min.replace(tzinfo=timezone.utc)):
            g.update(last_analyzed_at=r.get('date') or t['created_at'],last_signal=r.get('signal') or t.get('side'),last_confidence=r.get('confidence'))
    summary={'cutoff_utc':cutoff,'anchor_trade_id':anchor['id'],'remove_trade_ids':sorted(remove),'active_paper_trades':removed_live,'archived_candidates':len(removed_archive),'auxiliary_removed':{k:len(v) for k,v in auxiliary.items()},'trade_events_removed':len(dependencies.get('trade_events',[])),'shadows_preserved':c.execute('SELECT count(*) FROM shadow_exits').fetchone()[0],'broker_linked_lifecycles_preserved':sum(bool(t.get('broker_order_id') or t.get('broker_position_id')) for t in all_trades.values()),'companies_after':len(companies)}
    return {'summary':summary,'live_ids':removed_live,'archive_ids':removed_archive,'auxiliary':auxiliary,'dependencies':dependencies,'companies':companies}

def delete_ids(c,table,column,ids):
    for start in range(0,len(ids),400):
        batch=ids[start:start+400]
        c.execute('DELETE FROM '+table+' WHERE '+column+' IN ('+','.join('?' for _ in batch)+')',batch)

def apply_plan(c,p,archive_key):
    before={t:fingerprint(c,t) for t in ('shadow_exits','broker_closed_positions','broker_actions','position_management','users','profile_memberships','agent_rules')}
    for table,ids in p['dependencies'].items():delete_ids(c,table,'rowid',ids)
    delete_ids(c,'analysis_restores','trade_id',p['summary']['remove_trade_ids'])
    delete_ids(c,'archived_analysis','trade_id',p['archive_ids']);delete_ids(c,'trades','id',p['live_ids'])
    for table,ids in p['auxiliary'].items():delete_ids(c,table,'id',ids)
    keep=list(p['companies'])
    if keep:c.execute('DELETE FROM company_analytics WHERE ticker NOT IN ('+','.join('?' for _ in keep)+')',keep)
    else:c.execute('DELETE FROM company_analytics')
    for row in p['companies'].values():
        values=[row[k] for k in ('ticker','company_name','sector','times_analyzed','last_analyzed_at','last_signal','last_confidence')]
        values.extend([sum(row['confidence'])/len(row['confidence']) if row['confidence'] else 0,row['trade_count'],row['trades_accepted'],row['trades_accepted']/row['times_analyzed'],datetime.now(timezone.utc).isoformat()])
        columns='ticker,company_name,sector,times_analyzed,last_analyzed_at,last_signal,last_confidence,avg_confidence,trade_count,trades_accepted,acceptance_rate,last_updated_at'
        c.execute('INSERT INTO company_analytics ('+columns+') VALUES ('+','.join('?' for _ in values)+') ON CONFLICT(ticker) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in columns.split(',') if k!='ticker'),values)
    marker={**p['summary'],'removed_trade_ids':p['summary']['remove_trade_ids'],'archive_key':archive_key,'applied_at':datetime.now(timezone.utc).isoformat()}
    c.execute('INSERT INTO app_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(MARKER,json.dumps(marker)))
    c.execute("INSERT INTO maintenance_runs(action,details) VALUES ('legacy_history_cleanup',?)",(json.dumps(marker),))
    after={t:fingerprint(c,t) for t in before}
    if before!=after:raise ValueError('Protected data changed: transaction will be rolled back.')
    if list(c.execute('PRAGMA foreign_key_check')):raise ValueError('Foreign-key check failed: transaction will be rolled back.')
    return {'summary':p['summary'],'protected_verified':after}

@contextlib.contextmanager
def maintenance_leases(path,wait_seconds=0):
    owner='history-cleanup-'+uuid.uuid4().hex;c=connect(path)
    try:
        deadline=time.monotonic()+wait_seconds;waiting=False
        while True:
            c.execute('BEGIN IMMEDIATE');now=time.time()
            if not c.execute('SELECT count(*) FROM workflow_leases WHERE expires_at>?',(now,)).fetchone()[0]:break
            c.rollback()
            if time.monotonic()>=deadline:raise RuntimeError('An account/research operation is active; retry after it completes.')
            if not waiting:print(json.dumps({'status':'waiting_for_account_operation'}),flush=True);waiting=True
            time.sleep(2)
        c.execute('DELETE FROM workflow_leases WHERE expires_at<=?',(now,))
        c.executemany('INSERT INTO workflow_leases VALUES (?,?,?)',[(name,owner,now+900) for name in LEASES]);c.commit();yield
    finally:
        c.rollback();c.execute('DELETE FROM workflow_leases WHERE owner=?',(owner,));c.commit();c.close()

@contextlib.contextmanager
def writer_transaction(c):
    c.execute('BEGIN IMMEDIATE')
    try:yield
    except BaseException:
        c.rollback();raise
    finally:
        if c.in_transaction:c.rollback()


@contextlib.contextmanager
def pause_workers():
    """Called only with broker/research leases held and the SQLite writer lock held."""
    pids=[]
    try:
        for p in Path('/proc').iterdir():
            if not p.name.isdigit() or int(p.name)==os.getpid():continue
            try:args=(p/'cmdline').read_bytes().split(b'\0')
            except OSError:continue
            if any(a in (b'makemoney.py',b'/app/makemoney.py') for a in args) or (b'streamlit' in args and b'streamlit_app.py' in args):
                os.kill(int(p.name),signal.SIGSTOP);pids.append(int(p.name))
        yield pids
    finally:
        for pid in pids:
            try:os.kill(pid,signal.SIGCONT)
            except ProcessLookupError:pass


def archive_records(c,p):
    targets=[('trades','id',p['live_ids']),('archived_analysis','trade_id',p['archive_ids']),('analysis_restores','trade_id',p['summary']['remove_trade_ids'])]
    targets += [(table,'rowid',ids) for table,ids in p['dependencies'].items()]
    targets += [(table,'id',ids) for table,ids in p['auxiliary'].items()]
    result={}
    for table,column,ids in targets:
        columns=[r[1] for r in c.execute('PRAGMA table_info('+table+')')];records=[]
        for start in range(0,len(ids),400):
            batch=ids[start:start+400]
            for row in c.execute('SELECT * FROM '+table+' WHERE '+column+' IN ('+','.join('?' for _ in batch)+') ORDER BY '+column,batch):
                records.append({k:({'sqlite_blob_base64':base64.b64encode(row[k]).decode()} if isinstance(row[k],bytes) else row[k]) for k in columns})
        result[table]={'columns':columns,'rows':records}
    return result


def selection_digest(records):
    return hashlib.sha256(json.dumps(records,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def dated_archive(settings,c,p):
    # Archive exactly the removed cohort, not another full database copy.
    # The normal verified latest/previous recovery backups remain separate.
    from s3_backup import S3SQLiteBackup
    b=S3SQLiteBackup(settings);records=archive_records(c,p);digest=selection_digest(records)
    cache=[dict(r) for r in c.execute('SELECT * FROM company_analytics ORDER BY id')]
    document={'schema_version':1,'kind':'pre_etoro_history_archive','created_at':datetime.now(timezone.utc).isoformat(),'summary':p['summary'],'tables':records,'legacy_company_cache_before':cache,'selection_sha256':digest,'recovery_backup_key':settings.latest_key}
    raw=json.dumps(document,sort_keys=True,separators=(',',':')).encode();data=gzip.compress(raw,compresslevel=6,mtime=0);checksum=hashlib.sha256(data).hexdigest()
    key='finbot/archives/pre-etoro-cleanup/'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]+'/legacy-history.json.gz'
    b.s3.put_object(Bucket=settings.bucket,Key=key,Body=data,ContentType='application/gzip',ServerSideEncryption='AES256',Metadata={'sha256':checksum,'selection-sha256':digest,'schema-version':'1'})
    response=b.s3.get_object(Bucket=settings.bucket,Key=key);body=response['Body']
    try:downloaded=body.read(len(data)+1)
    finally:body.close()
    if hashlib.sha256(downloaded).hexdigest()!=checksum:raise ValueError('Dated history archive checksum mismatch.')
    verified=json.loads(gzip.decompress(downloaded))
    if verified['selection_sha256']!=digest or selection_digest(verified['tables'])!=digest:raise ValueError('Dated archive cohort verification failed.')
    return key,digest,len(data)


def main():
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('--db',default='/data/finbot.db');a.add_argument('--apply',action='store_true');a.add_argument('--compact',action='store_true');a.add_argument('--archive-key');args=a.parse_args()
    c=connect(args.db)
    try:
        p=plan(c);print(json.dumps({'preview':p['summary']}),flush=True)
        if not args.apply:return
        if not p['summary']['remove_trade_ids']:print(json.dumps({'status':'already_clean'}));return
        from s3_backup import BackupSettings
        settings=BackupSettings.from_environment()
        if Path(settings.database_path).resolve()!=Path(args.db).resolve():raise ValueError('Backup and cleanup paths must match.')
        if args.archive_key:
            from s3_backup import S3SQLiteBackup
            if not args.archive_key.startswith('finbot/archives/pre-etoro-cleanup/') or not args.archive_key.endswith('/legacy-history.json.gz'):raise ValueError('Invalid cohort archive key.')
            response=S3SQLiteBackup(settings).s3.get_object(Bucket=settings.bucket,Key=args.archive_key);body=response['Body']
            try:data=body.read(32*1024*1024)
            finally:body.close()
            if hashlib.sha256(data).hexdigest()!=response['Metadata'].get('sha256'):raise ValueError('Archive checksum mismatch.')
            document=json.loads(gzip.decompress(data));archive_digest=selection_digest(document['tables'])
            if archive_digest!=document['selection_sha256'] or archive_digest!=selection_digest(archive_records(c,p)):raise ValueError('Archive no longer matches selected history.')
            archive=args.archive_key;archive_bytes=len(data)
        else:archive,archive_digest,archive_bytes=dated_archive(settings,c,p)
        print(json.dumps({'dated_archive':archive,'verified':True,'compressed_bytes':archive_bytes}),flush=True)
        # Do not let an older in-flight report publish after the cleanup report.
        lock=Path(args.db).resolve().parent/'reporting-export.lock'
        with lock.open('a') as handle:
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
            with maintenance_leases(args.db,wait_seconds=600),writer_transaction(c):
                with pause_workers() as paused:
                    try:
                        p=plan(c)
                        if selection_digest(archive_records(c,p))!=archive_digest:raise ValueError('Selected data changed after archival; nothing removed. Retry with a fresh archive.')
                        result=apply_plan(c,p,archive);c.commit()
                        if args.compact:
                            before=os.path.getsize(args.db);deadline=time.monotonic()+180
                            c.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
                            try:c.execute('VACUUM')
                            finally:c.set_progress_handler(None,0)
                            result['database_bytes_before']=before
                        c.execute('PRAGMA wal_checkpoint(PASSIVE)')
                        result['paused_worker_count']=len(paused)
                    except BaseException:c.rollback();raise
                c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                result['database_bytes_after']=os.path.getsize(args.db)
        print(json.dumps({'status':'cleaned',**result}),flush=True)
    finally:c.close()

if __name__=='__main__':main()
