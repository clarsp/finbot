import importlib.util,json,sqlite3,tempfile,unittest,zlib
from pathlib import Path
from history_retention import retain_trade,MARKER
import reporting_export
spec=importlib.util.spec_from_file_location('cleanup',Path(__file__).resolve().parents[1]/'scripts/cleanup_legacy_history.py');cleanup=importlib.util.module_from_spec(spec);spec.loader.exec_module(cleanup)

class HistoryCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'fixture.db';self.c=cleanup.connect(self.path);self.addCleanup(self.c.close)
        self.c.executescript('''
        CREATE TABLE trades(id INTEGER PRIMARY KEY,created_at TEXT,symbol TEXT,company_name TEXT,status TEXT,broker_order_id TEXT,broker_position_id TEXT,order_reference TEXT,broker_fill_units REAL,policy_json TEXT,config_json TEXT);
        CREATE TABLE archived_analysis(trade_id INTEGER PRIMARY KEY,payload BLOB);
        CREATE TABLE trade_events(id INTEGER PRIMARY KEY,trade_id INTEGER REFERENCES trades(id),created_at TEXT,stage TEXT,agent_name TEXT,status TEXT,decision TEXT,message TEXT,details TEXT);
        CREATE TABLE broker_closed_positions(position_id TEXT PRIMARY KEY,net_profit REAL,closed_at TEXT,fees REAL,payload TEXT);
        CREATE TABLE shadow_exits(trade_id INTEGER REFERENCES trades(id),variant TEXT,horizon_hours REAL,payload TEXT);
        CREATE TABLE broker_actions(id INTEGER,trade_id INTEGER REFERENCES trades(id),payload TEXT);
        CREATE TABLE position_management(trade_id INTEGER REFERENCES trades(id));
        CREATE TABLE users(id INTEGER PRIMARY KEY,name TEXT,password_hash TEXT);
        CREATE TABLE profile_memberships(profile_id INTEGER,user_id INTEGER);
        CREATE TABLE trading_profiles(id INTEGER PRIMARY KEY);
        CREATE TABLE agent_rules(agent_name TEXT,rules_json TEXT);
        CREATE TABLE app_settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE maintenance_runs(id INTEGER PRIMARY KEY,action TEXT,details TEXT);
        CREATE TABLE signals(id INTEGER PRIMARY KEY,created_at TEXT,payload TEXT);
        CREATE TABLE agent_events(id INTEGER PRIMARY KEY,created_at TEXT,agent_name TEXT,decision TEXT,details TEXT);
        CREATE TABLE risk_events(id INTEGER PRIMARY KEY,created_at TEXT,details TEXT);
        CREATE TABLE research_cycle_companies(id INTEGER PRIMARY KEY,cycle_timestamp TEXT);
        CREATE TABLE analysis_restores(trade_id INTEGER PRIMARY KEY);
        CREATE TABLE news_articles(id INTEGER PRIMARY KEY,created_at TEXT);
        CREATE TABLE company_analytics(id INTEGER PRIMARY KEY,ticker TEXT UNIQUE,company_name TEXT,sector TEXT,times_analyzed INTEGER,last_analyzed_at TEXT,last_signal TEXT,last_confidence REAL,avg_confidence REAL,trade_count INTEGER,trades_accepted INTEGER,acceptance_rate REAL,last_updated_at TEXT);
        INSERT INTO users VALUES(1,'Admin','private-hash');INSERT INTO profile_memberships VALUES(1,1);INSERT INTO trading_profiles VALUES(1);INSERT INTO agent_rules VALUES('execution','{}');
        INSERT INTO news_articles VALUES(1,'2026-10-01');
        ''')
        def trade(i,date,status='REJECTED_STRATEGY',position=None,order=None,reference=None):
            self.c.execute('INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?)',(i,date,'ABC','Example',status,order,position,reference,0,'{}','{}'))
        trade(1,'2026-10-11 10:00:00','OPEN');trade(2,'2026-10-12 05:20:31','CLOSED','p2','o2');trade(3,'2026-10-10','REJECTED_EXECUTION');trade(4,'2026-10-09','OPEN');trade(6,'2026-10-13');trade(7,'2026-10-10',reference='uncertain-local-request');trade(8,'2026-10-10',position='other-broker-position');trade(9,'invalid-date')
        self.c.execute("INSERT INTO broker_closed_positions VALUES ('p2',42,'2026-10-14',0,'{}')")
        self.c.execute("INSERT INTO shadow_exits VALUES (4,'one-atr',336,'{\"observations\":[1,2,3]}')")
        self.c.execute("INSERT INTO broker_actions VALUES (1,2,'{}')");self.c.execute('INSERT INTO position_management VALUES (2)')
        for i,tid,decision,date in [(1,1,'accepted','2026-10-11'),(2,2,'submitted','2026-10-12'),(3,3,'uncertain','2026-10-10'),(4,6,'rejected','2026-10-13')]:
            self.c.execute('INSERT INTO trade_events VALUES (?,?,?,?,?,?,?,?,?)',(i,tid,date,'execution','execution','completed',decision,'fixture','{}'))
        self.legacy={'trade':{'id':5,'created_at':'2026-10-11','symbol':'OLD','status':'REJECTED_RESEARCH'},'events':[]}
        self.c.execute('INSERT INTO archived_analysis VALUES (?,?)',(5,zlib.compress(json.dumps(self.legacy).encode())))
        for table in ('signals','risk_events'):self.c.execute('INSERT INTO '+table+" VALUES (1,'2026-10-11','{}')")
        self.c.execute("INSERT INTO research_cycle_companies VALUES (1,'2026-10-11')")
        self.c.executemany('INSERT INTO agent_events VALUES (?,?,?,?,?)',[(1,'2026-10-11','researcher','accepted','{}'),(2,'2026-10-11','researcher','accepted','{"trade_id":2}'),(3,'2026-10-11','execution','uncertain','{}')])
        self.c.commit()
    def test_conservative_boundary(self):
        p=cleanup.plan(self.c)
        self.assertEqual(p['summary']['anchor_trade_id'],2);self.assertEqual(p['summary']['remove_trade_ids'],[1,5]);self.assertEqual(p['summary']['shadows_preserved'],1)
        self.assertEqual(p['auxiliary']['agent_events'],[1]);self.assertEqual(p['summary']['trade_events_removed'],1)
    def test_apply_preserves_broker_shadow_security_and_new_rejections(self):
        p=cleanup.plan(self.c);self.c.execute('BEGIN IMMEDIATE');result=cleanup.apply_plan(self.c,p,'finbot/archives/pre-etoro-cleanup/test/finbot.db.gz');self.c.commit()
        self.assertEqual([r[0] for r in self.c.execute('SELECT id FROM trades ORDER BY id')],[2,3,4,6,7,8,9]);self.assertEqual(result['protected_verified']['shadow_exits']['rows'],1)
        self.assertEqual(self.c.execute('SELECT net_profit FROM broker_closed_positions').fetchone()[0],42);self.assertEqual(self.c.execute('SELECT password_hash FROM users').fetchone()[0],'private-hash')
        self.assertEqual(self.c.execute('SELECT count(*) FROM news_articles').fetchone()[0],1);self.assertEqual(cleanup.plan(self.c)['summary']['remove_trade_ids'],[])
        self.assertEqual(self.c.execute('SELECT count(*) FROM archived_analysis').fetchone()[0],0)
    def test_unknown_foreign_key_blocks_instead_of_silently_deleting(self):
        self.c.execute('CREATE TABLE unexpected(trade_id INTEGER REFERENCES trades(id))');self.c.execute('INSERT INTO unexpected VALUES(1)');self.c.commit()
        with self.assertRaisesRegex(ValueError,'Protected dependency'):cleanup.plan(self.c)
        self.assertIsNotNone(self.c.execute('SELECT id FROM trades WHERE id=1').fetchone())
    def test_protected_change_rolls_back(self):
        self.c.execute("CREATE TRIGGER bad_trigger AFTER DELETE ON trades BEGIN UPDATE shadow_exits SET payload='changed'; END");self.c.commit();p=cleanup.plan(self.c);self.c.execute('BEGIN IMMEDIATE')
        with self.assertRaisesRegex(ValueError,'Protected data changed'):cleanup.apply_plan(self.c,p,'archive')
        self.c.rollback();self.assertIsNotNone(self.c.execute('SELECT id FROM trades WHERE id=1').fetchone());self.assertEqual(self.c.execute('SELECT count(*) FROM archived_analysis').fetchone()[0],1)
    def test_no_confirmed_anchor_does_not_guess_cutoff(self):
        self.c.execute('DELETE FROM broker_closed_positions')
        with self.assertRaisesRegex(ValueError,'No broker-confirmed anchor'):cleanup.plan(self.c)
    def test_export_floor_prevents_restored_paper_archives_reappearing(self):
        self.c.execute('INSERT INTO app_settings VALUES (?,?)',(MARKER,json.dumps({'cutoff_utc':'2026-10-12T05:20:31+00:00'})));self.c.commit()
        s=reporting_export.build_snapshot(self.path);ids={t['id'] for t in s['trades']}
        self.assertFalse({1,5}&ids);self.assertTrue({2,3,4,6,7,8,9}<=ids);self.assertEqual(len(s['shadow_exits']),1)
    def test_exact_archive_roundtrip_including_compressed_sqlite_blobs(self):
        from unittest.mock import Mock,patch
        from types import SimpleNamespace
        import io,gzip
        p=cleanup.plan(self.c);client=Mock();stored={}
        def put(**kw):stored.update(kw)
        client.put_object.side_effect=put;client.get_object.side_effect=lambda **kw:{'Body':io.BytesIO(stored['Body'])}
        with patch('s3_backup.S3SQLiteBackup',return_value=SimpleNamespace(s3=client)):
            key,digest,size=cleanup.dated_archive(SimpleNamespace(bucket='test',latest_key='latest'),self.c,p)
        doc=json.loads(gzip.decompress(stored['Body']));self.assertEqual(cleanup.selection_digest(doc['tables']),digest);self.assertEqual(size,len(stored['Body']))
        blob=doc['tables']['archived_analysis']['rows'][0]['payload']['sqlite_blob_base64']
        import base64
        self.assertEqual(json.loads(zlib.decompress(base64.b64decode(blob))),self.legacy)
        self.assertNotIn('users',doc['tables']);self.assertNotIn('shadow_exits',doc['tables'])
        before=cleanup.selection_digest(cleanup.archive_records(self.c,p));self.c.execute("UPDATE trades SET company_name='changed' WHERE id=1")
        self.assertNotEqual(cleanup.selection_digest(cleanup.archive_records(self.c,p)),before)
    def test_worker_resume_even_when_cleanup_raises(self):
        from unittest.mock import Mock,patch,call
        import signal
        worker=Mock();worker.name='1234';worker.__truediv__=Mock(return_value=Mock(read_bytes=Mock(return_value=b'/app/.venv/bin/python\0makemoney.py\0')))
        web=Mock();web.name='1235';web.__truediv__=Mock(return_value=Mock(read_bytes=Mock(return_value=b'/app/.venv/bin/python\0-m\0streamlit\0run\0streamlit_app.py\0')))
        with patch.object(cleanup.Path,'iterdir',return_value=[worker,web]),patch.object(cleanup.os,'kill') as kill:
            with self.assertRaisesRegex(RuntimeError,'fixture failure'):
                with cleanup.pause_workers():raise RuntimeError('fixture failure')
            self.assertEqual(kill.call_args_list,[call(1234,signal.SIGSTOP),call(1235,signal.SIGSTOP),call(1234,signal.SIGCONT),call(1235,signal.SIGCONT)])
    def test_lease_conflict_does_not_interrupt_account_operation(self):
        import time
        self.c.execute('CREATE TABLE workflow_leases(name TEXT PRIMARY KEY,owner TEXT,expires_at REAL)');self.c.execute('INSERT INTO workflow_leases VALUES (?,?,?)',('etoro_demo_account','real-operation',time.time()+300));self.c.commit()
        with self.assertRaisesRegex(RuntimeError,'operation is active'):
            with cleanup.maintenance_leases(self.path):self.fail('Busy account was not protected')
        self.assertEqual(self.c.execute('SELECT owner FROM workflow_leases').fetchone()[0],'real-operation')
    def test_ambiguous_or_missing_time_is_retained(self):
        for t in [{'id':1,'created_at':None},{'id':2,'created_at':'bad'},{'id':3,'created_at':'2026-10-01','status':'SUBMITTING'}]:self.assertTrue(retain_trade(t,'2026-10-12'))
        self.assertTrue(retain_trade({'id':8,'created_at':'2026-10-01'},'2026-10-12',events=[{'stage':'execution','decision':'accepted','details':'{"broker_order_submitted":true}'}]))

if __name__=='__main__':unittest.main()
