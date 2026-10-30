"""Lossless, daily reporting parts; dashboard switches generations only on commit."""
import gzip
import hashlib
import json
import math
import re
from functools import lru_cache
from collections import defaultdict

PART_BYTES=4*1024*1024
MAX_ROW_BYTES=32*1024*1024

def pack(value):
    return json.dumps(value,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()

@lru_cache(maxsize=4096)
def excluded(key):
    from reporting_export import SECRET_KEYS,OMIT
    normalized=re.sub('[^a-z0-9]','',key.lower())
    return normalized in SECRET_KEYS or normalized.endswith(('password','secret','token')) or key in OMIT

def full_clean(value,depth=0):
    if depth>100:raise ValueError('Reporting evidence nesting exceeds supported depth; evidence was not truncated.')
    if isinstance(value,dict):return {str(k):full_clean(v,depth+1) for k,v in value.items() if not excluded(str(k))}
    if isinstance(value,list):return [full_clean(v,depth+1) for v in value]
    if isinstance(value,float) and not math.isfinite(value):return None
    return value


def partitions(snapshot):
    from reporting_export import KINDS
    groups=defaultdict(list)
    for kind in KINDS:
        for row in snapshot[kind]:groups[kind,(row.get('created_at') or 'undated')[:10]].append(row)
    for (kind,period),rows in sorted(groups.items()):
        batch=[];size=0
        for row in sorted(rows,key=lambda r:str(r['id'])):
            length=len(pack(row))
            if length>MAX_ROW_BYTES:raise ValueError('One reporting row exceeds 32 MiB; no evidence was removed.')
            if batch and size+length>PART_BYTES:
                yield kind,period,batch;batch=[];size=0
            batch.append(row);size+=length+1
        if batch:yield kind,period,batch

def publish_partitioned(snapshot,client,bucket,prefix,notify):
    from reporting_export import KINDS
    if not prefix.startswith('finbot/reporting/') or '..' in prefix:raise ValueError('Reporting partitions require a dedicated reporting prefix.')
    descriptors=[];total=0
    for kind,period,rows in partitions(snapshot):
        raw=pack({'schema_version':2,'kind':kind,'period':period,'rows':rows})
        data=gzip.compress(raw,compresslevel=3,mtime=0);digest=hashlib.sha256(data).hexdigest()
        key=prefix+'/parts/'+digest+'.json.gz'
        try:
            head=client.head_object(Bucket=bucket,Key=key)
            if head.get('Metadata',{}).get('sha256')!=digest:
                client.put_object(Bucket=bucket,Key=key,Body=data,ContentType='application/gzip',Metadata={'sha256':digest},ServerSideEncryption='AES256')
        except Exception as error:
            if str(getattr(error,'response',{}).get('Error',{}).get('Code','')) not in {'404','NoSuchKey','NotFound'}:raise
            client.put_object(Bucket=bucket,Key=key,Body=data,ContentType='application/gzip',Metadata={'sha256':digest},ServerSideEncryption='AES256')
        descriptors.append({'object_key':key,'sha256':digest,'kind':kind,'period':period,'rows':len(rows),'raw_bytes':len(raw),'compressed_bytes':len(data)})
        total+=len(data)
    manifest={k:snapshot[k] for k in ('generation_id','generated_at','source_id','profile','counts')}
    manifest.update(schema_version=2,parts=descriptors,compressed_bytes=total)
    raw=pack(manifest);data=gzip.compress(raw,compresslevel=3,mtime=0)
    if len(raw)>1024*1024:raise ValueError('Reporting manifest exceeds 1 MiB.')
    key=prefix+'/generations/'+snapshot['generation_id']+'.json.gz'
    client.put_object(Bucket=bucket,Key=key,Body=data,ContentType='application/gzip',ServerSideEncryption='AES256')
    base={'schema_version':2,'generation_id':snapshot['generation_id'],'generated_at':snapshot['generated_at']}
    for part in descriptors:
        result=notify({**base,**part,'action':'part'})
        if result.get('status') not in ('staged','unchanged'):raise RuntimeError('Dashboard did not stage reporting partition.')
    commit={**base,'object_key':key,'sha256':hashlib.sha256(data).hexdigest(),'action':'commit'}
    result=notify(commit)
    if result.get('status') not in ('imported','unchanged'):raise RuntimeError('Dashboard did not commit reporting partitions.')
    # Publish the recovery pointer only after the consumer confirmed all parts.
    try:
        response=client.get_object(Bucket=bucket,Key=prefix+'/latest.json');previous=response['Body'].read();response['Body'].close()
        client.put_object(Bucket=bucket,Key=prefix+'/previous.json',Body=previous,ContentType='application/json',ServerSideEncryption='AES256')
    except Exception as error:
        if str(getattr(error,'response',{}).get('Error',{}).get('Code','')) not in {'404','NoSuchKey','NotFound'}:raise
    client.put_object(Bucket=bucket,Key=prefix+'/latest.json',Body=pack(commit),ContentType='application/json',ServerSideEncryption='AES256')
    # Keep only the current and preceding complete exports, including shared parts.
    try:
        keep={key}|{p['object_key'] for p in descriptors}
        if 'previous' in locals():
            old=json.loads(previous);keep.add(old['object_key'])
            if old.get('schema_version')==2:
                response=client.get_object(Bucket=bucket,Key=old['object_key'])
                try:older=json.loads(gzip.decompress(response['Body'].read()))
                finally:response['Body'].close()
                keep.update(p['object_key'] for p in older['parts'])
        import re
        for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix=prefix+'/'):
            remove=[{'Key':o['Key']} for o in page.get('Contents',[]) if o['Key'] not in keep and re.fullmatch(re.escape(prefix)+r'/(parts/[a-f0-9]{64}|generations/[A-Za-z0-9-]+)\.json\.gz',o['Key'])]
            if remove:
                cleanup_result=client.delete_objects(Bucket=bucket,Delete={'Objects':remove,'Quiet':True})
                if cleanup_result.get('Errors'):raise RuntimeError('Partition retention cleanup failed')
    except Exception as error:
        import logging
        logging.getLogger('finbot.reporting').warning('Partition export committed; retention cleanup failed (%s).',type(error).__name__)

    return {'status':result['status'],'generation_id':snapshot['generation_id'],'compressed_bytes':total,'counts':snapshot['counts'],'partitions':len(descriptors)}
