"""Dashboard read regressions; isolated databases and mocked providers."""
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import makemoney as app
import test_two_direction_workflow as _fixtures


class DashboardPerformanceTests(unittest.TestCase):
    setUp = _fixtures.DatabaseTests.setUp
    trade = _fixtures.DatabaseTests.trade
    update = _fixtures.DatabaseTests.update

    def test_history_batches_queries_and_preserves_evidence(self):
        for i in range(40):
            tid=self.trade()
            app.record_trade_event(tid,'research','researcher','completed','accepted','Evidence',{'large_document':'x'*20000})
        statements=[]
        factory=app.get_connection
        def traced():
            c=factory();c.set_trace_callback(statements.append);return c
        with patch.object(app,'get_connection',side_effect=traced):
            rows=app.get_trade_history()
        reads=[s for s in statements if s.lstrip().upper().startswith('SELECT')]
        self.assertEqual(len(reads),2)
        self.assertEqual(len(rows),40)
        self.assertEqual(rows[0]['events'][-1]['details']['large_document'],'x'*20000)

    def test_compact_history_does_not_transfer_large_evidence(self):
        tid=self.trade()
        app.record_trade_event(tid,'risk','risk_manager','completed','accepted','Decision',{'large_document':'x'*200000,'execution_reasons':['Capacity reached']})
        compact=app.get_trade_history(include_details=False)
        full=app.get_trade_history()
        self.assertLess(len(json.dumps(compact)),len(json.dumps(full))/10)
        self.assertEqual(compact[0]['events'][-1]['details']['execution_reasons'],['Capacity reached'])
        self.assertFalse(compact[0]['evidence_loaded'])
        self.assertNotIn('large_document',compact[0]['events'][-1]['details'])

    def test_sql_pagination_loads_only_selected_lifecycles(self):
        ids=[self.trade() for _ in range(12)]
        rows=app.get_trade_history(trade_ids=ids,limit=3,offset=3)
        self.assertEqual([r['id'] for r in rows],list(reversed(ids))[3:6])
        self.assertTrue(all(len(r['events'])==2 for r in rows))
        self.assertEqual(rows[0]['duplicate_of_trade_id'],rows[0]['id']-1)
        self.assertEqual(app.get_trade_history(trade_ids=[]),[])

    def test_historical_cutoff_applies_to_lazy_evidence(self):
        tid=self.trade()
        c=app.get_connection()
        c.execute("UPDATE trades SET created_at='2026-01-01 00:00:00' WHERE id=?",(tid,))
        c.execute("UPDATE trade_events SET created_at='2026-01-02 00:00:00' WHERE trade_id=?",(tid,));c.commit();c.close()
        rows=app.get_trade_history(end_at='2026-01-01 23:59:59',trade_ids=[tid],limit=1)
        self.assertEqual(rows[0]['events'],[])

    def test_indexes_and_wal_are_initialized(self):
        c=app.get_connection()
        self.assertEqual(c.execute('PRAGMA journal_mode').fetchone()[0],'wal')
        indexes={r['name'] for r in c.execute('PRAGMA index_list(trade_events)')}
        self.assertIn('idx_trade_events_trade_id_id',indexes)
        c.close()

    def test_dashboard_is_lazy_and_uses_no_automatic_broker_reads(self):
        from streamlit.testing.v1 import AppTest
        tid=self.trade(status='OPEN')
        self.update(tid,created_at=(app.datetime.now(app.timezone.utc)-app.timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S'))
        app.record_trade_event(tid,'risk','risk_manager','completed','accepted','Sizing evidence',{'large_document':'marker-not-rendered-until-open'})
        ui=AppTest.from_file(str(Path(app.__file__).with_name('streamlit_app.py')),default_timeout=20)
        ui.session_state['authenticated']=True;ui.session_state['username']='admin'
        with patch('integrations.EtoroBrokerAdapter',return_value=self.adapter),patch.object(app,'get_trade_history',wraps=app.get_trade_history) as history:
            ui.run()
            self.assertFalse(ui.exception,[e.message for e in ui.exception])
            self.assertFalse(any(c.kwargs.get('include_details',True) for c in history.call_args_list))
            self.adapter.get_positions.assert_not_called()
            ui.session_state[f'lifecycle_panel_{tid}']=True
            ui.run()
            self.assertFalse(ui.exception,[e.message for e in ui.exception])
            self.assertTrue(any(c.kwargs.get('include_details',True) for c in history.call_args_list))
            self.adapter.get_positions.assert_not_called()
