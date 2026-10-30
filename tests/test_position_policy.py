import unittest,sqlite3,json
from datetime import datetime,timezone,timedelta
from position_policy import profit_exit_evidence,ensure_schema,request_research,claim_research

class PolicyTests(unittest.TestCase):
    def test_both_directions_require_net_profit_with_costs_and_buffer(self):
        now=datetime(2026,10,21,16,tzinfo=timezone.utc)
        for side,rate in [('LONG',{'bid':110,'ask':110.1}),('SHORT',{'bid':89.9,'ask':90})]:
            t={'side':side,'broker_position_id':'p','opened_at':(now-timedelta(days=1)).isoformat()}
            p={'side':side,'position_id':'p','open_rate':100,'units':2,'amount':100,'unrealized_pnl':20,'fees':0}
            result=profit_exit_evidence(t,p,rate,{},now)
            self.assertTrue(result['allowed']);self.assertEqual(result['gross_profit'],20)
            for change in [{'unrealized_pnl':-.1},{'fees':25},{'units':float('nan')},{'fees':None},{'position_id':'other'}]:
                self.assertFalse(profit_exit_evidence(t,{**p,**change},rate,{},now)['allowed'])
            self.assertFalse(profit_exit_evidence(t,p,rate,{'profit_review_exits_enabled':False},now)['allowed'])
            self.assertFalse(profit_exit_evidence(t,p,rate,{'profit_exit_min_net_usd':30},now)['allowed'])
            near={'bid':100.2,'ask':100.3} if side=='LONG' else {'bid':99.7,'ask':99.8}
            self.assertFalse(profit_exit_evidence(t,{**p,'unrealized_pnl':.4},near,{},now)['allowed'])

    def test_research_requests_deduplicate_and_back_off(self):
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;ensure_schema(c)
        now=datetime.now(timezone.utc)
        self.assertEqual(request_research(c,'BE','old',now),1)
        self.assertEqual(request_research(c,'BE','old',now),0);c.commit()
        self.assertEqual(claim_research(c,4,now),['BE'])
        self.assertEqual(claim_research(c,4,now),[])
        self.assertEqual(claim_research(c,4,now+timedelta(hours=2)),['BE'])
        c.close()

class ProfitTrailingTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:')
        self.c.execute('CREATE TABLE trades(id INTEGER PRIMARY KEY)')
        self.c.execute('INSERT INTO trades VALUES (1)')
        self.t={'id':1,'side':'LONG','broker_position_id':'p','policy_json':json.dumps({'atr14':10})}
        self.p={'position_id':'p','side':'LONG','open_rate':100,'unrealized_pnl':0}
        self.rules={'profit_trailing_arming_basis':'atr'}
        self.addCleanup(self.c.close)

    def observe(self,price,now,rules=None):
        from position_policy import observe_trailing_profit
        return observe_trailing_profit(self.c,self.t,self.p,{'bid':price,'ask':price+.1},now,{**self.rules,**(rules or {})})

    def test_prime_scan_cadence_and_gap_below_trigger(self):
        self.assertEqual(self.observe(106,0)['tier'],'prime')
        self.assertIsNone(self.observe(104,1799))
        self.assertFalse(self.observe(105,1800)['close_due'])
        result=self.observe(103,3600)
        self.assertTrue(result['close_due']);self.assertEqual(result['best_price'],106)
        self.assertEqual(result['trigger_price'],104.5)

    def test_major_tier_never_downgrades_and_peak_moves(self):
        self.assertEqual(self.observe(110,0)['tier'],'prime')
        self.assertEqual(self.observe(111,1800)['tier'],'major')
        self.assertFalse(self.observe(115,3600)['close_due'])
        result=self.observe(112,5400)
        self.assertTrue(result['close_due']);self.assertEqual(result['trigger_price'],112.5)
        self.assertEqual(self.observe(105,7200)['tier'],'major')

    def test_entry_basis_is_frozen_and_old_dollar_peaks_are_not_used(self):
        self.c.execute('CREATE TABLE profit_trailing(trade_id INTEGER,peak_profit_usd REAL)')
        self.c.execute('INSERT INTO profit_trailing VALUES (1,500)')
        self.assertEqual(self.observe(99,0)['tier'],'tracking')
        self.observe(106,1800)
        self.t['policy_json']=json.dumps({'atr14':100})
        result=self.observe(104.5,3600)
        self.assertTrue(result['close_due']);self.assertEqual(result['entry_atr'],10)
        self.assertEqual(self.c.execute('SELECT peak_profit_usd FROM profit_trailing').fetchone()[0],500)
        self.p['open_rate']=101
        with self.assertRaises(ValueError):self.observe(104,5400)

    def test_price_decisions_are_size_leverage_and_pnl_independent(self):
        from position_policy import trailing_evidence
        for side,rate,best in [('LONG',{'bid':104.5,'ask':104.6},106),('SHORT',{'bid':95.4,'ask':95.5},94)]:
            for amount,units,leverage,pnl in [(200,1,1,5),(1000,5,2,25),(200,1,2,None)]:
                result=trailing_evidence({**self.t,'side':side},{**self.p,'side':side,'amount':amount,'units':units,'leverage':leverage,'unrealized_pnl':pnl},rate,best,self.rules)
                self.assertTrue(result['close_due']);self.assertEqual(result['tier'],'prime')
                self.assertAlmostEqual(result['retreat_atr'],.15)
                self.assertEqual(result['quote_basis'],'bid' if side=='LONG' else 'ask')

    def test_strict_arming_and_configurable_retreat(self):
        self.assertEqual(self.observe(105,0)['tier'],'tracking')
        self.assertEqual(self.observe(110,1800)['tier'],'prime')
        result=self.observe(108,3600,{'profit_trailing_prime_retreat_atr':.25})
        self.assertFalse(result['close_due']);self.assertEqual(result['best_price'],110)
        self.assertTrue(self.observe(107,5400,{'profit_trailing_prime_retreat_atr':.25})['close_due'])

    def test_restart_retains_entry_basis_and_best_price(self):
        import tempfile
        from pathlib import Path
        from position_policy import observe_trailing_profit
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'tracking.db'
            c=sqlite3.connect(path);c.execute('CREATE TABLE trades(id INTEGER PRIMARY KEY)');c.execute('INSERT INTO trades VALUES (1)')
            observe_trailing_profit(c,self.t,self.p,{'bid':112,'ask':112.1},0,self.rules);c.close()
            c=sqlite3.connect(path)
            result=observe_trailing_profit(c,self.t,self.p,{'bid':109,'ask':109.1},1800,self.rules);c.close()
            self.assertTrue(result['close_due']);self.assertEqual(result['best_price'],112)
            self.assertEqual(result['entry_atr'],10)

    def test_short_persisted_low_arms_major_and_upward_retreat_closes(self):
        self.t['side']='SHORT';self.p['side']='SHORT'
        from position_policy import observe_trailing_profit
        def short(ask,now):
            return observe_trailing_profit(self.c,self.t,self.p,{'bid':ask-.1,'ask':ask},now,self.rules)
        self.assertEqual(short(101,0)['tier'],'tracking')
        self.assertEqual(short(94,1800)['tier'],'prime')
        self.assertEqual(short(89,3600)['tier'],'major')
        self.assertFalse(short(85,5400)['close_due'])
        result=short(88,7200)
        self.assertTrue(result['close_due']);self.assertEqual(result['best_price'],85)
        self.assertEqual(result['trigger_price'],87.5)
        self.assertAlmostEqual(result['current_movement_atr'],1.2)

    def test_invalid_quotes_identity_atr_and_configuration(self):
        from position_policy import trailing_evidence,trailing_rules
        for rate in ({'bid':0,'ask':1},{'bid':105,'ask':104},{'bid':float('nan'),'ask':105}, {'bid':105,'ask':106,'executable':False},{'bid':105,'ask':106,'quoteType':'delayed'},{'bid':105,'ask':106,'timestamp':'2000-01-01T00:00:00Z'}):
            with self.assertRaises(ValueError):trailing_evidence(self.t,self.p,rate)
        with self.assertRaises(KeyError):
            trailing_evidence({**self.t,'policy_json':'{}'},self.p,{'bid':105,'ask':106})
        for atr in (0,-1,float('inf')):
            with self.assertRaises(ValueError):
                trailing_evidence(self.t,self.p,{'bid':105,'ask':106},entry_atr=atr)
        with self.assertRaises(ValueError):trailing_evidence(self.t,{**self.p,'position_id':'other'},{'bid':105,'ask':106})
        for rules in ({'profit_trailing_interval_minutes':0},{'profit_trailing_interval_minutes':1441},
                      {'profit_trailing_prime_retreat_atr':-1},{'profit_trailing_major_retreat_atr':float('nan')},
                      {'profit_trailing_major_arm_atr':.5},{'profit_trailing_prime_arm_atr':True},
                      {'profit_trailing_arming_basis':'atr','profit_trailing_prime_retreat_atr':.5}):
            with self.subTest(rules=rules),self.assertRaises(ValueError):trailing_rules(rules)

class AmountArmingTests(unittest.TestCase):
    def setUp(self):
        ProfitTrailingTests.setUp(self)
        self.rules={}

    # Reuse the fixture; the separate suite above explicitly covers ATR mode.
    def observe(self,price,now,rules=None):
        return ProfitTrailingTests.observe(self,price,now,{'profit_trailing_arming_basis':'atr',**(rules or {})})

    def amount(self,price,pnl,now,rules=None):
        from position_policy import observe_trailing_profit
        self.p['unrealized_pnl']=pnl
        return observe_trailing_profit(self.c,self.t,self.p,{'bid':price,'ask':price+.1},now,rules)

    def test_amount_default_strict_boundaries_and_no_atr_gate(self):
        for now,pnl,tier in [(0,10,'tracking'),(1800,10.01,'prime'),(3600,20,'prime'),(5400,20.01,'major')]:
            self.assertEqual(self.amount(101,pnl,now)['tier'],tier)
        self.assertEqual(self.amount(101,3,7200)['tier'],'major')

    def test_cash_peak_persists_for_long_and_short_retreat(self):
        for side,peak,retreat in [('LONG',103,101),('SHORT',97,99)]:
            self.c.execute('DROP TABLE IF EXISTS price_trailing')
            self.t['side']=self.p['side']=side
            self.amount(peak,11,0)
            result=self.amount(retreat,7,1800)
            self.assertTrue(result['close_due']);self.assertEqual(result['peak_profit_usd'],11)
            self.assertAlmostEqual(result['allowed_retreat_atr'],.15)

    def test_atr_movement_does_not_arm_amount_mode_or_multiply_leverage(self):
        self.p['leverage']=2
        self.assertEqual(self.amount(120,6,0)['tier'],'tracking')
        self.assertEqual(self.amount(121,None,1800)['tier'],'tracking')
        self.assertEqual(self.amount(122,float('nan'),3600)['tier'],'tracking')

    def test_basis_switch_resets_peaks_but_freezes_entry_atr(self):
        self.observe(120,0)
        self.t['policy_json']=json.dumps({'atr14':100})
        result=self.amount(103,11,1800)
        self.assertEqual(result['best_price'],103);self.assertEqual(result['entry_atr'],10)
        self.assertFalse(result['close_due'])
        self.assertTrue(self.amount(101,None,3600)['close_due'])

    def test_amount_configuration(self):
        from position_policy import trailing_rules
        self.assertEqual(trailing_rules({'profit_trailing_prime_retreat_atr':.6})['profit_trailing_prime_retreat_atr'],.6)
        for rules in [{'profit_trailing_arming_basis':'both'},{'profit_trailing_prime_arm_usd':True},
                      {'profit_trailing_prime_arm_usd':0},{'profit_trailing_major_arm_usd':10},
                      {'profit_trailing_major_arm_usd':float('nan')}]:
            with self.subTest(rules=rules),self.assertRaises(ValueError):trailing_rules(rules)
