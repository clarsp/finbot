import gzip
import io
import json
import sqlite3
import zlib
from datetime import datetime, timezone
from unittest.mock import Mock

import tempfile
import unittest
from pathlib import Path
import reporting_export as r


class ReportingExportTests(unittest.TestCase):
    def test_readonly_archive_config_and_profit_dedup(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);tmp_path=Path(temporary.name)
        path=tmp_path/'test.db'
        c=sqlite3.connect(path)
        c.executescript('''CREATE TABLE trades(id INTEGER PRIMARY KEY,symbol TEXT,status TEXT,broker_position_id TEXT,created_at TEXT,policy_json TEXT,config_json TEXT);
        CREATE TABLE trade_events(id INTEGER,trade_id INTEGER,stage TEXT,agent_name TEXT,decision TEXT,created_at TEXT,details TEXT);
        CREATE TABLE broker_closed_positions(position_id TEXT,net_profit REAL,closed_at TEXT,fees REAL);
        CREATE TABLE archived_analysis(trade_id INTEGER,payload BLOB);
        CREATE TABLE agent_rules(agent_name TEXT,rules_json TEXT);
        CREATE TABLE broker_actions(id INTEGER,trade_id INTEGER,created_at REAL,updated_at REAL,next_check_at REAL,payload TEXT);''')
        c.execute('INSERT INTO trades VALUES (1,?,?,?,?,?,?)',('MA','CLOSED','p1','2026-10-19 10:00:00',json.dumps({'api_key':'DO_NOT_EXPORT_CREDENTIAL_123','atr14':12}),json.dumps({'execution':{'max_order_units':5,'password':'DO_NOT_EXPORT_CREDENTIAL_123'}})))
        c.execute('INSERT INTO trades VALUES (2,?,?,?,?,?,?)',('MA','CLOSED','p1','2026-10-19 11:00:00','{}','{}'))
        c.execute('INSERT INTO broker_closed_positions VALUES (?,?,?,?)',('p1',42,'2026-10-20 10:00:00',0))
        c.execute('INSERT INTO trade_events VALUES (1,1,?,?,?,?,?)',('risk','risk_manager','accepted','2026-10-19 10:00:00','{}'))
        c.execute('INSERT INTO trade_events VALUES (2,1,?,?,?,?,?)',('risk','risk_manager','accepted','2026-10-19 11:00:00','{}'))
        c.execute('INSERT INTO archived_analysis VALUES (?,?)',(3,zlib.compress(json.dumps({'trade':{'id':3,'symbol':'MSFT','status':'REJECTED_STRATEGY'},'events':[]}).encode())))
        c.execute('INSERT INTO broker_actions VALUES (1,1,0,60,120,?)',('{}',))
        c.execute('INSERT INTO agent_rules VALUES (?,?)',('position_manager',json.dumps({'profit_trailing_prime_arm_atr':.5,'profit_trailing_major_arm_atr':1,'profit_trailing_prime_retreat_atr':.15,'profit_trailing_major_retreat_atr':.25})))
        c.execute('INSERT INTO agent_rules VALUES (?,?)',('risk_manager','{"stop_atr_multiple":2}'));c.commit();c.close()
        before=path.read_bytes();s=r.build_snapshot(path,now=datetime(2026,10,20,tzinfo=timezone.utc))
        assert path.read_bytes()==before
        assert s['recovery_actions'][0]['next_check_at']=='1970-01-01T00:02:00Z'
        assert len(s['trades'])==3 and s['trades'][2]['reporting_archived']
        assert sum(t['realized_pnl'] or 0 for t in s['trades'])==42
        assert [e['phase'] for e in s['events']]==['initial','recheck']
        assert s['configuration'][0]['rules']['risk_manager']['stop_atr_multiple']==2
        assert s['configuration'][0]['rule_metadata']['position_manager']['profit_trailing_prime_arm_atr']['unit']=='ATR'
        assert 'strictly greater' in s['configuration'][0]['rule_metadata']['position_manager']['profit_trailing_prime_arm_atr']['help']
        assert 'Frozen entry ATR' in s['configuration'][0]['methodology']['price_trailing']
        assert 'DO_NOT_EXPORT_CREDENTIAL_123' not in json.dumps(s)
        assert json.loads(gzip.decompress(r.encode_snapshot(s)))==s
        with self.assertRaises(ValueError):r.build_snapshot(path,profile_id=2)


    def test_clean_finite_and_credentials(self):
        assert r.clean({'x':float('nan'),'smtp_password':'secret','access_key_id':'id','nested':{'Authorization':'secret','ok':3}})=={'x':None,'nested':{'ok':3}}
        assert r.clean(list(range(121)))[-1]['reporting_truncated']


    def test_publish_atomic_pointer_and_reporting_only_retention(self):
        client=Mock();prefix='finbot/reporting/shared-demo';old=prefix+'/generations/old.json.gz'
        client.get_object.return_value={'Body':io.BytesIO(json.dumps({'object_key':old}).encode())}
        client.get_paginator.return_value.paginate.return_value=[{'Contents':[{'Key':old},{'Key':prefix+'/generations/obsolete.json.gz'},{'Key':prefix+'/generations/unknown.txt'}]}]
        client.delete_objects.return_value={}
        s={'generation_id':'new','generated_at':'2026-10-20T00:00:00Z','counts':{}}
        r.publish_snapshot(s,client,'datadb',prefix)
        keys=[call.kwargs['Key'] for call in client.put_object.call_args_list]
        assert keys==[prefix+'/generations/new.json.gz',prefix+'/previous.json',prefix+'/latest.json']
        assert client.delete_objects.call_args.kwargs['Delete']['Objects']==[{'Key':prefix+'/generations/obsolete.json.gz'}]
        with self.assertRaises(ValueError):r.publish_snapshot(s,client,'datadb','finbot/shared-demo')
        client.reset_mock();client.put_object.side_effect=RuntimeError('upload failed')
        with self.assertRaises(RuntimeError):r.publish_snapshot(s,client,'datadb',prefix)
        client.delete_objects.assert_not_called()

if __name__=="__main__":unittest.main()

class ReportingNotificationTests(unittest.TestCase):
    def test_notification_binds_timestamp_and_body(self):
        import hmac
        from types import SimpleNamespace
        from unittest.mock import patch,MagicMock
        import hashlib
        env={'FINBOT_REPORTING_ENABLED':'true','FINBOT_REPORTING_NOTIFY_URL':'https://reports.example/api/reporting-sync.php','FINBOT_REPORTING_SHARED_SECRET':'x'*64}
        snapshot={'profile':{'id':'1'},'source_id':'finbot/shared-demo','counts':{'trades':3}}
        manifest={'generation_id':'g1','object_key':'finbot/reporting/shared-demo/generations/g1.json.gz','compressed_bytes':123}
        client=Mock();client.generate_presigned_url.return_value='https://datadb.t3.storage.dev/report?temporary=1'
        response=MagicMock();response.__enter__.return_value.read.return_value=b'{"status":"imported"}'
        opener=Mock();opener.open.return_value=response
        with patch('s3_backup.configuration_environment',return_value=env),patch.object(r,'build_snapshot',return_value=snapshot),patch.object(r,'publish_snapshot',return_value=manifest),patch('urllib.request.build_opener',return_value=opener):
            temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
            result=r.publish_and_notify(SimpleNamespace(database_path=str(Path(temporary.name)/'test.db'),bucket='datadb'),client=client)
        request=opener.open.call_args.args[0];headers=dict(request.header_items());stamp=headers['X-finbot-timestamp']
        self.assertEqual(headers['X-finbot-signature'],hmac.new(b'x'*64,stamp.encode()+b'.'+request.data,hashlib.sha256).hexdigest())
        self.assertEqual(json.loads(request.data)['profile_id'],'1')
        self.assertEqual(client.generate_presigned_url.call_args.kwargs['ExpiresIn'],600)
        self.assertNotIn('temporary',json.dumps(result))

    def test_disabled_reporting_does_not_export(self):
        from unittest.mock import patch
        with patch('s3_backup.configuration_environment',return_value={}),patch.object(r,'build_snapshot') as build:
            self.assertEqual(r.publish_and_notify(),{'status':'disabled'});build.assert_not_called()

    def test_reporting_failure_does_not_fail_backup_or_log_credentials(self):
        from unittest.mock import patch
        with patch.object(r,'publish_and_notify',side_effect=RuntimeError('sensitive URL and token')),self.assertLogs('finbot.reporting',level='WARNING') as logs:
            r.scheduled_publish(None)
        self.assertIn('RuntimeError',logs.output[0]);self.assertNotIn('sensitive',logs.output[0])

class ReportingPayloadBoundsTests(unittest.TestCase):
    def test_price_series_keep_recent_observations_with_visible_limit(self):
        result=r.clean({'bars':list(range(80)),'reason':'x'*9000})
        self.assertEqual(result['bars'][:24],list(range(56,80)))
        self.assertEqual(result['bars'][-1]['original_items'],80)
        self.assertIn('reporting text truncated',result['reason'])

    def test_raw_operational_payloads_are_not_read(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fixture.db';c=sqlite3.connect(path)
            c.executescript("CREATE TABLE trades(id INTEGER,symbol TEXT,status TEXT);CREATE TABLE agent_events(id INTEGER,agent_name TEXT,created_at TEXT,details TEXT);CREATE TABLE broker_snapshots(id INTEGER,equity REAL,cash REAL,benchmark REAL,created_at TEXT,payload TEXT);")
            c.execute("INSERT INTO trades VALUES (1,'MA','OPEN')")
            c.execute("INSERT INTO agent_events VALUES (1,'position_manager','2026-10-20T12:00:00Z','private raw payload')")
            c.execute("INSERT INTO broker_snapshots VALUES (1,100,50,10,'2026-10-20T12:00:00Z','private raw payload')");c.commit();c.close()
            original=sqlite3.connect
            def restricted(*args,**kwargs):
                connection=original(*args,**kwargs)
                connection.set_authorizer(lambda action,table,column,*extra:sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and (table,column) in {('agent_events','details'),('broker_snapshots','payload')} else sqlite3.SQLITE_OK)
                return connection
            with patch.object(r.sqlite3,'connect',side_effect=restricted):snapshot=r.build_snapshot(path)
            self.assertEqual(len(snapshot['agent_events']),1);self.assertEqual(snapshot['account_history'][0]['equity'],100)
            self.assertNotIn('private raw payload',json.dumps(snapshot))

class ReportingBackupLifecycleTests(unittest.TestCase):
    def test_slow_reporting_does_not_delay_backups_or_final_snapshot(self):
        from unittest.mock import patch,Mock
        from types import SimpleNamespace
        import s3_backup
        backup=Mock();backup.settings=SimpleNamespace(interval_seconds=3600,database_path=Path('/tmp/test.db'))
        stop=Mock();stop.wait.side_effect=[False,False,True]
        process=Mock();process.poll.return_value=None
        with patch.object(s3_backup,'configuration_environment',return_value={'FINBOT_REPORTING_ENABLED':'true'}),patch.object(s3_backup.signal,'signal'),patch.object(s3_backup.threading,'Event',return_value=stop),patch.object(s3_backup.subprocess,'Popen',return_value=process) as factory:
            s3_backup.run_daemon(backup)
        self.assertEqual(backup.create_snapshot.call_count,3)
        factory.assert_called_once()

    def test_disabled_reporting_does_not_start_export_thread(self):
        from unittest.mock import patch,Mock
        from types import SimpleNamespace
        import s3_backup
        backup=Mock();backup.settings=SimpleNamespace(interval_seconds=3600);stop=Mock();stop.wait.side_effect=[False,True]
        with patch.object(s3_backup,'configuration_environment',return_value={}),patch.object(s3_backup.signal,'signal'),patch.object(s3_backup.threading,'Event',return_value=stop),patch.object(s3_backup.subprocess,'Popen') as factory:
            s3_backup.run_daemon(backup)
        self.assertEqual(backup.create_snapshot.call_count,2);factory.assert_not_called()

class EvidenceSizeRegressionTests(unittest.TestCase):
    def test_large_context_keeps_decisions_prices_and_all_lifecycle_headers(self):
        detail={'decision':'accepted','entry':100,'stop':90,'target':120,'reasons':['ATR and structure passed'],
                'context':{'bars':[{'close':i,'description':'x'*1000} for i in range(80)],'company':{'summary':'y'*9000}}}
        compact=r.compact_evidence(detail,3000)
        self.assertLessEqual(len(json.dumps(compact,separators=(',',':')).encode()),3000)
        for key in ('decision','entry','stop','target','reasons'):self.assertEqual(compact[key],detail[key])
        self.assertIn('reporting_evidence_summary',compact)
        snapshot={'events':[{'id':i,'trade_id':1,'stage':'risk','created_at':'2026-10-22T12:00:00Z','details':detail} for i in range(3)]}
        original=json.dumps(snapshot);encoded=r.encode_snapshot(snapshot)
        self.assertEqual(json.dumps(snapshot),original)
        self.assertEqual(len(json.loads(gzip.decompress(encoded))['events']),3)

    def test_failed_export_records_status_without_provider_secrets(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            settings=SimpleNamespace(database_path=str(Path(directory)/'test.db'))
            with patch('s3_backup.configuration_environment',return_value={'FINBOT_REPORTING_ENABLED':'true'}),patch.object(r,'_publish_and_notify',side_effect=ValueError('private token secret')):
                with self.assertRaises(ValueError):r.publish_and_notify(settings)
            status=json.loads((Path(directory)/'reporting_status.json').read_text())
            self.assertEqual(status['state'],'failed');self.assertEqual(status['last_error'],'ValueError')
            self.assertNotIn('private token',json.dumps(status))

    def test_adaptive_budget_preserves_every_event_without_mutating_source(self):
        from unittest.mock import patch
        detail={'decision':'accepted','target':120,'context':{'bars':[{'close':i,'text':'x'*1000} for i in range(80)]}}
        snapshot={'events':[{'id':i,'trade_id':1,'stage':'risk','details':detail} for i in range(3)]}
        before=json.dumps(snapshot)
        with patch.object(r,'MAX_REPORT_BYTES',20000),patch.object(r,'TARGET_REPORT_BYTES',18000):encoded=r.encode_snapshot(snapshot)
        raw=gzip.decompress(encoded);self.assertLess(len(raw),20000)
        self.assertEqual([e['id'] for e in json.loads(raw)['events']],[0,1,2])
        self.assertEqual(json.dumps(snapshot),before)
