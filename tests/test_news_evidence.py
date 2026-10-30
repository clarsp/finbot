import asyncio
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import news_evidence as news
import trading_workflow as workflow


class HeadlineSelectionTests(unittest.TestCase):
    def test_company_news_displaces_unrelated_first_eight_and_deduplicates(self):
        articles = [{"title": f"Microsoft story {i}", "url": str(i)} for i in range(8)]
        articles += [{"title": "Amazon announces earnings", "url": "a"},
                     {"title": "Amazon announces earnings", "url": "syndicated-copy"}]
        selected, audit = news.select_headlines("AMZN", "Amazon.com Inc.", articles)
        self.assertEqual(selected[0]["title"], "Amazon announces earnings")
        self.assertEqual(audit["unique"], 9)
        self.assertEqual(audit["direct_text_matches"], 1)
        self.assertEqual(len(selected), 8)

    def test_symbol_substring_is_not_company_relevance(self):
        selected, audit = news.select_headlines("CAT", "Caterpillar Inc.", [{"title": "Education spending grows"}])
        self.assertEqual(audit["direct_text_matches"], 0)
        self.assertEqual(len(selected), 1)  # uncertain evidence is not silently discarded

    def test_index_prioritizes_macro_over_fund_issuer(self):
        selected, audit = news.select_headlines("VOO", "Vanguard S&P 500 ETF", [
            {"title": "Vanguard hires a new executive"}, {"title": "S&P 500 rises after inflation data"}])
        self.assertIn("inflation", selected[0]["title"])
        self.assertEqual(audit["direct_text_matches"], 1)

    def test_newer_direct_evidence_precedes_older_direct_evidence(self):
        selected, _ = news.select_headlines("TSLA", "Tesla", [
            {"title": "Tesla old", "published_at": "2026-10-20T10:00:00Z"},
            {"title": "Tesla new", "published_at": "2026-10-22T10:00:00Z"}])
        self.assertEqual(selected[0]["title"], "Tesla new")


class NeutralReuseTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE neutral_research_cache(symbol TEXT PRIMARY KEY,fingerprint TEXT,result_json TEXT,analyzed_at REAL,reuse_count INTEGER)")
        self.result = {"status": "available", "signal": "neutral", "confidence": .5}

    def tearDown(self):
        self.conn.close()

    def test_reuse_expires_and_changed_evidence_does_not_reuse(self):
        news.remember_neutral(self.conn, "TSLA", "a", self.result, now=100)
        self.assertIsNone(news.cached_neutral(self.conn, "TSLA", "b", now=101))
        self.assertEqual(news.cached_neutral(self.conn, "TSLA", "a", now=101)["neutral_reuse"]["reuse_count"], 1)
        self.assertIsNone(news.cached_neutral(self.conn, "TSLA", "a", now=7300))

    def test_unavailable_is_not_cached_and_directional_result_clears_neutral(self):
        news.remember_neutral(self.conn, "TSLA", "a", {**self.result, "status": "unavailable"}, now=100)
        self.assertIsNone(news.cached_neutral(self.conn, "TSLA", "a", now=101))
        news.remember_neutral(self.conn, "TSLA", "a", self.result, now=100)
        news.remember_neutral(self.conn, "TSLA", "b", {**self.result, "signal": "bullish"}, now=101)
        self.assertIsNone(news.cached_neutral(self.conn, "TSLA", "a", now=102))

    def test_fingerprint_ignores_poll_clocks_but_preserves_market_changes(self):
        a = {"headlines": [{"title": "Tesla earnings"}], "market_context": {"price": 100, "observed_at": "a", "local_timestamp": "a"}}
        b = copy.deepcopy(a); b["market_context"].update(observed_at="b", local_timestamp="b")
        self.assertEqual(news.neutral_fingerprint(a, "m"), news.neutral_fingerprint(b, "m"))
        b["market_context"]["price"] = 101
        self.assertNotEqual(news.neutral_fingerprint(a, "m"), news.neutral_fingerprint(b, "m"))
        self.assertNotEqual(news.neutral_fingerprint(a, "m"), news.neutral_fingerprint(a, "new-model"))

    def test_researcher_reuses_neutral_but_reanalyzes_changed_news(self):
        import makemoney as app
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"test.db"
            c = sqlite3.connect(path)
            c.execute("CREATE TABLE neutral_research_cache(symbol TEXT PRIMARY KEY,fingerprint TEXT,result_json TEXT,analyzed_at REAL,reuse_count INTEGER)"); c.close()
            adapter = SimpleNamespace(settings=SimpleNamespace(openai_model="test"), research=AsyncMock(return_value=self.result))
            args = dict(ticker="TSLA", company_name="Tesla", sector="Technology", headlines=[{"title": "Tesla earnings"}], fundamentals={}, market_context={}, research_adapter=adapter)
            with patch.object(app, "get_connection", side_effect=lambda: sqlite3.connect(path)), patch.object(app, "get_agent_rules", return_value={"researcher": {"enabled": True}}):
                asyncio.run(app.ResearcherAgent().analyze_news_candidate(**args))
                reused = asyncio.run(app.ResearcherAgent().analyze_news_candidate(**args))
                self.assertIn("neutral_reuse", reused)
                self.assertEqual(adapter.research.await_count, 1)
                args["headlines"] = [{"title": "Tesla new earnings guidance"}]
                asyncio.run(app.ResearcherAgent().analyze_news_candidate(**args))
                self.assertEqual(adapter.research.await_count, 2)


class EconomicGateTests(unittest.TestCase):
    def size(self, side, **rules):
        return workflow.size_order(side=side, entry=100, atr=2, equity=10000, cash=10000, leverage=2,
            positions=[], risk_rules={"stop_atr_multiple": 2, "target_reward_risk": 1, "minimum_reward_risk": 1, **rules}, execution_rules={})

    def test_costs_apply_to_uncapped_targets_in_both_directions(self):
        for side in ("LONG", "SHORT"):
            r = self.size(side)
            self.assertGreater(r["estimated_net_target_profit"], 0)
            self.assertFalse(r["economic_gate"]["passed"])
            self.assertIn("below_minimum_reward_risk", r["economic_gate"]["failure_codes"])
            self.assertIn("profitable", r["rejection_reasons"][0])

    def test_larger_units_do_not_improve_reward_risk(self):
        a = self.size("LONG", target_reward_risk=1.5)
        b = workflow.size_order(side="LONG", entry=100, atr=2, equity=10000, cash=10000, leverage=2,
            positions=[], risk_rules={"stop_atr_multiple": 2, "target_reward_risk": 1.5}, execution_rules={"max_order_spend_usd": 1000, "max_order_units": 10})
        self.assertGreater(b["estimated_net_target_profit"], a["estimated_net_target_profit"])
        self.assertAlmostEqual(b["economic_gate"]["net_reward_risk"], a["economic_gate"]["net_reward_risk"])

    def test_nonpositive_net_target_has_distinct_reason(self):
        r = self.size("SHORT", estimated_roundtrip_cost_bps=1000)
        self.assertIn("non_positive_net_reward", r["economic_gate"]["failure_codes"])
        self.assertIn("does not cover estimated costs", r["rejection_reasons"][0])


if __name__ == "__main__":
    unittest.main()
