"""Read-only reporting contract v1, separate from recovery backups and trading.

Exports curated JSON to an offline file or a dedicated private Tigris prefix.
No broker calls, account mutations, credentials or user-password synchronization.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from uuid import uuid4
import zlib
from agent_rule_metadata import rule_metadata

SCHEMA_VERSION=1
MAX_REPORT_BYTES=64*1024*1024
TARGET_REPORT_BYTES=60*1024*1024
KINDS=('trades','events','agent_events','performance_reviews','account_history','shadow_exits','recovery_actions','configuration')
SECRET_KEYS={'password','passwordhash','apikey','userkey','secretaccesskey','accesskeyid','sessiontoken','authorization','credentialsref','privatekey','awsaccesskeyid','awssecretaccesskey','accesstoken','refreshtoken'}
OMIT={'raw','response','broker_api_logs','api_logs','rate_limits','headers','prompt','messages'}


def clean(value,depth=0,field=None):
    if depth>14:return {'reporting_truncated':True,'reason':'depth limit'}
    if isinstance(value,dict):
        result={}
        for key,item in value.items():
            normalized=re.sub('[^a-z0-9]','',str(key).lower())
            if normalized in SECRET_KEYS or normalized.endswith(('password','secret','token')) or key in OMIT:continue
            result[str(key)]=clean(item,depth+1,str(key))
        return result
    if isinstance(value,list):
        # Preserve selected structure/provenance, cap verbose provider arrays.
        recent=field in {'bars','daily_bars','weekly_bars','monthly_bars','observations','path','price_history','history','candles','ohlcv','recent_bars'}
        chosen=value[-24:] if recent else value[:24]
        return [clean(item,depth+1) for item in chosen]+([{'reporting_truncated':True,'original_items':len(value),'kept':'latest 24' if recent else 'first 24'}] if len(value)>24 else [])
    if isinstance(value,float) and not math.isfinite(value):return None
    if isinstance(value,str):return value[:8000]+(' [reporting text truncated]' if len(value)>8000 else '')
    return value


def timestamp(value):
    if not value:return None
    try:
        result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return (result if result.tzinfo else result.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    except (ValueError,TypeError):return None


def parsed(value,default=None):
    if isinstance(value,(dict,list)):return value
    try:return json.loads(value or '{}')
    except (TypeError,ValueError):return {} if default is None else default


def compact_evidence(value, budget=12000):
    """Bound duplicated context, retaining decisions/levels and visible provenance."""
    cleaned=clean(value)
    encoded=json.dumps(cleaned,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    if len(encoded)<=budget:return cleaned
    def shrink(item,depth,limit,field=None):
        if isinstance(item,dict):
            if depth>=limit:
                result={k:(v[:160] if isinstance(v,str) else v) for k,v in item.items() if v is None or isinstance(v,(str,int,float,bool))}
                result['reporting_truncated']=True;result['reason']='Nested context summarized; full evidence retained in Finbot'
                return result
            return {k:shrink(v,depth+1,limit,k) for k,v in item.items()}
        if isinstance(item,list):
            recent=field in {'bars','daily_bars','weekly_bars','monthly_bars','observations','path','price_history','history','candles','ohlcv','recent_bars','resistance','support'}
            chosen=item[-3:] if recent else item[:3]
            result=[shrink(v,depth+1,limit,field) for v in chosen]
            if len(item)>3:result.append({'reporting_truncated':True,'original_items':len(item),'kept':'latest 3' if recent else 'first 3'})
            return result
        if isinstance(item,str) and len(item)>500:return item[:500]+' [reporting context summarized]'
        return item
    for depth in (7,5,3,2):
        result=shrink(cleaned,0,depth)
        if isinstance(result,dict):
            result={**result,'reporting_evidence_summary':{'original_clean_bytes':len(encoded),'full_evidence':'Finbot primary database'}}
        if len(json.dumps(result,separators=(',',':'),ensure_ascii=False).encode())<=budget:return result
    # A pathological broad object must not stop every other trade from syncing.
    scalars={k:(v[:160] if isinstance(v,str) else v) for k,v in (cleaned.items() if isinstance(cleaned,dict) else []) if v is None or isinstance(v,(str,int,float,bool))}
    result={'reporting_truncated':True,'original_clean_bytes':len(encoded),'sha256':hashlib.sha256(encoded).hexdigest(),
            'summary':{},'full_evidence':'Finbot primary database'}
    for key,value in list(scalars.items())[:24]:
        candidate={**result,'summary':{**result['summary'],key:value}}
        if len(json.dumps(candidate,separators=(',',':'),ensure_ascii=False).encode())<=budget:result=candidate
    return result


def build_snapshot(database, *, source_id='finbot/shared-demo', profile_id=1, now=None,
                   periods=None, include_archived=True):
    from reporting_partitions import full_clean
    if profile_id!=1:raise ValueError('Finbot currently has one active reporting runtime; export only profile 1.')
    now=now or datetime.now(timezone.utc)
    c=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True,timeout=30);c.row_factory=sqlite3.Row
    periods=set(periods or ()) or None
    allowed=lambda value: periods is None or str(value or '')[:10] in periods
    try:
        c.execute('BEGIN')
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'trades' not in tables:raise ValueError('Not a Finbot database.')
        def rows(table,fields=None):
            if table not in tables:return []
            if fields:
                existing={r[1] for r in c.execute('PRAGMA table_info('+table+')')}
                selected=[f for f in fields if f in existing]
                if not selected:return []
                columns=','.join(selected)
            else:columns='*'
            return [dict(r) for r in c.execute('SELECT '+columns+' FROM '+table)]
        from history_retention import MARKER,retain_trade,execution_evidence
        history_row=c.execute('SELECT value FROM app_settings WHERE key=?',(MARKER,)).fetchone() if 'app_settings' in tables else None
        history=parsed(history_row[0]) if history_row else {}
        cutoff=history.get('cutoff_utc')
        protected={int(r['trade_id']) for table in ('shadow_exits','broker_actions','position_management') for r in rows(table,('trade_id',))}
        if cutoff and 'trade_events' in tables:
            protected.update(int(r[0]) for r in c.execute("SELECT DISTINCT trade_id FROM trade_events WHERE stage='execution' AND decision IN ('submitted','executed','uncertain')"))
            for row in c.execute("SELECT trade_id,stage,decision,details FROM trade_events WHERE stage='execution' AND julianday(created_at)<julianday(?)",(cutoff,)):
                if execution_evidence([dict(row)]):protected.add(int(row['trade_id']))
        all_trades={int(t['id']):t for t in rows('trades') if int(t.get('trading_profile_id') or 1)==profile_id and retain_trade(t,cutoff,protected)}
        trades={tid:t for tid,t in all_trades.items() if allowed(t.get('created_at'))}
        events=[]
        if include_archived and 'archived_analysis' in tables:
            for row in c.execute('SELECT trade_id,payload FROM archived_analysis'):
                inflater=zlib.decompressobj();data=inflater.decompress(row['payload'],32*1024*1024)
                if not inflater.eof:raise ValueError('Archived analysis exceeds reporting safety limit.')
                archive=json.loads(data);t=archive['trade']
                archive_events=[e for e in archive['events'] if allowed(e.get('created_at'))]
                if int(t.get('trading_profile_id') or 1)==profile_id and retain_trade(t,cutoff,protected,archive['events']) and (allowed(t.get('created_at')) or archive_events):
                    trades[int(t['id'])]={**t,'reporting_archived':True};events.extend(archive_events)
        ids=set(all_trades)
        if 'trade_events' in tables:
            query='SELECT * FROM trade_events'
            parameters=[]
            if periods:
                query+=' WHERE substr(created_at,1,10) IN ('+','.join('?' for _ in periods)+')'
                parameters=sorted(periods)
            events.extend(dict(e) for e in c.execute(query,parameters) if int(e['trade_id']) in ids)
            event_trade_ids={int(e['trade_id']) for e in events}
            for tid in event_trade_ids:
                if tid in all_trades:trades.setdefault(tid,all_trades[tid])
        ids=set(trades)
        closed={str(r['position_id']).removesuffix('.0'):r for r in rows('broker_closed_positions')}
        output={kind:[] for kind in KINDS};canonical_ids={}
        for t in sorted(trades.values(),key=lambda x:(x.get('status')=='CLOSED',int(x['id']))):
            if t.get('broker_position_id'):canonical_ids[str(t['broker_position_id']).removesuffix('.0')]=int(t['id'])
        for t in sorted(trades.values(),key=lambda x:int(x['id'])):
            if not allowed(t.get('created_at')):continue
            policy=parsed(t.get('policy_json'));observation=policy.get('broker_observation') or {}
            pid=str(t.get('broker_position_id') or '').removesuffix('.0');ledger=closed.get(pid)
            canonical=bool(pid and canonical_ids.get(pid)==int(t['id']))
            fields=('id','symbol','company_name','side','status','leverage','allocated_capital','entry_price','quantity','stop_loss_price','take_profit_price','broker_order_id','broker_position_id','strategy_version','reconciliation_status','stop_state','reporting_archived')
            item={k:t.get(k) for k in fields}
            item.update(created_at=timestamp(t.get('created_at')),opened_at=timestamp(t.get('opened_at')),closed_at=timestamp(ledger.get('closed_at') if ledger else t.get('closed_at')),
                        realized_pnl=ledger.get('net_profit') if ledger and canonical and t['status']=='CLOSED' else None,
                        broker_fees=ledger.get('fees') if ledger else t.get('broker_fees'),running_pnl=observation.get('reported_running_pnl'),
                        current_price=observation.get('current_price'),quote_at=timestamp(observation.get('quote_observed_at')),policy=policy,
                        entry_config=parsed(t.get('config_json')),profile_id=str(profile_id),canonical_position=canonical)
            item['submitted']=bool(pid or t.get('broker_order_id'));output['trades'].append(full_clean(item))
        stages={}
        for e in sorted(events,key=lambda x:int(x['id'])):
            tid=int(e['trade_id']);stage=e['stage'];key=(tid,stage);occurrence=stages.get(key,0);stages[key]=occurrence+1
            phase=('initial' if occurrence==0 else 'recheck') if stage in {'research','strategy','risk','execution'} else 'post_execution'
            # The primary trade event is the canonical full evidence record.
            # Reporting carries a bounded, attributable view so one busy day
            # cannot expand hundreds of MiB of JSON into multiple GiB of RAM.
            details=compact_evidence(parsed(e.pop('details',None)))
            if stage=='execution' and e.get('decision')=='submitted':phase='submission'
            output['events'].append(full_clean({**{k:e.get(k) for k in ('id','trade_id','stage','agent_name','status','decision','message')},'created_at':timestamp(e.get('created_at')),'phase':phase,'symbol':trades[tid]['symbol'],'details':details}))
        for row in rows('agent_events',('id','agent_name','status','action','message','decision','created_at')):
            if not allowed(row.get('created_at')):continue
            output['agent_events'].append(full_clean({**{k:row.get(k) for k in ('id','agent_name','status','action','message','decision')},'created_at':timestamp(row.get('created_at'))}))
        for row in rows('performance_reviews'):
            if not allowed(row.get('created_at')):continue
            output['performance_reviews'].append(full_clean({**row,'created_at':timestamp(row.get('created_at')),'evidence':parsed(row.get('evidence'))}))
        for row in rows('broker_snapshots',('id','equity','cash','benchmark','created_at')):
            if not allowed(row.get('created_at')):continue
            # Raw account/portfolio responses are deliberately excluded.
            output['account_history'].append(full_clean({**{k:row.get(k) for k in ('id','equity','cash','benchmark')},'created_at':timestamp(row.get('created_at'))}))
        for row in rows('shadow_exits'):
            if periods and not allowed(row.get('created_at') or row.get('observed_at')):continue
            if int(row['trade_id']) in ids:
                payload=parsed(row.get('payload'));output['shadow_exits'].append(full_clean({**row,'id':f"{row['trade_id']}:{row['variant']}:{row['horizon_hours']}",'payload':payload}))
        for row in rows('broker_actions',('id','trade_id','position_id','kind','state','attempts','checks','failures','mismatches','last_error','created_at','updated_at','next_check_at','alert_at','last_attempt_at','payload')):
            if periods and datetime.fromtimestamp(float(row.get('created_at') or 0),timezone.utc).date().isoformat() not in periods:continue
            if int(row['trade_id']) in ids:
                output['recovery_actions'].append(full_clean({**{k:row.get(k) for k in ('id','trade_id','position_id','kind','state','attempts','checks','failures','mismatches','last_error')},**{k:timestamp(datetime.fromtimestamp(float(row[k]),timezone.utc).isoformat()) if row.get(k) is not None else None for k in ('created_at','updated_at','next_check_at','alert_at','last_attempt_at')},'payload':parsed(row.get('payload'))}))
        settings={r['key']:r['value'] for r in rows('app_settings')}
        # Read only agent rules, never the integration/runtime environment or users.
        rules={r['agent_name']:parsed(r['rules_json']) for r in rows('agent_rules')}
        if not rules:rules=parsed(settings.get('agent_rules'))
        if not rules:
            for key,value in settings.items():
                if key in {'agent_rules_json','agent_configurations'}:rules=parsed(value)
        output['configuration']=[{'id':'current','created_at':timestamp(now.isoformat()),'rules':full_clean(rules),'rule_metadata':rule_metadata(rules),'methodology':{'purpose':'Reporting mirror; trading controls remain in Finbot.','price_trailing':('Frozen entry ATR for retreats. Selectable arming: amount uses broker-reported unrealized USD profit; ATR uses favorable movement from entry. Amount is the default for updated configurations; snapshots without an arming selector use ATR. Prime and major thresholds are configurable alternatives, not combined gates. Retreat distances always use frozen entry ATR from the best executable bid (LONG) or ask (SHORT). No extra leverage multiplier applies. The profit_trailing_mode setting selects scheduled exits or native broker trailing; scheduled observations continue in both modes. A tighter native stop is retained through major upgrades. SL/TP remain active. Switching arming basis starts fresh observations; existing broker trailing stops are retained.' if 'profit_trailing_prime_arm_atr' in rules.get('position_manager',{}) else 'Legacy dollar-profit tracking in this imported snapshot; awaiting updated ATR configuration.'),'time_management':'Elapsed time schedules reassessment, never forced closure.','profit_formula':'Direction-adjusted price change × actual units; leverage is already reflected in position sizing.','account_scope':'Broker-confirmed closed positions supply realized net P/L; shadow outcomes are counterfactual.','history_starts_at':cutoff}}]
        profile=next((r for r in rows('trading_profiles') if int(r['id'])==profile_id),{})
        account=next((r for r in rows('broker_accounts') if r['id']==profile.get('broker_account_id')),{})
        connection=next((r for r in rows('broker_connections') if r['id']==account.get('connection_id')),{})
        output.update(schema_version=SCHEMA_VERSION,generation_id=now.strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:12],generated_at=timestamp(now.isoformat()),source_id=source_id,
                      profile={'id':str(profile_id),'name':profile.get('name','Finbot shared demo'),'provider':connection.get('provider','etoro'),'environment':connection.get('environment','demo')},
                      counts={kind:len(output[kind]) for kind in KINDS})
        return output
    finally:c.rollback();c.close()


def encode_snapshot(snapshot):
    raw=json.dumps(snapshot,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    if len(raw)>TARGET_REPORT_BYTES:
        snapshot={**snapshot,'events':[dict(event) for event in snapshot.get('events',[])]}
        for budget in (6000,3000,1000):
            for event in snapshot['events']:
                event['details']=compact_evidence(event.get('details',{}),budget)
            raw=json.dumps(snapshot,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
            if len(raw)<=TARGET_REPORT_BYTES:break
    if len(raw)>MAX_REPORT_BYTES:raise ValueError('Reporting headers exceed 64 MB; split the reporting contract before upload.')
    return gzip.compress(raw,compresslevel=6,mtime=0)


def publish_snapshot(snapshot,client,bucket,prefix,encoded=None):
    prefix=prefix.strip('/')
    if not prefix.startswith('finbot/reporting/'):raise ValueError('Reporting exports must use a dedicated finbot/reporting/ prefix, outside recovery backups.')
    data=encoded if encoded is not None else encode_snapshot(snapshot);key=f"{prefix}/generations/{snapshot['generation_id']}.json.gz"
    checksum=hashlib.sha256(data).hexdigest()
    client.put_object(Bucket=bucket,Key=key,Body=data,ContentType='application/gzip',Metadata={'sha256':checksum,'schema-version':'1'},ServerSideEncryption='AES256')
    # An immutable object is published before the small pointer; readers never see a partial generation.
    manifest={'schema_version':1,'generation_id':snapshot['generation_id'],'generated_at':snapshot['generated_at'],'object_key':key,'sha256':checksum,'compressed_bytes':len(data),'counts':snapshot['counts']}
    previous=None
    try:
        response=client.get_object(Bucket=bucket,Key=prefix+'/latest.json');body=response['Body']
        try:previous=json.loads(body.read(65536))
        finally:body.close()
    except Exception as error:
        if str(getattr(error,'response',{}).get('Error',{}).get('Code','')) not in {'NoSuchKey','404','NotFound'}:raise
    if previous:client.put_object(Bucket=bucket,Key=prefix+'/previous.json',Body=json.dumps(previous).encode(),ContentType='application/json',ServerSideEncryption='AES256')
    client.put_object(Bucket=bucket,Key=prefix+'/latest.json',Body=json.dumps(manifest).encode(),ContentType='application/json',ServerSideEncryption='AES256')
    keep={key}
    if previous and str(previous.get('object_key','')).startswith(prefix+'/generations/'):keep.add(previous['object_key'])
    inventory=list(client.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix=prefix+'/generations/'))
    for page in inventory:
        remove=[{'Key':o['Key']} for o in page.get('Contents',[]) if o['Key'] not in keep and re.fullmatch(re.escape(prefix)+r'/generations/[A-Za-z0-9-]+\.json\.gz',o['Key'])]
        if remove:
            result=client.delete_objects(Bucket=bucket,Delete={'Objects':remove,'Quiet':True})
            if result.get('Errors'):raise RuntimeError('Reporting generation retention cleanup failed.')
    return manifest


def _publish_and_notify(settings=None, *, client=None, now=None, prepared_snapshot=None, encoded=None):
    """Publish a curated report and notify the consumer; never expose backup credentials."""
    import hmac
    import time
    from urllib.request import Request,build_opener,HTTPRedirectHandler
    from urllib.parse import urlsplit
    from s3_backup import BackupSettings,S3SQLiteBackup,configuration_environment
    env=configuration_environment()
    if str(env.get('FINBOT_REPORTING_ENABLED','false')).lower() not in {'true','1','yes','on'}:
        return {'status':'disabled'}
    endpoint=env.get('FINBOT_REPORTING_NOTIFY_URL','')
    parsed_url=urlsplit(endpoint)
    if parsed_url.scheme!='https' or parsed_url.username or parsed_url.password or parsed_url.fragment:
        raise ValueError('Reporting notification requires an HTTPS URL without embedded credentials.')
    secret=env.get('FINBOT_REPORTING_SHARED_SECRET','')
    if len(secret)<32:raise ValueError('Reporting notification secret is missing or too short.')
    settings=settings or BackupSettings.from_environment()
    client=client or S3SQLiteBackup(settings).s3
    prefix=env.get('FINBOT_REPORTING_S3_PREFIX','finbot/reporting/shared-demo').strip('/')
    import fcntl
    lock_path=Path(settings.database_path).resolve().parent/'reporting-export.lock'
    with lock_path.open('a') as lock:
        os.chmod(lock_path,0o600)
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'busy'}
        previous_manifest=None
        dirty_pairs=None
        if prepared_snapshot is not None:
            snapshot=prepared_snapshot
        else:
            try:
                pointer=client.get_object(Bucket=settings.bucket,Key=prefix+'/latest.json')
                try:latest=json.loads(pointer['Body'].read(1024*1024))
                finally:pointer['Body'].close()
                if latest.get('schema_version')==2 and latest.get('object_key'):
                    response=client.get_object(Bucket=settings.bucket,Key=latest['object_key'])
                    try:previous_manifest=json.loads(gzip.decompress(response['Body'].read(2*1024*1024)))
                    finally:response['Body'].close()
            except (KeyError,TypeError,ValueError):
                previous_manifest=None
            except Exception as error:
                if str((getattr(error,'response',None) or {}).get('Error',{}).get('Code','')) not in {'404','NoSuchKey','NotFound'}:raise
            if previous_manifest:
                since=timestamp(previous_manifest.get('generated_at'))
                since_sql=since.replace('T',' ').removesuffix('Z')
                current=(now or datetime.now(timezone.utc)).date().isoformat()
                periods={current}
                c=sqlite3.connect(Path(settings.database_path).resolve().as_uri()+'?mode=ro',uri=True,timeout=30)
                try:
                    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    dated=(('trade_events','created_at'),('agent_events','created_at'),('performance_reviews','created_at'),('broker_snapshots','created_at'))
                    for table,column in dated:
                        if table in tables:
                            periods.update(str(r[0])[:10] for r in c.execute(f'SELECT DISTINCT substr({column},1,10) FROM {table} WHERE {column}>=?',(since_sql,)) if r[0])
                    if 'trades' in tables:
                        periods.update(str(r[0])[:10] for r in c.execute("SELECT DISTINCT substr(created_at,1,10) FROM trades WHERE created_at>=? OR closed_at>=? OR status IN ('OPEN','EXIT_PENDING','SUBMITTED_PENDING_RECONCILIATION','RECONCILIATION_REQUIRED','SUBMITTING')",(since_sql,since_sql)) if r[0])
                finally:c.close()
                # Historical archive evidence is already represented by the
                # unchanged daily parts in the prior manifest. Re-inflating every
                # archive merely to discard dates outside ``periods`` recreated a
                # full-history memory spike. Live rows are authoritative for all
                # dirty periods; archived historical parts remain referenced.
                snapshot=build_snapshot(settings.database_path,now=now,periods=periods,include_archived=False)
                dirty_pairs={(kind,period) for kind in snapshot.keys() if kind in KINDS for period in periods}
                dirty_pairs.add(('configuration',snapshot['generated_at'][:10]))
                # Configuration has one current row; replace every older configuration partition.
                dirty_pairs.update(('configuration',part.get('period')) for part in previous_manifest.get('parts',[]) if part.get('kind')=='configuration')
            else:
                snapshot=build_snapshot(settings.database_path,now=now,include_archived=False)
        def notify(manifest):
            url=client.generate_presigned_url('get_object',Params={'Bucket':settings.bucket,'Key':manifest['object_key']},ExpiresIn=600,HttpMethod='GET')
            notice={'schema_version':manifest.get('schema_version',1),'manifest':manifest,'download_url':url,'profile_id':snapshot['profile']['id'],'source_id':snapshot['source_id']}
            body=json.dumps(notice,separators=(',',':')).encode();stamp=str(int(time.time()))
            signature=hmac.new(secret.encode(),stamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
            class NoRedirect(HTTPRedirectHandler):
                def redirect_request(self,*args,**kwargs):raise RuntimeError('Reporting notifications cannot follow redirects.')
            from urllib.error import HTTPError,URLError
            for attempt in range(3):
                request=Request(endpoint,data=body,headers={'Content-Type':'application/json','Accept':'application/json','User-Agent':'FinbotReporting/2.0','X-Finbot-Timestamp':stamp,'X-Finbot-Signature':signature},method='POST')
                try:
                    with build_opener(NoRedirect()).open(request,timeout=90) as response:return json.loads(response.read(65537))
                except (HTTPError,URLError,TimeoutError) as error:
                    if attempt==2 or (isinstance(error,HTTPError) and error.code not in (409,429,502,503,504)):raise
                    time.sleep(2*(attempt+1))
        if snapshot.get('schema_version')==1:
            from reporting_partitions import publish_partitioned
            return publish_partitioned(snapshot,client,settings.bucket,prefix,notify,
                                       previous_manifest=previous_manifest,rebuild_periods=dirty_pairs)
        manifest=publish_snapshot(snapshot,client,settings.bucket,prefix,encoded=encoded)
        result=notify(manifest)
        if result.get('status') not in {'imported','unchanged'}:raise RuntimeError('Dashboard did not confirm the reporting import.')
        return {'status':result['status'],'generation_id':manifest['generation_id'],'compressed_bytes':manifest['compressed_bytes'],'counts':snapshot['counts']}



def publish_and_notify(settings=None, *, client=None, now=None, prepared_snapshot=None, encoded=None):
    from s3_backup import BackupSettings,configuration_environment
    if str(configuration_environment().get('FINBOT_REPORTING_ENABLED','false')).lower() not in {'true','1','yes','on'}:
        return {'status':'disabled'}
    settings=settings or BackupSettings.from_environment()
    path=Path(settings.database_path).resolve().parent/'reporting_status.json'
    def status(**fields):
        try:
            old=json.loads(path.read_text()) if path.exists() else {}
            temporary=path.with_name('.reporting-status-'+uuid4().hex)
            temporary.write_text(json.dumps({**old,**fields},sort_keys=True));temporary.chmod(0o600);os.replace(temporary,path)
        except (OSError,ValueError,TypeError):pass  # Observability must not break an otherwise valid import.
    status(last_attempt_at=timestamp(datetime.now(timezone.utc).isoformat()),state='publishing')
    try:
        result=_publish_and_notify(settings,client=client,now=now,prepared_snapshot=prepared_snapshot,encoded=encoded)
        if result.get('status') in {'imported','unchanged'}:
            status(state='healthy',last_success_at=timestamp(datetime.now(timezone.utc).isoformat()),last_error=None,generation_id=result['generation_id'],compressed_bytes=result.get('compressed_bytes'))
        else:status(state=result.get('status','unknown'))
        return result
    except Exception as error:
        code=getattr(error,'code',None)
        status(state='failed',last_error=type(error).__name__+(f':{code}' if isinstance(code,int) else ''),last_failure_at=timestamp(datetime.now(timezone.utc).isoformat()))
        raise


def scheduled_publish(settings):
    """Called by the existing backup daemon; reporting failure never fails a DB backup."""
    import logging
    try:
        result=publish_and_notify(settings)
        if result['status'] not in {'disabled','busy'}:logging.getLogger('finbot.reporting').info('Reporting sync %s: generation=%s bytes=%s',result['status'],result['generation_id'],result['compressed_bytes'])
    except Exception as error:
        logging.getLogger('finbot.reporting').warning('Reporting sync failed (%s); prior reporting data retained.',type(error).__name__)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--db',required=True);parser.add_argument('--output');parser.add_argument('--publish',action='store_true');parser.add_argument('--notify',action='store_true');parser.add_argument('--input-report');parser.add_argument('--prefix',default='finbot/reporting/shared-demo');args=parser.parse_args()
    if args.notify:
        from s3_backup import BackupSettings
        prepared=None;encoded=None
        if args.input_report:
            import io
            path=Path(args.input_report)
            if path.stat().st_size>64*1024*1024:raise ValueError('Reporting object exceeds 64 MB.')
            encoded=path.read_bytes()
            with gzip.GzipFile(fileobj=io.BytesIO(encoded),mode='rb') as handle:raw=handle.read(64*1024*1024+1)
            if len(raw)>MAX_REPORT_BYTES:raise ValueError('Reporting payload exceeds 64 MB.')
            prepared=json.loads(raw)
        print(json.dumps(publish_and_notify(BackupSettings.from_environment(),prepared_snapshot=prepared,encoded=encoded)));return
    if not args.output and not args.publish:parser.error('Choose --output or --publish.')
    snapshot=build_snapshot(args.db);data=encode_snapshot(snapshot)
    if args.output:
        path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as tmp:
            tmp.write(data);os.chmod(tmp.name,0o600);temporary=tmp.name
        os.replace(temporary,path)
    if args.publish:
        from s3_backup import BackupSettings,S3SQLiteBackup
        settings=BackupSettings.from_environment();result=publish_snapshot(snapshot,S3SQLiteBackup(settings).s3,settings.bucket,args.prefix)
    else:result={'generation_id':snapshot['generation_id'],'compressed_bytes':len(data),'counts':snapshot['counts']}
    print(json.dumps(result))
if __name__=='__main__':main()
