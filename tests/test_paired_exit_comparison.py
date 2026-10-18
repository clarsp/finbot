import unittest
import pandas as pd
from paired_exit_comparison import daily_before_entry,replay,economics,ExitPolicy,entry_levels

class PairedReplayTests(unittest.TestCase):
    def trade(self,**kw):
        t=dict(side='LONG',entered_at='2026-10-12T14:00Z',entry=100,atr14=2,units=4,leverage=2,
               cost_model=dict(roundtrip_bps=0,financing_annual_pct=0,known_opening_fees=0))
        return dict(t,**kw)
    def bars(self,values):
        return pd.DataFrame(values,columns=['Open','High','Low','Close'],
                    index=pd.date_range('2026-10-12T14:00Z',periods=len(values),freq='5min'))
    def levels(self):return dict(status='eligible',stop=96,target=106)
    def test_same_bar_stop_first(self):
        r=replay(self.trade(),self.bars([[100,107,95,103]]),self.levels(),24,'2026-10-12T14:10Z')
        self.assertEqual(r['exit'],'ambiguous_stop_first');self.assertEqual(r['exit_price'],96)
    def test_adverse_gap_not_optimistic_stop_fill(self):
        r=replay(self.trade(),self.bars([[94,95,93,94]]),self.levels(),24,'2026-10-12T14:10Z')
        self.assertEqual(r['exit_price'],94)
        r=replay(self.trade(side='SHORT'),self.bars([[106,107,105,106]]),dict(status='eligible',stop=104,target=94),24,'2026-10-12T14:10Z')
        self.assertEqual(r['exit_price'],106)
    def test_partial_entry_bar_and_incomplete_candle_not_used(self):
        r=replay(self.trade(entered_at='2026-10-12T14:02Z'),self.bars([[100,120,90,100],[100,101,99,100]]),self.levels(),24,'2026-10-12T14:09Z')
        self.assertEqual(r['status'],'unavailable')
    def test_unfinished_horizon_not_falsely_closed_or_counted_as_win(self):
        r=replay(self.trade(),self.bars([[100,101,99,100]]),self.levels(),168,'2026-10-12T14:10Z')
        self.assertEqual(r['status'],'censored');self.assertNotIn('net_pnl_actual_units',economics(self.trade(),r,ExitPolicy('x',3,1,False)))
    def test_time_boundary_bar_ambiguity_is_flagged(self):
        r=replay(self.trade(),self.bars([[100,107,99,101],[101,102,100,101]]),self.levels(),.05,'2026-10-12T14:15Z')
        self.assertEqual(r['exit'],'time_exit');self.assertEqual(r['exit_price'],101)
        self.assertTrue(r['deadline_bar_ambiguity'])
    def test_daily_levels_exclude_entry_day_and_future(self):
        b=pd.DataFrame({'Open':[100.]*22,'High':[102.]*21+[999.], 'Low':[98.]*22,'Close':[100.]*22},index=pd.date_range('2026-09-21',periods=22,tz='America/New_York'))
        before=daily_before_entry(b,'2026-10-12T14:00Z')
        self.assertEqual(len(before),21);self.assertEqual(before['High'].max(),102)
    def test_wider_stop_keeps_equal_risk_budget(self):
        t=self.trade();r=dict(status='complete',gross_per_unit=2,hours_held=1)
        e=economics(t,r,ExitPolicy('x',3,1,False))
        self.assertAlmostEqual(e['equal_risk_units'],4*2/3)
        self.assertAlmostEqual(e['net_pnl_actual_units'],8)
        self.assertAlmostEqual(e['net_pnl_equal_risk'],16/3)
    def test_structure_at_entry_blocks_instead_of_inventing_profitable_target(self):
        b=pd.DataFrame({'Open':[100.]*25,'High':[100.1]*25,'Low':[99.]*25,'Close':[100.]*25,'atr14':[2.]*25},index=pd.date_range('2026-09-01',periods=25,tz='America/New_York'))
        self.assertEqual(entry_levels(self.trade(),b,ExitPolicy('x',3,1,True))['status'],'blocked')

class SnapshotExtractionTests(unittest.TestCase):
    def test_legacy_entry_uses_broker_snapshot_timestamp_not_created_at(self):
        import json
        from pathlib import Path
        import sqlite3
        import tempfile
        from scripts.compare_trade_exits import extract
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'snapshot.db';c=sqlite3.connect(path)
            c.executescript('''CREATE TABLE trades(id INTEGER,symbol TEXT,side TEXT,broker_fill_price REAL,
                broker_fill_units REAL,broker_order_id TEXT,broker_position_id TEXT,opened_at TEXT,
                policy_json TEXT,config_json TEXT,leverage REAL,strategy_version TEXT);
                CREATE TABLE trade_events(trade_id INTEGER,id INTEGER,stage TEXT,decision TEXT,details TEXT);
                CREATE TABLE broker_snapshots(id INTEGER,payload TEXT);''')
            c.execute('INSERT INTO trades VALUES (1,?,?,?,?,?,?,?,?,?,?,?)',('MA','LONG',100,4,'o','p',None,'{}','{}',2,None))
            payload={'positions':{'positions':[{'position_id':'p','opened_at':'2026-10-12T14:00Z','raw':{}}]}}
            c.execute('INSERT INTO broker_snapshots VALUES (1,?)',(json.dumps(payload),));c.commit();c.close()
            trades,excluded=extract(path)
            self.assertFalse(excluded)
            self.assertEqual(trades[0]['entered_at'],'2026-10-12T14:00Z')
            self.assertEqual(trades[0]['entry_timestamp_source'],'stored broker portfolio snapshot')
            self.assertIsNone(trades[0]['atr14'])
            c=sqlite3.connect(path);self.assertIsNone(c.execute('SELECT opened_at FROM trades').fetchone()[0]);c.close()
