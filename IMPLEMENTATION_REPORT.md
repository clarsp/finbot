# News reaction workflow repairs — 14 October 2026

## Implemented behavior

- Versioned `news_reaction_v2`: positive-news LONG and negative-news SHORT trades; structural trend conflicts keep the news direction subject to countertrend gates. Temporary reaction fades use completed post-news reversal evidence, a pre-news anchor, explicit confidence, direction-aware stops and realistic bounded targets.
- Final execution eligibility is mandatory for automated, manual and sample-replay entry. Missing optional research data is distinct from a rejected thesis. Actual agent rejections stop downstream candidate processing.
- Default caps are **5 units AND $200 account capital/margin including a cost reserve**. Both are configurable under Execution; leverage stays an integer. Each lifecycle explains units, individual constraints, limiting constraint, notional, margin and cost estimates. Market fills are reconciled; an excessive actual new-version fill triggers a close request.
- Portfolio risk includes current marked stop risk, existing gross exposure, cash, position count, single-position exposure and a configurable adverse-gap stress scenario. Optional sector concentration explicitly requires complete labels. It defaults off.
- Shared renewable account leases, atomic order intents and persisted original request references guard concurrent submissions and blind timeout retries. Order lookup resolves uncertain outcomes where the API returns an unambiguous result.
- Independent minute-by-minute broker maintenance adjusts new directional ATR levels around actual fills, requests protection corrections, manages stop/target/time/thesis exits, records broker history net P&L and available fees, and keeps close requests pending until broker evidence confirms absence/closure. Known protection warnings do not disable automation.
- Previously filled positions absent from a complete broker snapshot become P&L-pending instead of rebinding to another position in the same symbol. Unconfirmed submissions remain unresolved. Foreign pending orders and mirrored positions with unverifiable aggregate stop risk are explicit execution dependencies.
- Broker equity/cash snapshots and SPY benchmark observations are separate from local simulations. Broker-reported closed outcomes are grouped by version/framework and fixture-test attribution. Incomplete fees and cash-flow-adjusted performance remain explicit limitations.
- Many-to-many company/article associations preserve multi-company stories. Statement and quote currencies, reporting periods, retrieval time, original availability uncertainty and ADR uncertainty are distinguished. Daily bars respect provider session-close metadata, including early closes.
- The sample purchase replay creates a lifecycle and uses the shared entry/reconciliation/management path. Its status follows that lifecycle. It no longer has a separate direct-submit bypass.
- Hourly reversible archival removes zero-allocation, never-submitted analysis from active lists after three days by default. Broker references, saved intents, uncertain events and executed positions are protected. Administrators can archive now or restore an ID; restoration receives a retention grace period. Archive payloads remain in SQLite.
- One supervised scheduler, a worker heartbeat, HTTP/worker container healthcheck, persistent logs/audit location, pinned direct dependency versions and administrator-only trading/settings controls address operational audit findings. Existing accounts remain; first account creation requires an explicit bootstrap password.

Historical evaluation remains informational and never gates demo orders. Existing automation preferences are preserved, and successful orders do not turn automation off. No credentials were changed and no actual broker requests/orders were made during these repairs.

## Validation evidence

Command:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

**63 tests pass.** Tests use temporary databases and mocked external providers. Coverage includes:

- Real Strategist/Risk/Execution gates and automatic news-cycle submission.
- LONG/SHORT directional entries and both conditional mean reversion directions through the shared final order boundary.
- Confidence, structural/temporary distinction, completed-bar reversal evidence, aggregate risk, stress/concentration, spend/unit/cash limits and broker cost reserves.
- Fresh execution rejection, stale quotes, full portfolios, existing positions, expired/rejected research, definite HTTP rejection and ambiguous HTTP 408 handling.
- Stable references, large broker IDs, sample lifecycle/stake limits, account lease exclusion/release, history pagination, optional fees, protection PATCH and pending-close preservation.
- Side-aware exits, broker closure/P&L evidence, known-absent position identity, zero unrealized P&L, ISO/epoch timestamps, multi-company story mapping and session-aware daily bars.
- Scheduled cleanup, archive/restore preservation and grace periods, authenticated Streamlit dashboard rendering, deferred/executed chart distinctions, request User-Agent/redaction and worker/frontend health checks.

Additional checks passed: Python compilation, `bash -n start.sh`, `docker compose config --quiet`, and `git diff --check`.

A separate migration rehearsal copied `data/finbot.db` through a **read-only source connection**. Additive initialization preserved 378 lifecycle rows and 1,595 event rows, including status, broker IDs, quantities and allocations. Advancing only that disposable copy four days archived and restored 372 eligible analysis lifecycles with exact preservation of the checked records. No original database cleanup was performed during that rehearsal. Normal scheduled cleanup starts with the deployed new worker; records younger than the retention threshold remain visible.

## Remaining evidence and implementation limits

This repairs execution and observability; it does **not** establish profitable predictions. The original audit's empirical strategy-evaluation work remains open:

- No complete point-in-time historical news/fundamental/model replay exists. The SMA-only legacy backtest cannot validate the news strategy.
- No verified deposit/withdrawal history, complete financing reconciliation, confidence calibration, AI-versus-simple-baseline study, or out-of-sample profitability result is available. Observed equity changes are not cash-flow-adjusted strategy returns.
- Sector labels for every external broker position and explicit ADR/statement FX conversion are not supplied automatically. Sector checking safely blocks unknown labels when enabled. A calibrated correlation model and cash-flow-aware daily drawdown circuit breaker remain future work; nominal/stressed risk limits are implemented now.
- Lookup currently automatically resolves one unambiguous position execution. Multi-position/partial-fill allocation and ambiguous broker responses remain flagged for reconciliation rather than guessed or resubmitted. Closed history queries cover 90 days by default and paginate to 10,000 records; older/incomplete history is not a complete account ledger.
- Endpoint compatibility, permissions and protection-update behavior have not been exercised against your actual demo account in this task. The documented API supports these operations, but a mocked test does not prove account-level support. A request acknowledgment is never presented as a confirmed protection change or close.
- Polling is approximately one minute and can wait behind an active account operation. Broker stops/targets are essential between polls. Quotes, provider session metadata, gaps, market closures and broker minimum quantities can still prevent individual orders. No guarantee of staying within intended loss/spend limits through market gaps is possible.
- Direct dependencies are pinned; this is not a complete transitive dependency lock or a built/deployed Docker image verification.

Official API contracts used for the adapter changes: [order lookup](https://api-portal.etoro.com/api-reference/trading--demo/get-order-information-and-position-details), [position modification](https://api-portal.etoro.com/api-reference/trading--demo/modify-stop-loss-and-take-profit-settings-on-an-open-position), [closed trade history](https://api-portal.etoro.com/api-reference/trading--demo/list-trading-history), and [what-if costs](https://api-portal.etoro.com/api-reference/trading--demo/get-what-if-trading-cost-breakdown).

## Files changed in this repair

- `makemoney.py`: agent workflow, strict final gates, versioned policies, durable intents, maintenance, cleanup and broker evidence.
- `trading_workflow.py` (new): pure reaction, sizing and exit rules.
- `workflow_store.py` (new): leases, archive/restore, snapshots and broker outcome storage.
- `integrations.py`: provider provenance, research schema, direction-aware adapter support, order lookup/history/modification/cost requests, headers and normalization.
- `strategy.py`: strategy version and completed-session daily data handling.
- `streamlit_app.py`: active settings, sizing evidence, accounting, cleanup/restore, access control and chart status handling.
- `run_streamlit.py`, `start.sh`, `compose.yaml`, `healthcheck.py` (new), `requirements.txt`: worker supervision, health, persistence and dependencies.
- `tests/test_news_execution_gates.py`, `tests/test_two_direction_workflow.py` (new): updated and expanded offline regressions.
- `README.md`, `LOCAL_MVP.md`, `FIX_SUMMARY.md`, this report and the audit follow-up: current behavior and limits.

Pre-existing workspace database/log changes were preserved. They are not source files to bake into the Docker image.

## Deploy

```sh
docker compose up -d --build --force-recreate
docker compose logs -f finbot
```

This deploys the changed source and starts scheduled maintenance. `./data/finbot.db` remains mounted. Execution configuration contains the new unit/spend/retention controls; existing persisted values take precedence over defaults.
