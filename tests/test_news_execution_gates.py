import unittest
import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

# Mocked broker submissions must never contact a real notification service.
os.environ["FINBOT_DISABLE_EXTERNAL_NOTIFICATIONS"] = "true"

# The regression tests exercise decision gates only. Stub optional provider/data
# packages when this test image has not installed the application's requirements.
if importlib.util.find_spec("pandas") is None:
    pandas_stub = types.ModuleType("pandas")
    pandas_stub.DataFrame = type("DataFrame", (), {})
    pandas_stub.Series = type("Series", (), {})
    pandas_stub.MultiIndex = type("MultiIndex", (), {})
    pandas_stub.DatetimeIndex = type("DatetimeIndex", (), {})
    pandas_stub.Timestamp = type("Timestamp", (), {})
    sys.modules["pandas"] = pandas_stub
if importlib.util.find_spec("yfinance") is None:
    sys.modules["yfinance"] = types.ModuleType("yfinance")
if importlib.util.find_spec("yfscreen") is None:
    sys.modules["yfscreen"] = types.ModuleType("yfscreen")
if importlib.util.find_spec("openai") is None:
    openai_stub = types.ModuleType("openai")
    openai_stub.AsyncOpenAI = type("AsyncOpenAI", (), {})
    sys.modules["openai"] = openai_stub

import makemoney
from integrations import EtoroBrokerAdapter


class NewsExecutionGateRegressionTests(unittest.TestCase):
    def test_news_without_publication_timestamp_remains_analyzable(self):
        grouped = makemoney.group_news_candidates([
            {"ticker": "acme", "title": "Acme announces a new product", "published_at": None},
            {"ticker": "BAD TICKER", "title": "Ignore invalid symbol"},
        ])

        self.assertEqual(list(grouped), ["ACME"])
        self.assertEqual(grouped["ACME"][0]["published_at"], None)
        self.assertEqual(makemoney.newest_known_article_time(grouped["ACME"]), 0.0)

    def test_quote_freshness_uses_check_time_not_cycle_start(self):
        checked_at = makemoney.datetime(2026, 10, 13, 3, 26, 32, tzinfo=makemoney.timezone.utc)
        quote = {
            "available": True, "provider": "eToro",
            "fetched_at": "2026-10-13T03:26:28+00:00",
        }
        self.assertTrue(makemoney.is_fresh_etoro_quote(quote, checked_at=checked_at))
        stale_quote = {**quote, "fetched_at": "2026-10-13T03:25:00+00:00"}
        self.assertFalse(makemoney.is_fresh_etoro_quote(stale_quote, checked_at=checked_at))

    def test_pending_execution_checks_never_authorize_auto_submission(self):
        common = {
            "automation_enabled": True, "research_decision": "accepted",
            "strategy_result": {"decision": "accepted"},
            "strategy": {"position": "LONG"},
            "risk_result": {"decision": "accepted", "execution_ready": False},
        }
        self.assertFalse(makemoney.news_candidate_auto_submit_ready(
            **common, execution_result={"decision": "pending", "status": "waiting_for_execution_data"},
        ))
        self.assertTrue(makemoney.news_candidate_auto_submit_ready(
            **{**common, "risk_result": {"decision": "accepted", "execution_ready": True}},
            execution_result={"decision": "accepted", "status": "ready"},
        ))

    def test_news_lifecycle_status_preserves_agent_rejections_and_capacity_deferrals(self):
        accepted = {"decision": "accepted"}
        self.assertEqual(makemoney.news_candidate_lifecycle_status(
            research_decision="rejected", strategy_result=accepted,
            risk_result=accepted, execution_result=accepted,
        ), "REJECTED_RESEARCH")
        self.assertEqual(makemoney.news_candidate_lifecycle_status(
            research_decision="accepted", strategy_result={"decision": "rejected"},
            risk_result={"decision": "skipped"}, execution_result={"decision": "skipped"},
        ), "REJECTED_STRATEGY")
        self.assertEqual(makemoney.news_candidate_lifecycle_status(
            research_decision="accepted", strategy_result=accepted,
            risk_result={"decision": "accepted", "execution_reasons": ["Open position limit reached (7/5)."]},
            execution_result={"decision": "pending", "reasons": ["Open position limit reached (7/5)."]},
        ), "DEFERRED_CAPACITY")
        self.assertEqual(makemoney.news_candidate_lifecycle_status(
            research_decision="accepted", strategy_result=accepted,
            risk_result=accepted, execution_result={"decision": "pending", "reasons": ["Automated demo orders are disabled."]},
        ), "ANALYSIS_ONLY")

    def test_legacy_news_lifecycle_status_is_inferred_without_database_rewrite(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE position_management (trade_id INTEGER PRIMARY KEY, architecture_version TEXT, origin TEXT);
            CREATE TABLE trades (
                id INTEGER, symbol TEXT, company_name TEXT, status TEXT,
                signal_provider TEXT, side TEXT DEFAULT 'LONG', broker_order_id TEXT, broker_position_id TEXT, opened_at TEXT, created_at TEXT, closed_at TEXT
            );
            CREATE TABLE trade_events (
                id INTEGER, trade_id INTEGER, created_at TEXT, stage TEXT,
                agent_name TEXT, status TEXT, decision TEXT, message TEXT,
                details TEXT
            );
            INSERT INTO trades (id,symbol,company_name,status,signal_provider) VALUES
                (154, 'AVGO', 'Broadcom', 'ANALYSIS_ONLY',
                 'Yahoo Finance News + OpenAI research');
            INSERT INTO trade_events VALUES
                (1, 154, '2026-10-13 04:56:43', 'research', 'researcher', 'completed', 'accepted', '', '{}'),
                (2, 154, '2026-10-13 04:56:43', 'strategy', 'strategist', 'completed', 'accepted', '', '{}'),
                (3, 154, '2026-10-13 04:56:43', 'risk', 'risk_manager', 'completed', 'accepted', '', '{"execution_reasons":["Open position limit reached (7/5)."]}'),
                (4, 154, '2026-10-13 04:56:43', 'execution', 'execution', 'completed', 'pending', '', '{"decision":"pending","reasons":["Open position limit reached (7/5)."]}');
        """)
        with patch.object(makemoney, "get_connection", return_value=connection):
            trade = makemoney.get_trade_history()[0]

        self.assertEqual(trade["status"], "DEFERRED_CAPACITY")
        self.assertEqual(trade["stored_status"], "ANALYSIS_ONLY")
        self.assertTrue(trade["status_inferred_from_events"])

    def test_reconciliation_instrument_id_is_read_from_saved_strategy_context(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE trade_events (trade_id INTEGER, stage TEXT, id INTEGER, details TEXT)")
        connection.execute(
            "INSERT INTO trade_events VALUES (?, 'strategy', ?, ?)",
            (87, 310, json.dumps({"analysis": {"market_quote": {"instrument_id": 4261}}})),
        )
        self.assertEqual(makemoney._instrument_id_from_trade_events(connection, 87), "4261")
        connection.close()

    def test_known_position_protection_mismatch_does_not_freeze_all_new_orders(self):
        self.assertTrue(makemoney.reconciliation_blocks_new_order({
            "status": "RECONCILIATION_REQUIRED",
            "reconciliation_status": "position_not_confirmed_by_broker",
            "broker_position_id": None,
        }))
        self.assertTrue(makemoney.reconciliation_blocks_new_order({
            "status": "SUBMITTED_PENDING_RECONCILIATION",
            "reconciliation_status": "awaiting_broker_position",
            "broker_position_id": None,
        }))
        self.assertFalse(makemoney.reconciliation_blocks_new_order({
            "status": "RECONCILIATION_REQUIRED",
            "reconciliation_status": "unexpected_broker_take_profit",
            "broker_position_id": "broker-position-1",
        }))

    def test_strategist_does_not_reject_direction_for_missing_quote_or_sma(self):
        rules = dict(makemoney.DEFAULT_AGENT_RULES["strategist"])
        candidate = {
            "ticker": "ACME",
            "company_name": "Acme",
            "signal": "bullish",
            "confidence": 1.0,
            "price": None,
            "market_quote": {"available": False, "provider": "eToro"},
            "fundamentals": {},
            "daily_trend_context": {"available": False, "trend_state": "unknown"},
        }

        result = makemoney.evaluate_strategist_gate(candidate, "LONG", [], rules)

        self.assertTrue(result["accepted"], result["rejection_reasons"])
        self.assertEqual(result["daily_trend_alignment"], "unavailable")
        self.assertFalse(result["market_quote_available"])

    def test_conditional_mean_reversion_long_and_short_are_accepted_with_entry_zones(self):
        rules = dict(makemoney.DEFAULT_AGENT_RULES["strategist"])
        scenarios = [
            ("bearish", "bullish", 95.0, "LONG", 92.0, 110.0),
            ("bullish", "bearish", 105.0, "SHORT", 90.0, 108.0),
        ]
        for sentiment, trend, price, expected_position, floor, ceiling in scenarios:
            candidate = {
                "ticker": "ACME", "company_name": "Acme", "signal": sentiment,
                "company_news_sentiment": sentiment, "confidence": 0.9,
                "ai_analysis": {"reaction_assessment": "temporary"},
                "news_reaction": {"available": True, "anchor": 110 if expected_position == "LONG" else 90,
                                  "low": 90, "high": 110, "last": price,
                                  "previous": price-1 if expected_position == "LONG" else price+1},
                "price": price,
                "daily_trend_context": {
                    "available": True, "trend_state": trend, "sma50": 100.0,
                    "sma200": 90.0 if trend == "bullish" else 110.0, "atr14": 10.0,
                },
                "fundamentals": {},
            }
            with patch.object(makemoney, "get_agent_rules", return_value={"strategist": rules}), patch.object(
                makemoney, "record_agent_event"
            ):
                result = makemoney.StrategistAgent().analyze_and_decide({
                    "tech_data": candidate, "energy_data": candidate, "recent_news": [],
                })
            self.assertEqual(result["position"], expected_position)
            self.assertTrue(result["decision_gate"]["accepted"], result["decision_gate"]["rejection_reasons"])
            self.assertEqual(result["decision_gate"]["strategy_framework"], "CONDITIONAL_MEAN_REVERSION")
            self.assertIn(f"ENTRY_ZONE: {floor:.4f} to {ceiling:.4f}", result["notes"])
            self.assertIn("SOURCE_ATR: 10", result["notes"])

    def test_conditional_mean_reversion_rejects_insufficient_sma_variance(self):
        candidate = {
            "ticker": "ACME", "company_news_sentiment": "bearish", "confidence": 0.2,
            "price": 110.0, "fundamentals": {},
            "ai_analysis": {"reaction_assessment": "temporary"},
            "news_reaction": {"available": True, "anchor": 115, "low": 108, "high": 115, "last": 110, "previous": 109},
            "daily_trend_context": {
                "available": True, "trend_state": "bullish", "sma50": 100.0,
                "sma200": 90.0, "atr14": 10.0,
            },
        }
        result = makemoney.evaluate_strategist_gate(
            candidate, "LONG", [], dict(makemoney.DEFAULT_AGENT_RULES["strategist"]),
        )
        self.assertFalse(result["accepted"])
        self.assertIn(
            "Asset lacks sufficient mathematical variance from the pre-news anchor to justify a countertrend entry.",
            result["rejection_reasons"],
        )
        self.assertFalse(any("countertrend confidence" in reason.lower() for reason in result["rejection_reasons"]))

    def test_risk_analysis_completes_but_marks_missing_execution_inputs(self):
        rules = {
            **makemoney.DEFAULT_AGENT_RULES,
            "risk_manager": {**makemoney.DEFAULT_AGENT_RULES["risk_manager"], "enabled": True},
        }
        with patch.object(makemoney, "get_agent_rules", return_value=rules), patch.object(
            makemoney, "record_agent_event"
        ):
            result = makemoney.RiskManagerAgent().review_news_demo_candidate(
                candidate={
                    "ticker": "ACME",
                    "confidence": 0.9,
                    "market_quote": {"available": False},
                    "daily_trend_context": {"available": False, "trend_state": "unknown"},
                },
                strategy={"position": "LONG"},
                account={"available": False},
                broker_snapshot={"available": False, "positions": []},
                instrument={},
                atr14=None,
            )

        self.assertEqual(result["decision"], "accepted")
        self.assertFalse(result["execution_ready"])
        self.assertTrue(any("quote" in reason.lower() for reason in result["execution_reasons"]))

    def test_inconclusive_historical_evaluation_does_not_gate_demo_execution(self):
        rules = {**makemoney.DEFAULT_AGENT_RULES, "execution": {"enabled": True}}
        with patch.object(makemoney, "get_agent_rules", return_value=rules), patch.object(
            makemoney, "record_agent_event"
        ), patch.object(makemoney,'weekend_entry_block_reason',return_value=None):
            result = makemoney.ExecutionAgent().review_news_demo_candidate(
                candidate={"ticker": "ACME"},
                strategy={"position": "LONG", "decision_gate": {"accepted": True}},
                risk={"decision": "accepted", "execution_ready": True, "execution_reasons": []},
                evaluation={"status": "inconclusive"},
                automation_enabled=True,
            )

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["decision"], "accepted")
        self.assertNotIn("chronological", " ".join(result["reasons"]).lower())

    def test_automation_toggle_does_not_read_or_require_evaluation(self):
        settings = types.SimpleNamespace(broker_provider="etoro", etoro_demo_trading=True)
        connection = MagicMock()
        with patch.object(makemoney.IntegrationSettings, "from_environment", return_value=settings), patch.object(
            makemoney, "get_connection", return_value=connection
        ), patch.object(makemoney, "get_strategy_evaluation", side_effect=AssertionError("evaluation must not gate")):
            makemoney.set_demo_automated_trading_enabled(True)

        connection.execute.assert_called_once()
        connection.commit.assert_called_once()

    def test_automated_demo_setting_survives_database_initialization(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "finbot.db"
            settings = types.SimpleNamespace(broker_provider="etoro", etoro_demo_trading=True)
            with patch.object(makemoney, "DB_PATH", database_path), patch.object(
                makemoney.IntegrationSettings, "from_environment", return_value=settings
            ):
                makemoney.ensure_db()
                makemoney.set_demo_automated_trading_enabled(True)
                makemoney.ensure_db()
                self.assertTrue(makemoney.get_demo_automated_trading_enabled())

    def test_broker_take_profit_mismatch_requires_reconciliation(self):
        trade = {
            "id": 87, "symbol": "DELL", "status": "SUBMITTED_PENDING_RECONCILIATION",
            "broker_position_id": "position-1", "broker_order_id": "order-1",
            "stop_loss_price": 537.4586, "take_profit_price": None,
        }
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [trade]
        position = {
            "position_id": "position-1", "symbol": "DELL", "open_rate": 588.0,
            "units": 10.47801, "stop_loss_price": 537.4586,
            "take_profit_price": 3527.99,
        }

        with patch.object(makemoney, "get_connection", return_value=connection), patch.object(
            makemoney, "record_trade_event"
        ) as record_event:
            result = makemoney.reconcile_news_demo_positions([position])

        updates = [call.args for call in connection.execute.call_args_list]
        self.assertTrue(any("unexpected_broker_take_profit" in args[1] for args in updates if len(args) > 1 and isinstance(args[1], tuple)))
        self.assertTrue(any("RECONCILIATION_REQUIRED" in args[1] for args in updates if len(args) > 1 and isinstance(args[1], tuple)))
        self.assertEqual(result["errors"], 1)
        self.assertTrue(any("3527.99" in call.args[5] for call in record_event.call_args_list))

    def test_etoro_order_request_explicitly_disables_take_profit(self):
        adapter = object.__new__(EtoroBrokerAdapter)
        adapter.settings = types.SimpleNamespace(etoro_demo_trading=True, etoro_api_debug_logging=False)
        adapter.api_logs = []
        adapter._find_instrument = MagicMock(return_value={"instrumentId": 123, "currency": "USD"})
        adapter._request_json = MagicMock(return_value={"orderId": 456})

        adapter.submit_order({
            "symbol": "DELL", "side": "LONG", "amount": 100,
            "leverage": 2, "stop_loss_price": 537.4586,
            "take_profit_price": None, "currency": "usd",
        })

        payload = adapter._request_json.call_args.kwargs["payload"]
        self.assertIsNone(payload["takeProfitRate"])
        self.assertIs(payload["isNoTakeProfit"], True)

    def test_news_research_cycle_handles_fresh_quote_without_name_error(self):
        now = makemoney.datetime.now(makemoney.timezone.utc).isoformat()
        settings = types.SimpleNamespace(
            broker_provider="etoro", etoro_demo_trading=True, openai_api_key=""
        )
        adapter = MagicMock()
        adapter.get_account_snapshot.return_value = {"available": True, "equity": 10000, "cash_available": 10000}
        adapter.get_positions.return_value = {"available": True, "positions": []}
        adapter.resolve_instrument.return_value = {
            "instrument_id": "1", "currency": "USD", "tradeable": True,
            "supported_asset": True,
        }
        bars = types.SimpleNamespace(empty=True)
        research_result = {
            "status": "available", "signal": "bullish", "confidence": 0.95,
            "catalyst": "earnings", "summary": "Positive company news.",
        }
        strategy_result = {
            "position": "LONG", "leverage": 2,
            "decision_gate": {"accepted": True, "rejection_reasons": []},
        }
        risk_result = {"decision": "accepted", "reasons": [], "execution_reasons": [], "execution_ready": False}
        execution_result = {"decision": "pending", "status": "automation_disabled", "reasons": []}
        researcher = MagicMock()
        researcher.analyze_news_candidate = AsyncMock(return_value=research_result)
        strategist = MagicMock()
        strategist.analyze_and_decide.return_value = strategy_result
        risk = MagicMock()
        risk.review_news_demo_candidate.return_value = risk_result
        execution = MagicMock()
        execution.review_news_demo_candidate.return_value = execution_result

        class NewsProvider:
            def __init__(self, _settings):
                pass

            async def fetch_news(self):
                return [{"ticker": "ACME", "title": "Acme reports strong growth", "published_at": None}]

        with ExitStack() as stack:
            patches = [
                patch.object(makemoney, "get_demo_automated_trading_enabled", return_value=False),
                patch.object(makemoney, "get_strategy_evaluation", return_value={"status": "inconclusive"}),
                patch.object(makemoney, "EtoroBrokerAdapter", return_value=adapter),
                patch.object(makemoney, "reconcile_news_demo_positions", return_value={"errors": 0}),
                patch.object(makemoney, "YahooFinanceNewsProvider", NewsProvider),
                patch.object(makemoney, "save_news_articles", return_value={"inserted": 1, "updated": 0}),
                patch.object(makemoney, "fetch_company_fundamentals", new=AsyncMock(return_value={"company_name": "Acme", "data_status": "available"})),
                patch.object(makemoney, "fetch_market_quote", new=AsyncMock(return_value={"available": True, "provider": "eToro", "ask": 100, "bid": 99, "fetched_at": now})),
                patch.object(makemoney, "fetch_market_trends", new=AsyncMock(return_value={"data_source": "test"})),
                patch.object(makemoney, "load_daily_bars", return_value=bars),
                patch.object(makemoney, "evaluate_latest_signal", return_value={"available": False, "trend_state": "unknown"}),
                patch.object(makemoney, "ResearcherAgent", return_value=researcher),
                patch.object(makemoney, "StrategistAgent", return_value=strategist),
                patch.object(makemoney, "RiskManagerAgent", return_value=risk),
                patch.object(makemoney, "ExecutionAgent", return_value=execution),
                patch.object(makemoney, "get_agent_rules", return_value=makemoney.DEFAULT_AGENT_RULES),
                patch.object(makemoney, "get_ai_research_enabled", return_value=False),
                patch.object(makemoney, "save_trade", return_value=501),
                patch.object(makemoney, "record_trade_event"),
                patch.object(makemoney, "record_agent_event"),
                patch.object(makemoney, "save_signal"),
            ]
            for context in patches:
                stack.enter_context(context)
            result = asyncio.run(makemoney.run_etoro_news_research_cycle(settings))
            researcher.analyze_news_candidate.return_value = {
                "status": "available", "signal": "neutral", "confidence": 0.2,
                "summary": "No directional evidence.",
            }
            rejected_result = asyncio.run(makemoney.run_etoro_news_research_cycle(settings))

        self.assertEqual(result["status"], "analysis_complete")
        self.assertEqual(result["research_candidates"][0]["analysis_decision"], "accepted")
        rejected_candidate = rejected_result["research_candidates"][0]
        self.assertEqual(rejected_candidate["analysis_decision"], "rejected")
        self.assertEqual(rejected_candidate["agent_workflow"]["strategist"]["decision"], "skipped")
        self.assertEqual(rejected_candidate["agent_workflow"]["risk_manager"]["decision"], "skipped")
        self.assertEqual(rejected_candidate["agent_workflow"]["execution"]["decision"], "skipped")
        self.assertEqual(strategist.analyze_and_decide.call_count, 1)
        self.assertEqual(risk.review_news_demo_candidate.call_count, 1)
        self.assertEqual(execution.review_news_demo_candidate.call_count, 1)


if __name__ == "__main__":
    unittest.main()
