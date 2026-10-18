# Finbot operating contract

The objective is positive net returns with explicit loss and exposure limits. This implementation is a versioned **demo experiment**, not evidence of profitability. The active strategy version is `news_reaction_v2`.

## Candidate and research workflow

Yahoo Finance news and `yfscreen` discover candidates dynamically. `FINBOT_NEWS_SYMBOL_LIMIT` defaults to 40 screened companies and `FINBOT_RESEARCH_MAX_CANDIDATES` to 8 full analyses per cycle. `FINBOT_NEWS_TICKERS` is an optional explicit discovery fallback. `research_feed.json` is limited to local simulation and clearly marked sample replay tests.

Article/company associations are stored separately so a multi-company URL does not lose earlier ticker associations. Related tickers each receive candidate analysis. Provider association alone is not proof of relevance: OpenAI records direct, indirect, irrelevant, or uncertain company relevance. Irrelevant or non-directional research is rejected before subsequent gates.

Research receives supplied headlines, company financial data, recent price context, and measured post-news OHLC where available. Its output includes direction, confidence, temporary/structural/uncertain reaction assessment, rationale, and forecast horizon. Confidence is an uncalibrated model assessment. Company statement currency and quote currency are distinct; reporting-period dates and retrieval time are recorded. Original statement availability dates and ADR conversions remain unverified rather than being invented.

Completed Yahoo adjusted daily bars supply SMA50, SMA200 and ATR14. Yahoo session-close metadata determines whether today's bar is complete; if metadata is unavailable, today's bar is conservatively excluded. These indicators inform the strategy and risk review. They do not replace the news thesis.

Each news candidate also carries exchange-aware session evidence. The Researcher resolves the primary exchange from eToro instrument metadata with Yahoo exchange/timezone metadata as supporting evidence, converts the observation into the exchange's IANA timezone, and labels opening discovery, morning, midday, afternoon, closing, a local lunch break, pre-market, after-hours or closed. Provider session timestamps override normal hours when they identify the current session, including early closes; otherwise the record explicitly says that only the weekday schedule was available. The Strategist, Risk Manager and Execution records retain this context, and the final submission performs a fresh session calculation. Missing exchange metadata does not reject research or independently authorize/reject a trade.

The underlying exchange state and eToro state are stored separately. An available eToro quote outside the underlying regular session is not represented as an open underlying market. The broker remains the authority on whether an order can actually be submitted. Session and weekday are initially observational variables: they do not create a direction or change ATR targets until accumulated results demonstrate a net benefit after costs.

## Two-direction strategies

**Directional news:** bullish research proposes LONG, bearish research proposes SHORT. Structural or uncertain news is not automatically reversed just because the daily trend disagrees. Existing confidence and company-evidence gates apply. Opposing daily trends require the configured countertrend confidence and receive half the normal risk budget; mixed/unavailable trends receive half risk. Missing optional data does not masquerade as a research rejection.

Directional entries use a stop 2×ATR14 from entry and a target 1.5 times that stop distance by default. LONG stop/target are below/above entry; SHORT stop/target are above/below entry. After the actual fill is reported, these offsets are applied to the fill and broker protection is updated when necessary. A PATCH acknowledgment is not confirmation that the levels changed.

**Conditional mean reversion:** only research explicitly identifying a temporary reaction can propose the opposite-news track:

- Bullish daily trend + temporary bearish news: LONG after a measured dip and recovery.
- Bearish daily trend + temporary bullish news: SHORT after a measured burst and reversal.

The reference is the completed five-minute close immediately before the news timestamp. At least two completed post-news bars are required. The excursion from that anchor must exceed 1.25×ATR14 by default; recovery from the extreme must reach 0.2×ATR14, and the latest completed close must turn in the intended direction. Research confidence must reach 70%. The target is the pre-news anchor; the stop is 0.25×ATR14 beyond the reaction extreme. The actual entry must retain a favorable stop/target and pass the normal cost/reward and risk gates. Failed conditions reject this setup. This replaces the earlier SMA-distance-only interpretation; old decisions retain their stored evidence.

Notes carry `STRATEGY: CONDITIONAL_MEAN_REVERSION; ENTRY_ZONE: ... to ...; SOURCE_ATR: ...` for downstream agents. All thresholds are agent settings.

## Order eligibility and sizing

eToro must confirm a tradeable USD stock/ETF, the proposed direction, account equity/cash, complete positions, and a fresh eToro bid/ask quote. LONG uses ask; SHORT uses bid. Yahoo never silently substitutes for an execution quote. Unavailable execution inputs block that order while research can continue.

The Risk Manager calculates units as the smallest capacity permitted by:

1. Per-trade risk budget divided by entry-to-stop distance.
2. Remaining aggregate marked stop-risk capacity.
3. `max_order_units` (default **5 units**).
4. `max_order_spend_usd` (default **$200 capital/margin**, with a cost reserve).
5. Available cash/margin and remaining total gross exposure.
6. Single-position gross exposure and portfolio stop-plus-gap stress capacity.
7. Sector concentration when explicitly enabled and all sector labels are available.

Risk defaults: 0.5% equity per trade, 2% aggregate nominal stop risk, 100% total gross exposure, 10% single-position exposure, 30 open positions (or `FINBOT_MAX_OPEN_POSITIONS`), and leverage 2. The extra stress scenario assumes a 5% adverse gap and permits at most 10% equity stressed loss. These are configurable scenarios, not guarantees against larger losses. Sector checking is off until complete sector labels are supplied; enabling it makes missing labels an explicit execution dependency.

Costs reserve the observed spread, configured round-trip cost (10 bps), and financing estimate (8% annualized) for the holding horizon. The eToro what-if cost endpoint is used when available; explicit USD opening fees are reserved additionally. If unavailable, the configured estimate is labeled as an estimate. Estimated target payoff must cover costs and minimum reward/risk. Market slippage can exceed an intended cap: confirmed fills over either cap trigger a broker close request. Existing historical positions are not resized retroactively.

Each lifecycle shows the formula, per-constraint maximum units, selected quantity, limiting constraint, margin, notional, stop risk and estimated costs. Each submission snapshots rules, strategy/prompt version, model, confidence, holding policy, quote source/time, and a durable request reference.

## Submission and position management

Demo automation defaults enabled for a new database; existing user preferences are preserved. The checkbox remains on after submission. Two orders per research cycle is the default Execution limit. Every candidate is freshly reviewed at submission, including a strict `execution_ready=True` requirement and an empty execution-error list. An earlier accepted thesis cannot bypass a fresh risk failure.

Account operations share a renewable database lease. A lifecycle is atomically marked SUBMITTING with its stable request reference before sending. Acknowledgments remain SUBMITTED_PENDING_RECONCILIATION. Network/server/timeout ambiguity retains the intent for broker order lookup; it is not automatically retried. Known protection mismatches do not freeze unrelated orders. Unknown order existence and unverifiable aggregate risk remain real dependencies on new account risk.

Independent maintenance runs every minute even when no new news arrives. It reads broker positions and closed-trade history, resolves uncertain orders, updates fill-based protection, and submits exits for confirmed new-version positions when:

- The side-appropriate stop/target is observed in a current eToro quote.
- Maximum holding time expires (48h directional / 24h mean reversion by default, shortened by a supplied research forecast horizon).
- Later high-confidence structural research invalidates the position thesis.
- Confirmed fill units or capital exceed entry-time caps.

Broker-installed stops/targets remain important protection between polls. A close acknowledgment stays EXIT_PENDING; local updates alone never claim closure. Broker absence after a submitted close confirms absence with P&L still unavailable; closed-trade history supplies actual close rate, reported net profit, and fees when present. A stop hit is not claimed solely from a price crossing. Protection corrections for legacy positions preserve their recorded requested levels.

## Evaluation and accounting

Historical news-strategy evaluation remains inconclusive until point-in-time news, fundamentals, model outputs, and execution/cost evidence can be replayed without look-ahead. The retained SMA-only backtest does not validate this strategy. **No historical evaluation result gates demo automation.**

The dashboard uses recorded verified broker equity/cash snapshots when available and labels local fallback metrics. It records SPY adjusted daily benchmark snapshots, observed equity change/drawdown, and broker-reported closed net profit/fees. Closed outcomes are grouped by strategy version/framework, with saved fixture tests separate. Account equity includes manual/test activity; cash-flow-adjusted strategy returns, calibrated confidence, and statistical superiority remain unestablished. Unknown fees remain explicitly incomplete.

## Lifecycle retention

Maintenance archives never-submitted analysis hourly after **3 days** by default (`Execution → analysis_retention_days`). It includes old rejected and capacity-deferred zero-allocation analysis. It preserves broker-linked orders, persisted order references, uncertain submissions, fills, and positions. The archive stores compressed lifecycle and event records in the mounted SQLite database. It cleans active dashboard history, not the entire physical database. Administrators can archive now or restore an ID; restored entries receive a fresh retention grace period. Archives are not permanently purged automatically.

## Operation and test workflow

Compose uses Docker-managed `finbot_data` mounted at `/data`, plus `./logs`. Local Python defaults to a different `./finbot.db`. Run all live SQLite access inside Docker and export consistent backup snapshots for host-side inspection; do not share the live WAL database between macOS and Docker Linux. Schema changes are additive; archival is reversible. Audit reports live beside the configured DB in `audit_logs/`.

Hosted single-replica deployments can enable consistent S3 snapshots and
startup restoration. The live SQLite file stays on local or persistent block
storage; S3 is never used as SQLite's active filesystem. See `HOSTING.md`.

Optional trade email notifications are sent only after eToro acknowledges a
demo submission. They summarize Researcher, Strategist, Risk Manager and
Execution decisions and may attach the complete sanitized lifecycle evidence.
SMTP supports STARTTLS, implicit TLS, authentication, and multiple recipients.

One supervised scheduler runs alongside Streamlit. A heartbeat and HTTP healthcheck expose a stalled worker; required task failures stop the supervisor so Docker's restart policy can recover. Container health status alone does not restart a container.

Initial account creation requires `FINBOT_PASSWORD`; there is no built-in password fallback. Administrators control order actions/settings; non-admin dashboard access is read-only.

**Research Actions → Test purchase workflow** replays a saved bullish research fixture with a $1–$25 stake. It creates a normal lifecycle and uses the shared fresh Strategist/Risk/Execution submission boundary, position caps, durable references, reconciliation and management. It does not bypass safety gates or invent a trade when the fixture is ineligible. Test orders are marked separately for strategy attribution.

All adapter requests use the configured `ETORO_USER_AGENT`. API debug logging remains toggleable and masks credentials; lifecycle evidence contains relevant request/response logs. Live eToro orders are blocked and credentials are unchanged by this implementation.

## Dashboard loading

Lifecycle lists use compact metadata, SQL pagination and on-demand evidence. Tabs and collapsed panels skip hidden content. Dashboard reads may be cached for five seconds; broker values carry the worker snapshot timestamp. Use **Refresh eToro positions** for an explicit current broker read. UI caches never authorize orders. See [DASHBOARD_PERFORMANCE.md](DASHBOARD_PERFORMANCE.md) for measurements and deployment details.

## Support/resistance target experiment

New entries use `daily_weekly_monthly_v2` with up to five years of completed
adjusted daily bars. Daily swing zones use 120 sessions, pivots confirmed by
two later bars, and at least two pivots separated by five bars within 0.25 ATR.
Weekly highs/lows (104 periods) and monthly highs/lows (60 periods) provide
additional potential barriers. Current-period extremes use completed daily
bars only; their evolving status and source-through date are recorded rather
than presented as confirmed swing resistance/support. Directional production
uses the outer monthly range: the highest high for LONG or lowest low for SHORT
from the current month plus one preceding calendar month by default. Missing
monthly data falls back to the outer four-week range; missing dated range data
falls back to the earlier nearest-level rule. The selected boundary caps the
initial ATR target with a 0.1 daily ATR buffer; it never extends that initial
target. Nearby levels remain recorded breakout obstacles, not automatic ceilings.
A quote already beyond the monthly boundary does not silently authorize chasing
a new high/low; normal directional and net-return checks apply. These levels
are historical evidence, not a prediction of a successful breakout. Intraday
structure is not yet included. Mean reversion retains its anchor and nearest cap.
The nearest-level production mode remains selectable in the Risk Manager.

Missing structure retains the baseline with unavailable evidence.
`Risk Manager → structure_targets_enabled` defaults true; disable for baseline
comparison. New entries with adjusted targets require minimum reward/risk
**after** estimated costs, including broker opening fees at final submission.
Leverage does not relax this gate. Stops retain ATR/reaction rules and are not
tightened to manufacture a passing ratio. Absolute structure targets do not
shift with fills. Lifecycle protection and policy evidence retain the baseline,
selected target, timeframe, zone, method, and observation date.

`scripts/recompute_structure_targets.py` previews linked open demo positions;
`--trade-id ID` limits scope, and `--apply` submits eligible reviewed targets under
the shared account lease. Existing-position management preserves broker stops,
requires a fresh quote, clearance beyond the current executable price, and
positive estimated net profit from actual entry. Original entry reward/risk is
reported but does not veto profit taking on an existing position. No positions
are closed when the proposed target has already been passed or is too close.
Reports are stored beside the configured database in `audit_logs/`.

## Position economics and broker close requests

Expanded lifecycles show invested margin, opening/current market exposure,
profit per unit, gross and estimated net position P/L at target and stop, and
estimated return on margin. Actual broker units are the full exposure; no
additional leverage multiplier is applied. Broker running P/L, saved cost
forecasts, and confirmed realised profit are displayed separately, with
observation timestamps and missing-data labels. Saved costs scale to the
current quantity when their original sizing quantity is available; unknown
costs do not become zero. Legacy positions without saved cost forecasts retain
gross scenarios with net outcomes explicitly unavailable. New submissions
save spread, round-trip bps, financing rate and horizon assumptions. Closing
fees, duration changes and slippage can differ from the saved estimate.

The demo close endpoint requires `InstrumentID` as well as the position ID.
Maintenance uses the verified broker instrument; direct close calls resolve
it from a unique open portfolio position when necessary. The returned
`orderForClose.orderID` is retained in lifecycle evidence. Acknowledgment
stays EXIT_PENDING until broker reconciliation confirms absence/closure.
Live close routing remains blocked.

An additional minimum dollar-profit floor remains undecided. Existing
entry cost/reward/risk gates and positive-profit requirements for discretionary
target reductions are unchanged while that policy is discussed.

## Prospective shadow exit tracking

Risk Manager freezes an optional shadow plan only at the shared final submission
boundary, after normal research, strategy, account, cost and risk checks. Broker
reconciliation starts comparisons only once a new directional fill has confirmed
price, units and opening time. Previously opened positions and conditional mean
reversion are not retrospectively enrolled.

Defaults compare the production price/time exit baseline with 2-ATR and 3-ATR
stops paired with 1-ATR and 2-ATR targets. Each uses the production holding limit
plus 168/336 calendar hours. Alternative targets retain the earlier nearest-level cap using saved entry-time
daily, weekly and monthly structure, including the 1 ATR shadow; wider stops reduce simulated quantity to preserve
initial stop risk without exceeding the approved/fill quantity. Alternative net
reward/risk eligibility is shown, not used to bypass the real entry gates.

The Position Manager maintenance loop observes eToro bid for LONG and ask for
SHORT, including after the actual position closes. Rows persist in SQLite and
resume after restart. No additional orders or protection changes are sent by the
shadow observer. The baseline freezes initial price/time rules; later thesis
invalidation, manual actions and actual broker fills are not replayed.

The Shadow comparison view and lifecycle details show ATR14, requested multiples,
uncapped ATR prices, structure-capped prices, estimated outcomes, observation gaps
and paired completed results. Sampled quote crossings do not prove first touches
or executable fills. Gaps over 120 seconds and late horizon exits remain visible.
Spread is already included in executable quotes; other costs and financing are
estimates, with missing broker opening costs explicitly identified.

Directional initial target distance is now explicitly bounded by Risk Manager's
`max_target_atr_multiple` (default 3). Its formula is
`min(stop_atr_multiple * target_reward_risk, max_target_atr_multiple)` before
structure caps. The default 2-ATR stop and 1.5R target remain unchanged. This
ceiling applies to new directional sizing, not existing orders or news-anchor
mean-reversion targets.

Companies analytics read retained lifecycle and research evidence directly,
separating research acceptance from broker submission. Prior analysis references
appear inside a lifecycle. A duplicate submission label requires broker-submitted
records for the same symbol and direction, plus a shared broker reference or
an overlapping recorded position lifetime. A later re-entry after closure is
not a duplicate.

Open-position target reviews run through Risk Manager and the shared demo
protection update/reconciliation path under the account lease. They may extend a
previously shortened target within the ATR/monthly boundary while preserving the
broker stop, quantity and positive net payoff check. Expired holding limits,
missing data, crossed stops, or insufficient clearance are reported as skips.
Saved shadow plans and their initial baselines are immutable across these reviews.

## Tracked execution recovery

The normal maintenance workflow persists close/protection intent and recovery state in SQLite. Restarting retains attempts and next-check times. Historical pending requests are adopted without sending them again. Uncertain entry lookups and close-status checks back off; unknown close outcomes require evidence rather than a blind replacement. Broker-confirmed terminal close failures permit bounded replacement against the verified remaining position. Protection correction uses existing portfolio snapshots, spaced mismatches and identical fixed-price updates, with at most three writes by default. Oversight and each trade show pending actions, next checks, evidence and attention flags. No shared API budget or separate recovery agent bypasses the normal workflow.

## Six-role architecture — 18 October 2026

Researcher → Strategist → Risk Manager → Execution is the entry path. Execution performs final checks, durable submission and broker acknowledgment. **Position Manager** owns fill confirmation, held-position monitoring, protection, reconciliation, controlled recovery, exits and quote-observed shadow tracking. It uses the existing renewable account lease, adapter and persisted action state; it does not create a second scheduler or repeat orders during migration.

Existing broker-linked active positions are adopted idempotently into `position_management`. Their policies, stop/target levels, holding limits, historical event authors and pending broker-action IDs, attempts and next-check times are preserved. New verified fills receive ownership metadata automatically. Historical trades remain available to analysis; shadow plans are never fabricated retrospectively.

**Performance Analyst** is an advisory, deterministic evidence-review agent. The worker checks locally every 15 minutes and saves one immutable review of the previous completed Stockholm calendar day and previous completed Monday–Monday week. On first start/resume it reviews the latest completed periods; cumulative broker-confirmed results include earlier history. Frozen reviews record available evidence; late broker history is reflected in later reports. It adds no broker/LLM polling and cannot change rules or submit orders.

Reports distinguish broker-reported realized net P/L from current entry exposure, count each broker position once, exclude unknown net P/L and show fees without subtracting them twice. Results are grouped by direction, symbol and captured-policy fingerprint; legacy attribution remains incomplete. Same-trade/same-horizon completed shadow pairs use estimated net costs and disclose observation gaps. The default 20-closure review threshold is a minimum reporting guard, not statistical proof of profitability. Recommendations stay advisory.

Dashboards expose six agent roles, a five-role per-trade overview (Performance Analyst is portfolio-level), optional expanded Position Manager stages, a Position management view and a Performance reviews view. Existing trade details retain full historical events.

## Weekend scheduler pacing — 18 October 2026

Research Actions now separates weekend workload from permission to open new positions. Defaults: weekend pacing enabled; new weekend entries disabled; research every 360 minutes; news every 360 minutes; routine Position Manager checks every 15 minutes. Weekday intervals remain as configured. Effective weekend research/news intervals never run faster than their normal intervals.

Saturday/Sunday are determined using the configurable IANA scheduler timezone (Europe/Stockholm by default). This is a calendar pacing policy, not an exchange holiday calendar or a claim that all international markets close simultaneously. Existing exchange-session evidence, broker availability, freshness and risk gates still apply. Enabling weekend entries grants no exemption from those gates. Entry policy is enforced both during Execution review and at the final submission workflow, including manual demo entries. Protection and exits are never blocked by this switch.

Uncertain entries/closes and outstanding protection actions retain 60-second maintenance checks; ordinary acknowledged pending closes use slower weekend maintenance. Durable action-specific next-check times, Retry-After and write limits still apply. The worker heartbeat and daily/weekly advisory reviews continue independently. The worker wakes locally each minute to respond to urgent state, changed configuration and weekday transitions; it does not perform a broker poll on every wake during routine weekend pacing.

News-attempt times and research claims persist across restarts. The scheduler resumes normal weekday cadence without waiting for a weekend interval to finish. Existing automatic/manual mode and suspension controls are preserved. Tests cover timezone boundaries, weekend spacing, restart deduplication, Monday resumption, entry controls and urgent maintenance. **181 tests pass**.

## Consolidated review tools — 18 October 2026

Position Manager now owns configuration audits. The old Overwatcher implementation is a compatibility helper (`TradeConfigurationAuditor`), not a seventh registered agent. A local audit runs at most once per 24 hours during successful maintenance, with an on-demand control under Position management. Its explicit scope is the latest 50 eligible trades. Reports persist in SQLite; existing JSONL audit files remain viewable under Legacy configuration audit reports. These checks do not query the broker or modify positions.

Performance Analyst owns on-demand historical path analysis and the batch tool formerly on Trade lifecycles. Performance reviews → Historical path analysis provides access to requests, saved analyses and the historical path chart. Individual trades retain an Analyze historical path shortcut. New records retain their reference prices/timestamp and distinguish broker-position references, simulated references and unexecuted counterfactuals. They do not confirm fills or represent realized returns.

New scheduled performance reviews incorporate the latest saved configuration-audit snapshot and historical analyses generated during their window. Existing immutable reviews and historical event authors are preserved. No scheduled historical-price fetching is added. The duplicate Trade Oversight sidebar action and separate Oversight navigation page are removed; recovery evidence lives under Position management. Analysis archive controls remain separate administrative tools. **187 regression tests pass**.

## Shared profiles and login permissions — 18 October 2026

The active trading runtime is now named **Finbot shared demo**. User memberships grant viewer, operator or owner access independently of platform-admin status. Existing admins retain shared-profile owner membership; new normal users default to viewers, with an explicit shared-access selection during registration. Platform admins can suspend/reactivate accounts; suspension revokes sessions without stopping the shared trading workflow. The last active platform admin and last active profile owner are protected.

The active profile is visible in the dashboard. All signed-in users can manage their own login, create inactive draft profile/account/connection metadata, and share owned profiles. Broker/SMTP/AI credentials remain platform-admin-only. Backend action checks and guarded cached reads enforce roles, account suspension and profile access. Existing trading records are tagged to profile 1 without changing their policies or broker actions.

Only the original shared profile can run or display trading data. Draft profiles are deliberately blocked from independent trading and from showing the shared profile's data as their own. Full broker/account isolation, additional adapters, per-profile workers and secret storage remain parked. [MULTITENANCY.md](MULTITENANCY.md) records the implemented boundary and contains a copy/paste prompt for that uplift.
