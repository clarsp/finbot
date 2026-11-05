import gzip,io,json,unittest
from unittest.mock import Mock,patch
import reporting_partitions as p
from reporting_export import KINDS

class PartsTests(unittest.TestCase):
 def snapshot(self):
  s={k:[] for k in KINDS};s.update(schema_version=1,generation_id='g1',generated_at='2026-10-22T12:00:00Z',source_id='finbot/shared-demo',profile={'id':'1'})
  s['events']=[{'id':i,'trade_id':1,'created_at':'2026-10-22T12:00:00Z','details':{'bars':list(range(100)),'text':'evidence'*2000,'api_key':'private'}} for i in range(5)]
  s['events'].append({'id':99,'created_at':'2026-10-21T12:00:00Z','details':{'reason':'older evidence'}})
  s['counts']={k:len(s[k]) for k in KINDS};return s
 def test_lossless_redaction_and_daily_size_partitioning(self):
  s=p.full_clean(self.snapshot());self.assertNotIn('private',json.dumps(s));self.assertEqual(len(s['events'][0]['details']['bars']),100);self.assertEqual(len(s['events'][0]['details']['text']),16000)
  with patch.object(p,'PART_BYTES',20000):parts=list(p.partitions(s))
  self.assertEqual(len(parts),6);self.assertEqual({period for _,period,_ in parts},{'2026-10-21','2026-10-22'})
  rows=[r for _,_,rs in parts for r in rs];self.assertEqual(sorted(r['id'] for r in rows),[0,1,2,3,4,99])
 def test_commit_and_pointer_follow_all_parts(self):
  s=p.full_clean(self.snapshot());client=Mock();client.get_object.return_value={'Body':io.BytesIO(b'{"object_key":"old"}')};client.get_paginator.return_value.paginate.return_value=[{'Contents':[{'Key':'finbot/reporting/shared-demo/parts/'+'f'*64+'.json.gz'}]}];client.delete_objects.return_value={}
  calls=[]
  def notify(m):
   calls.append(m['action']);return {'status':'staged' if m['action']=='part' else 'imported'}
  result=p.publish_partitioned(s,client,'datadb','finbot/reporting/shared-demo',notify)
  self.assertEqual(calls,['part','part','commit']);self.assertEqual(result['partitions'],2)
  keys=[x.kwargs['Key'] for x in client.put_object.call_args_list];self.assertTrue(keys[-1].endswith('/latest.json'))
 def test_failed_part_never_commits_or_changes_pointer(self):
  client=Mock();s=p.full_clean(self.snapshot())
  with self.assertRaises(RuntimeError):p.publish_partitioned(s,client,'datadb','finbot/reporting/shared-demo',lambda m:{'status':'failed'})
  self.assertFalse(any(c.kwargs['Key'].endswith('/latest.json') for c in client.put_object.call_args_list))
 def test_oversized_single_row_is_never_summarized(self):
  with patch.object(p,'MAX_ROW_BYTES',10),self.assertRaises(ValueError):list(p.partitions(self.snapshot()))
 def test_incremental_rebuild_reuses_untouched_daily_parts(self):
  s=p.full_clean(self.snapshot());s['events']=[row for row in s['events'] if row['created_at'].startswith('2026-10-22')]
  prior={'parts':[{'object_key':'old-21','sha256':'a','kind':'events','period':'2026-10-21','rows':4,'raw_bytes':10,'compressed_bytes':8},
                  {'object_key':'old-22','sha256':'b','kind':'events','period':'2026-10-22','rows':9,'raw_bytes':10,'compressed_bytes':8}]}
  client=Mock();client.get_object.side_effect=[{'Body':io.BytesIO(json.dumps({'schema_version':2,'object_key':'prior-manifest'}).encode())},
                                               {'Body':io.BytesIO(gzip.compress(json.dumps(prior).encode()))}]
  client.get_paginator.return_value.paginate.return_value=[]
  calls=[]
  result=p.publish_partitioned(s,client,'datadb','finbot/reporting/shared-demo',lambda m:(calls.append(m),{'status':'staged' if m['action']=='part' else 'imported'})[1],previous_manifest=prior,rebuild_periods={('events','2026-10-22')})
  manifest=gzip.decompress(next(call.kwargs['Body'] for call in client.put_object.call_args_list if '/generations/' in call.kwargs['Key']))
  parts=json.loads(manifest)['parts']
  self.assertTrue(any(part['object_key']=='old-21' for part in parts));self.assertFalse(any(part['object_key']=='old-22' for part in parts))
  self.assertEqual(result['counts']['events'],9)
 def test_database_snapshot_summarizes_long_evidence_before_partitioning(self):
  import sqlite3,tempfile
  from pathlib import Path
  from reporting_export import build_snapshot
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'evidence.db';c=sqlite3.connect(path)
   c.executescript('CREATE TABLE trades(id INTEGER,symbol TEXT,status TEXT); CREATE TABLE trade_events(id INTEGER,trade_id INTEGER,stage TEXT,agent_name TEXT,status TEXT,decision TEXT,message TEXT,created_at TEXT,details TEXT);')
   c.execute("INSERT INTO trades VALUES (1,'TEST','OPEN')")
   evidence={'bars':list(range(180)),'reason':'decision evidence '*2000,'api_key':'private'}
   c.execute('INSERT INTO trade_events VALUES (1,1,?,?,?,?,?,?,?)',('risk','risk_manager','accepted','accepted','Evidence','2026-10-22T12:00:00Z',json.dumps(evidence)));c.commit();c.close()
   report=build_snapshot(path);details=report['events'][0]['details']
   self.assertLess(len(json.dumps(details)),12000);self.assertLess(len(details['bars']),len(evidence['bars']));self.assertLess(len(details['reason']),len(evidence['reason']));self.assertNotIn('api_key',details)
