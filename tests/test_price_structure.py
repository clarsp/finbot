import unittest
import pandas as pd
from price_structure import daily_structure, structure_target
from trading_workflow import size_order

class StructureTests(unittest.TestCase):
    def test_targets_are_side_aware_and_do_not_extend_baseline(self):
        structure={'available':True,'resistance':[{'low':105,'high':105.2}],
                   'support':[{'low':94.8,'high':95}]}
        self.assertAlmostEqual(structure_target('LONG',100,106,2,structure)[0],104.8)
        self.assertAlmostEqual(structure_target('SHORT',100,94,2,structure)[0],95.2)
        self.assertEqual(structure_target('LONG',100,103,2,structure)[0],103)

    def test_small_profit_cannot_bypass_net_reward_risk_using_leverage(self):
        for leverage in (1,2,3):
            result=size_order(side='LONG',entry=100,atr=2,equity=10000,cash=10000,
                leverage=leverage,positions=[],risk_rules={},execution_rules={},
                structure={'available':True,'resistance':[{'low':102,'high':102.2}]})
            self.assertTrue(result['execution_reasons'])
            self.assertEqual(result['proposed_stop'],96)

    def test_zone_containing_entry_blocks_trade(self):
        target,_=structure_target('LONG',100,106,2,
            {'available':True,'resistance':[{'low':99.9,'high':100.1}]})
        self.assertLess(target,100)

    def test_pivots_need_two_confirming_bars_and_independent_touches(self):
        highs=[101.]*25
        highs[5]=105;highs[12]=105.2;highs[-1]=120
        bars=pd.DataFrame({'High':highs,'Low':[99.]*25,'atr14':[2.]*25},
                          index=pd.date_range('2026-01-01',periods=25,tz='UTC'))
        zones=daily_structure(bars)['resistance']
        self.assertEqual(len(zones),1)
        self.assertEqual(zones[0]['touches'],2)
        self.assertEqual(zones[0]['high'],105.2)

    def test_unavailable_data_preserves_baseline(self):
        self.assertEqual(structure_target('SHORT',100,94,2,{'available':False})[0],94)

class MultiTimeframeTests(unittest.TestCase):
    def bars(self):
        return pd.DataFrame({'High':[101.]*24+[105.], 'Low':[99.]*25,
                             'Close':[100.]*24+[104.], 'atr14':[2.]*25},
                            index=pd.bdate_range('2026-09-14',periods=25,tz='America/New_York'))

    def test_weekly_monthly_capture_emerging_high_without_claiming_confirmed_swing(self):
        from price_structure import multi_timeframe_structure
        structure=multi_timeframe_structure(self.bars())
        self.assertEqual(structure['timeframes'],['daily','weekly','monthly'])
        highs=[z for z in structure['resistance'] if z['low']==105]
        self.assertEqual({z['timeframe'] for z in highs},{'weekly','monthly'})
        self.assertTrue(all(z['kind']=='period_extreme' for z in highs))
        monthly=next(z for z in highs if z['timeframe']=='monthly')
        self.assertFalse(monthly['period_complete'])
        self.assertEqual(monthly['source_through'],self.bars().index[-1].isoformat())
        target,evidence=structure_target('LONG',104,110,2,structure)
        self.assertAlmostEqual(target,104.8)
        self.assertTrue(evidence['adjusted'])

    def test_monthly_support_caps_short_symmetrically(self):
        from price_structure import multi_timeframe_structure
        structure=multi_timeframe_structure(self.bars())
        target,evidence=structure_target('SHORT',100,94,2,structure)
        self.assertAlmostEqual(target,99.2)
        self.assertIn(evidence['zone']['timeframe'],('weekly','monthly'))

class RangeBoundaryTests(unittest.TestCase):
    def structure(self):
        return {'available':True,'as_of':'2026-10-16T00:00:00Z',
            'resistance':[
                {'low':40,'high':40,'timeframe':'weekly','source_through':'2026-10-16T00:00:00Z'},
                {'low':49,'high':49,'timeframe':'weekly','source_through':'2026-10-09T00:00:00Z'},
                {'low':49,'high':49,'timeframe':'monthly','source_through':'2026-10-16T00:00:00Z'},
                {'low':200,'high':200,'timeframe':'monthly','source_through':'2026-08-31T00:00:00Z'},
                {'low':300,'high':300,'timeframe':'monthly','source_through':'2026-11-30T00:00:00Z'}],
            'support':[
                {'low':60,'high':60,'timeframe':'weekly','source_through':'2026-10-16T00:00:00Z'},
                {'low':51,'high':51,'timeframe':'monthly','source_through':'2026-10-16T00:00:00Z'}]}

    def test_higher_monthly_boundary_replaces_closest_weekly_cap(self):
        target,e=structure_target('LONG',37,68,2,self.structure(),mode='range_boundary')
        self.assertAlmostEqual(target,48.8)
        self.assertEqual(e['nearest_rule_target'],39.8)
        self.assertEqual(e['boundary_timeframe'],'monthly')
        self.assertTrue(e['requires_nearby_breakout'])
        self.assertEqual(e['zone']['low'],49)

    def test_atr_target_is_not_extended_to_monthly_boundary(self):
        self.assertEqual(structure_target('LONG',37,45,2,self.structure(),mode='range_boundary')[0],45)

    def test_short_is_symmetric_and_monthly_boundary_wins(self):
        self.assertAlmostEqual(structure_target('SHORT',63,30,2,self.structure(),mode='range_boundary')[0],51.2)
        self.assertEqual(structure_target('SHORT',63,55,2,self.structure(),mode='range_boundary')[0],55)

    def test_monthly_cap_cannot_be_bypassed_when_entry_already_broke_it(self):
        target,e=structure_target('LONG',50,68,2,self.structure(),mode='range_boundary')
        self.assertLess(target,50)

    def test_weekly_fallback_and_missing_dates_preserve_nearest_behavior(self):
        s=self.structure();s['resistance']=[z for z in s['resistance'] if z['timeframe']=='weekly']
        target,e=structure_target('LONG',37,68,2,s,mode='range_boundary')
        self.assertAlmostEqual(target,48.8);self.assertEqual(e['boundary_timeframe'],'weekly')
        s.pop('as_of')
        target,e=structure_target('LONG',37,68,2,s,mode='range_boundary')
        self.assertAlmostEqual(target,39.8);self.assertEqual(e['mode'],'nearest')

    def test_default_function_stays_nearest_for_historical_and_shadow_consumers(self):
        self.assertAlmostEqual(structure_target('LONG',37,68,2,self.structure())[0],39.8)
