# Finbot feasibility and gap audit

Date: 14 October 2026. Scope: the current working tree, Docker startup, Python dependencies, the existing regression suite, and a read-only inspection of `data/finbot.db`. This is an application and strategy review, not a measured profitability result. The deployed container was not inspected; no eToro requests or orders were submitted during this audit.

## Assessment

Finbot is a feasible demo research and execution platform. Its useful foundation includes dynamic news discovery, company data, completed daily indicators, broker instrument resolution, explicit demo routing, persisted agent evidence, and broker order/position references. It has demonstrated order submission and broker fills.

It is not yet a complete, measurable trading system. The highest-priority gaps are enforcement at the final order boundary, position management and closure reconciliation, portfolio accounting, and evidence of predictive value. More agents, higher model confidence, or more executions will not resolve those gaps by themselves.

The current strategy has no demonstrated positive net expectancy. Its active evaluation function always reports inconclusive because a historical news/research replay is unavailable. That should remain a reporting limitation rather than become another blanket demo-order lock.

## Evidence available

- The mounted local database has 370 trade lifecycle rows spanning 11–14 October 2026.
- Four rows have eToro order/position references and recorded fills: DELL #87, MA #253, META #277, and UNH #283. All four have `unexpected_broker_take_profit` reconciliation status and no recorded broker fees.
- Two older `OPEN` rows have no broker references and represent a different local execution history. They must not be counted as confirmed eToro fills.
- No reconciled closed broker trades are available in this database to establish realized net performance.
- The separate test-workflow table has one failed run.
- The inspected settings enable demo automation and schedule research every 60 minutes. The local environment sets a 30-position maximum.
- The research usage table records 392 calls and 549,810 tokens under `gpt-4o-mini`; the environment also selects that model. The model conducting this audit does not change the application's research model.
- The existing suite ran 17 tests: 16 passed, one failed. The failure is the entry-zone test still expecting the previous 1.5× ATR default after the default changed to 1.25×. It demonstrates incomplete regression maintenance, rather than a failed broker request.
- An isolated, fully mocked execution reproduction reached `submit_broker_order` with a fresh risk result containing `execution_ready=False` and a missing-existing-position-risk warning.
- An isolated chart reproduction returned no execution point for an explicit `decision='deferred'` event.

## Findings and repair requirements

### 1. Critical: fresh risk checks can be bypassed at submission

Evidence: `makemoney.py:3841`. `place_analysis_candidate_demo_order()` tests `decision`, units, and stop, but does not require `execution_ready` or an empty `execution_reasons` list. Risk Manager deliberately keeps thesis acceptance separate from execution eligibility. Positive units can therefore coexist with a position-cap violation, disabled risk agent, or incomplete aggregate-risk inputs.

The automated cycle performs an earlier readiness check, but calls this same helper for the final fresh check. A changed account state can therefore fail the new execution checks and still reach submission. The mocked reproduction confirmed this path.

Repair: require the complete fresh execution gate at the final broker boundary. Use the same invariant for automatic, manual, and test submissions. Verify scenarios involving a full portfolio, missing stop data, and a changed account snapshot.

### 2. Critical: the active strategy lacks a complete exit policy

Evidence: `run_etoro_news_research_cycle()` and `place_analysis_candidate_demo_order()` create long entries with stops and no intended take-profit. Automatic SMA-based exits exist in the retired trend cycle, not the active news cycle. Active positions otherwise rely on broker protection or explicit manual closure.

There is no active news-thesis expiry, maximum holding time, profit-taking policy, or automatic exit after a contrary updated thesis. An indefinite leveraged holding period is materially different from a short-term news trade.

Repair: define and implement a versioned exit policy for each strategy track. Decide the expected holding horizon and how an invalidated catalyst, profitable move, and time expiry lead to an actual broker close request. Reconcile that close independently of news discovery.

### 3. High: broker closures and unknown outcomes can leave persistent blockers

Evidence: `makemoney.py:3168`, `makemoney.py:3185`, `makemoney.py:1791`. Known protection mismatches are exempt from the global submission blocker. A missing position without an earlier local close request becomes unresolved, however, so a stop-hit or broker-side manual close can leave a persistent reconciliation requirement. Unknown submissions also lack an implemented order-history resolution path.

All four recorded broker trades have a TP mismatch. The in-app close action requires status `OPEN`, which prevents closing those reconciliation-required positions through that action despite their known broker position IDs.

Repair: reconcile orders, open positions, and closed-position/fill history. Separate uncertainty about whether an order exists from a known open position with a protection mismatch. Allow a verified known position to be managed while preserving its warning. Do not describe missing realized P&L as an open position indefinitely.

These are application implementation gaps. This audit does not establish that eToro lacks the necessary API capabilities.

### 4. High: portfolio constraints are inconsistent

Evidence: `makemoney.py:2403–2455`. The configured gross-exposure limit caps the proposed position, rather than existing total gross exposure plus the proposal. Risk Manager independently chooses default leverage rather than consistently using the Strategist's capped leverage. A configuration with default leverage above max leverage can therefore produce different approved and submitted values.

Open risk uses absolute entry-to-stop distance. It does not represent gap risk, financing, correlated holdings, or a loss limit measured from current marked equity. A stop above entry also needs direction-aware treatment rather than an absolute distance.

Repair: calculate projected portfolio totals using verified broker holdings and reserved orders. Enforce position count, gross notional exposure, cash/margin, and stop-risk constraints together. Use one approved leverage value throughout. Add sector/concentration limits and distinguish nominal stop risk from stress loss.

### 5. High: concurrency protection is incomplete

Evidence: `start.sh:33` launches a worker, and `run_streamlit.py:85` launches another. The scheduled-cycle claim uses `BEGIN IMMEDIATE`, which prevents two workers from simply claiming the same due time. It is not a lease covering the whole cycle, and manual cycles do not share it. Overlap is possible when a cycle runs beyond its interval or a user starts another cycle.

The atomic `ANALYSIS_ONLY` → `SUBMITTING` update protects one lifecycle. It does not reserve capacity across different lifecycles for the same instrument or portfolio. Random request IDs provide traceability but the code does not establish broker-supported order idempotency.

Repair: run one supervised execution worker, serialize account-level reservations, and use a durable order-intent identifier. Resolve crashes and network timeouts before retrying an uncertain order. Preserve the useful per-lifecycle compare-and-set protection.

### 6. High: the mean-reversion track does not identify conventional pullback entries

Evidence: `makemoney.py:2252`, `makemoney.py:2788`. A bullish trend plus bearish news proposes LONG, but approval requires current price to be sufficiently ABOVE SMA50. This identifies an elevated price in an uptrend, not an observed dip below that reference. The short rule is symmetric below SMA50.

This matches the earlier explicitly requested inequalities. The strategic interpretation still needs correction: either name it a trend-continuation/news-disagreement trade, or define a genuine pullback-and-recovery trigger. SMA distance alone does not establish that bearish news is temporary or already priced in.

Conditional approval also sets required confidence to zero. The existing test intentionally accepts a 20% confidence setup when the distance rule passes. A technical extension can therefore override weak news evidence.

The previous change from 1.5× to 1.25× made #353's 1.269× distance pass the variance test on the same inputs. That is retrospective threshold adjustment, not evidence of improved returns. Treat it as a separately versioned demo experiment, preserve the prior variant, and compare future outcomes.

### 7. High: the system cannot yet measure its objective

Evidence: `makemoney.py:549`, `makemoney.py:1224`, `strategy.py:115`. Active evaluation is a fixed inconclusive result. The remaining historical backtest implements retired SMA-only rules, so it cannot validate the news/LLM strategy.

Local portfolio equity is starting capital plus locally recorded realized P&L. It does not include broker unrealized P&L, actual financing, deposits/withdrawals, or complete closing fees. The new broker-position chart and the local portfolio metrics therefore have different scopes.

The legacy backtest also caps exposure per position and deducts accumulated financing at exit rather than reflecting accrued financing in intermediate equity. Its reported drawdown is not sufficient evidence for the active system.

Repair: create a broker-derived equity and transaction ledger; reconcile fills and all costs, distinguish trading returns from cash flows, and record benchmark equity at the same timestamps. Display net return, drawdown, turnover, costs, and holding duration by strategy version. Use forward demo observation immediately; do not reintroduce a historical-evaluation execution lock.

### 8. High: headline identity and company relevance can be distorted

Evidence: `integrations.py:909–980`. The provider searches news for each screened ticker and stores one record per article URL. When a multi-company article appears in several searches, later iterations overwrite its single ticker. Search association is then treated as company relevance.

The strategy additionally uses keyword sentiment and short company aliases. Negation, mixed headlines, another company's news, and ambiguous names can produce misleading support or opposition. Repeated stories are reanalyzed without a formal distinction between a new event, new information, and continued coverage.

Repair: store articles independently from a many-to-many article/company relation, with evidence for each association. Preserve lifecycle duplicates for debugging, but give execution a configurable event/position policy. Record story age and new information as features rather than using a blanket timestamp rejection.

### 9. High: company data mixes financial statement and quote currencies

Evidence: `integrations.py:804–814`. Statement totals are combined with `info.currency`, which is the quoted-security currency; no separate financial-statement currency or ADR conversion context is passed to research.

In #353, the research summary described approximately $3.81 trillion of TSM revenue using the supplied statement total. The payload does not establish that this is a USD statement figure. Ratios can remain useful, but absolute company-size claims and cross-company comparisons are unreliable without currency provenance.

Repair: retain statement currency, quote currency, reporting period, availability date, and ADR context. Convert explicitly when required. Do not label a statement total with the quote currency by default.

### 10. Medium: daily-bar completion and data freshness need session awareness

Evidence: `strategy.py:40`. All bars whose date equals today's exchange-local date are removed, including after the exchange has closed. The app can use the previous day's indicators for several hours after a completed US session. The saved October 14 early-morning UTC examples used October 12 daily indicators while October 13 had already completed.

The submission helper refreshes quotes and ATR but reuses saved research/fundamental/trend context. It checks quote age before subsequent work, not immediately at the broker call.

Repair: use exchange session close times, including holidays and early closes; refresh relevant context before execution; attach age and provider to inputs; and enforce execution quote freshness at submission.

### 11. Medium: five named agents are not five independent trading validations

The Researcher uses one LLM; Strategist and Risk Manager are predominantly deterministic gates. Execution routes orders. Overwatcher is invoked through UI actions and applies legacy percentage-stop/target assumptions, which conflict with ATR stops. It is not an independent continuous broker monitor on the active path.

Several sidebar settings belong to retired/local simulation paths, while important active risk settings remain environment variables. The research toggle can also be outweighed by the trend-review setting when the active cycle decides whether to construct an LLM adapter.

Repair: clearly mark active versus legacy controls, centralize effective configuration, and show the resolved settings for each decision. Audit actual broker invariants and the strategy version applicable at entry, rather than checking current settings against old trades.

### 12. Medium: a new deferred state is lost by the agent chart

Evidence: `streamlit_app.py:1197–1200`. The chart recognizes deferred capacity reasons but adds `pending` to its accepted decision set instead of allowing an explicit `deferred` event. New persisted deferred events are omitted. Historical pending events with those reasons are handled differently. The isolated reproduction confirmed the omitted point.

Repair: define one lifecycle-state vocabulary and shared display mapping. Cover pending data, deferred capacity, rejected thesis, submitted order, executed fill, and reconciliation warnings consistently.

An analysis-stage pending/deferred record is also not a durable queued order. The active scheduler runs another news analysis cycle rather than draining a dedicated execution queue when capacity or data becomes available. That distinction should be explicit in the UI; any retry queue needs freshness checks and event-level deduplication.

### 13. Medium: persistence and operations are sufficient for a prototype, not unattended reliability

The database bind mount is correctly configured and excluded from image builds. Existing database migrations generally preserve rows. Two different local database paths still create opportunities to inspect the wrong history.

Only `/data` is mounted. Logs and file-based oversight reports under `/app` do not have the same persistence guarantee. The image has no healthcheck for a dead scheduler, dependencies have open-ended version constraints, and startup does not supervise both workers as a single healthy unit. Shared writable settings/order controls are available beyond the user-administration screen; deployment security needs an explicit role policy if multiple users can log in.

Repair: use one explicit database path, backup/restore procedures, durable audit storage, worker heartbeat and cycle-status records, pinned dependencies, and an account-control permission policy. SQLite remains viable for a single worker; replacing it is less urgent than preventing concurrent submissions.

## Strategy feasibility

Combining company news, business fundamentals, daily regime context, and volatility-based sizing is a reasonable hypothesis to investigate. Current evidence does not show that Finbot predicts future moves better than a baseline after costs. An LLM's self-reported confidence is not a calibrated probability of a profitable trade. The prompt also lacks a defined forecast horizon and expected move, so a broadly bullish summary is currently being used as an executable signal without a measured net reward estimate.

Leveraged holdings incur instrument/account-dependent costs. eToro documents CFD overnight charges and weekly triple overnight charges; the actual terms applicable to the account and instrument must be used rather than one generic assumption. See [eToro overnight fees](https://www.etoro.com/trading/fees/cfd-overnight-fees/) and [eToro fees](https://www.etoro.com/trading/fees/).

Demo orders establish workflow behavior and produce useful forward observations. They do not by themselves establish live execution economics or a durable edge. Measure return, costs, drawdown, and exposure over time; count research attempts separately from independent market events and positions.

## Recommended implementation sequence

1. **Reliable execution:** enforce the final fresh risk gate, unify leverage/exposure constraints, remove duplicate workers, and add durable account-level order reservations and uncertain-order resolution.
2. **Complete the position lifecycle:** manage known positions regardless of cosmetic mismatch state, implement explicit exits, ingest broker close/fill history, and reconcile protection levels against actual fills.
3. **Measure net outcomes:** unify broker account accounting, fees/financing, benchmark snapshots, and strategy-version attribution. Keep demo automation and research available while performance evidence is collected.
4. **Repair research inputs:** many-to-many company/news mapping, currency/reporting-date provenance, exchange-aware bars, and new-information/age features.
5. **Run a frozen forward experiment:** compare the current news track, a separately specified mean-reversion track, and a simple benchmark. Keep parameters and prompt/model versions fixed during each evaluation window; compare the value added by AI with a simpler baseline. Change parameters between experiments with a recorded rationale rather than to admit one rejected example.

Success criteria should include: every submitted order is traceable and risk-authorized; every broker position is represented; every closure is reconciled; reported equity agrees with the broker within a stated tolerance; costs and benchmark returns are available; and results can be attributed to a frozen strategy version. Profitability remains an empirical outcome, not an engineering acceptance assumption.

## Audit changes

This audit added this report only. Trading source logic, credentials, and persisted trading preferences were not changed. The test failure and confirmed defects are reported for a subsequent repair task.


## Repair follow-up — 14 October 2026

The implementation task following this audit addressed execution enforcement, two-direction strategy handling, measured mean reversion, broker maintenance/history, portfolio sizing/stress, concurrency, provenance, chart/permission issues and reversible cleanup. See [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md) for the exact coverage, 63-test evidence, migration rehearsal and remaining empirical/partial-fill/accounting limitations. The findings above describe the pre-repair snapshot.
