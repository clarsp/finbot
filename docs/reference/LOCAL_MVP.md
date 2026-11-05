# Finbot operating contract

The objective is positive net returns with explicit loss and exposure limits. This implementation is a versioned **demo experiment**, not evidence of profitability. The latest source strategy version is `news_reaction_v5`; see [CONTEXT.md](CONTEXT.md) for deployment state.

## Candidate and research workflow

Yahoo Finance news and `yfscreen` discover candidates dynamically. `FINBOT_NEWS_SYMBOL_LIMIT` defaults to 40 screened companies and `FINBOT_RESEARCH_MAX_CANDIDATES` to 8 full analyses per cycle. `FINBOT_NEWS_TICKERS` is an optional explicit discovery fallback. `research_feed.json` is limited to local simulation and clearly marked sample replay tests.

Article/company associations are stored separately so a multi-company URL does not lose earlier ticker associations. Related tickers each receive candidate analysis. Provider association alone is not proof of relevance: OpenAI records direct, indirect, irrelevant, or uncertain company relevance. Irrelevant or non-directional research is rejected before subsequent gates.

Before the eight-headline model limit, the Researcher ranks instrument text matches
and recency and removes duplicate titles, retaining the collected evidence for audit.
Neutral research records a reason category and can be reused for up to two hours
only when substantive evidence, market inputs and model are unchanged; reuse does
not create another entry lifecycle. New evidence, changed market context or expiry
requires fresh analysis. Target economics consistently compare estimated net reward
after costs with planned stop risk for both capped and uncapped targets.

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

The approved 30 October `news_reaction_v3` update also permits these measured
reversals in a mixed daily trend at half risk, controlled by
`strategist.allow_mixed_trend_reversals` (enabled). An established opposing trend
still requires its configured higher countertrend confidence. Direction selection
is shared by initial analysis, Strategist and final order review.

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

Costs reserve the observed spread, configured round-trip cost (10 bps), and financing estimate (8% annualized) for the holding horizon. The eToro what-if cost endpoint is used when available; explicit USD opening fees are reserved additionally. If unavailable, the configured estimate is labeled as an estimate. Estimated target payoff must cover costs and minimum reward/risk. Market slippage can exceed an intended cap: confirmed fills over either cap raise a review; discretionary closure additionally requires the conservative net-profit guard. Existing historical positions are not resized retroactively.

Each lifecycle shows the formula, per-constraint maximum units, selected quantity, limiting constraint, margin, notional, stop risk and estimated costs. Each submission snapshots rules, strategy/prompt version, model, confidence, holding policy, quote source/time, and a durable request reference.

## Submission and position management

Demo automation defaults enabled for a new database; existing user preferences are preserved. The checkbox remains on after submission. Two orders per research cycle is the default Execution limit. Every candidate is freshly reviewed at submission, including a strict `execution_ready=True` requirement and an empty execution-error list. An earlier accepted thesis cannot bypass a fresh risk failure.

Account operations share a renewable database lease. A lifecycle is atomically marked SUBMITTING with its stable request reference before sending. Acknowledgments remain SUBMITTED_PENDING_RECONCILIATION. Network/server/timeout ambiguity retains the intent for broker order lookup; it is not automatically retried. Known protection mismatches do not freeze unrelated orders. Unknown order existence and unverifiable aggregate risk remain real dependencies on new account risk.

Independent maintenance runs every minute even when no new news arrives. It reads broker positions and closed-trade history, resolves uncertain orders, updates fill-based protection, and submits exits for confirmed new-version positions when:

- The side-appropriate stop/target is observed in a current eToro quote.
- Elapsed time triggers reassessment rather than closure. Review cadence defaults to 48h directional / 24h mean reversion; forecasts can bring the first review forward.
- Later structural opposing research and confirmed sizing breaches authorize discretionary closure only through the conservative net-profit guard.

Broker-installed stops/targets remain important protection between polls. A close acknowledgment stays EXIT_PENDING; local updates alone never claim closure. Broker absence after a submitted close confirms absence with P&L still unavailable; closed-trade history supplies actual close rate, reported net profit, and fees when present. A stop hit is not claimed solely from a price crossing. Protection corrections for legacy positions preserve their recorded requested levels.

## Evaluation and accounting

Historical news-strategy evaluation remains inconclusive until point-in-time news, fundamentals, model outputs, and execution/cost evidence can be replayed without look-ahead. The retained SMA-only backtest does not validate this strategy. **No historical evaluation result gates demo automation.**

The dashboard uses recorded verified broker equity/cash snapshots when available and labels local fallback metrics. It records SPY adjusted daily benchmark snapshots, observed equity change/drawdown, and broker-reported closed net profit/fees. Closed outcomes are grouped by strategy version/framework, with saved fixture tests separate. Account equity includes manual/test activity; cash-flow-adjusted strategy returns, calibrated confidence, and statistical superiority remain unestablished. Unknown fees remain explicitly incomplete.

## Lifecycle retention

Maintenance archives never-submitted analysis hourly after **3 days** by default (`Execution → analysis_retention_days`). It includes old rejected and capacity-deferred zero-allocation analysis. It preserves broker-linked orders, persisted order references, uncertain submissions, fills, and positions. The compressed archive remains restorable for `analysis_archive_retention_days`, **7 days** by default, then expired never-submitted archives are permanently purged. Maintenance records purged row counts and compressed bytes. Restored entries receive a fresh active-retention grace period. Deletion frees reusable SQLite pages; a separately controlled compaction is required to shrink the filesystem file.

Detailed per-candidate evidence is canonical in immutable `trade_events`. The
legacy `signals` and `agent_events` operational indexes retain compact summaries
and lifecycle identifiers instead of repeating research, strategy and risk JSON.
Legacy signal-polling rows expire after `operational_history_retention_days`
(3 days by default), while the latest row per sector remains available.

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

Lifecycle lists use compact metadata, SQL pagination and on-demand evidence. Tabs and collapsed panels skip hidden content. Dashboard reads may be cached for five seconds; broker values carry the worker snapshot timestamp. Use **Refresh eToro positions** for an explicit current broker read. UI caches never authorize orders. See [the archive inventory](WORKSPACE.md) for historical performance measurements and deployment details.

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
comparison. New entries require strictly positive estimated net target profit
**after** estimated costs, including broker opening fees at final submission.
The active minimum reward/risk is zero; the measured ratio remains recorded.
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
broker stop, quantity and positive net payoff check. Missing fresh evidence,
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

### Exit policy clarification — 21 October 2026

Automatic configured stop-loss and take-profit exits remain active. Elapsed time is advisory only. Fresh opposing structural news and confirmed sizing breaches may close a position only through the conservative net-profit guard, for LONG and SHORT. Explicit user-requested closures remain supported. Losing discretionary exits are blocked. See POSITION_MANAGEMENT_UPDATE.md for costs, buffers, session restrictions and execution limitations.

## Positive-payoff entry policy — 26 October 2026

The shared demo uses `minimum_reward_risk=0`: the structure-capped target must
still provide strictly positive estimated net profit after estimated costs and
broker opening costs. A payoff below 1R is permitted. Stops, targets, monthly
breakout blocks, sizing, exposure, broker eligibility and other gates are retained.
Reward/risk is still recorded as evidence; positive payoff is not a probability
or profitability guarantee. Existing position protection is unchanged.

## eToro search exchange flag reconciliation — 30 October 2026

The search endpoint's `isExchangeOpen` remains recorded exactly as returned.
For demo entries only, an otherwise active, currently tradable supported
instrument with a false search flag can now be verified independently: a
mapped `exchange_calendars` regular session (including holidays, early closes
and lunch breaks), matching demo-account eligibility with `allowOpenPosition=true`,
and valid realtime bid/ask carrying a broker timestamp no older than 60 seconds
and no more than five seconds in the future. All three checks are required.
Unknown calendars, unavailable/ambiguous eligibility, inactive/delisted assets,
stale/undated/delayed quotes and direction restrictions retain execution blocks.
Order preflight repeats the checks and requires a non-potential direction/leverage
configuration. No old candidate is replayed automatically.

`execution_availability` records the calendar, eligibility, quote and conflict
reason separately from the raw search flag. This is a conservative Finbot
execution policy, not a claim that eToro supplies a real-time market-open flag
through eligibility. Exceptional unscheduled halts are not established by the
calendar; broker order acceptance remains authoritative. Failed submissions
retain the existing rejection/uncertain-outcome recovery handling.

The broker's v2 quote `date` is now normalized and checked; previously freshness
looked only for timestamp aliases and could overlook an old v2 quote.
Position ownership, research/strategy/risk rules and trailing configuration are
unchanged. Reference: [eToro demo eligibility](https://api-portal.etoro.com/api-reference/trading--demo/check-instrument-trading-eligibility),
[v2 rates](https://api-portal.etoro.com/api-reference/market-data/retrieve-bidask-rates-for-one-or-more-instruments).

### Automatic exchange discovery

An unfamiliar exchange now triggers discovery during instrument resolution,
even when the instrument is inactive or the session is closed. Finbot verifies
the exchange name and, when present, exchange ID against eToro's official
`GET /api/v1/market-data/exchanges` directory. Without an instrument exchange ID,
the directory must contain exactly one matching name. Exact installed calendar
names/MICs/aliases and explicit venue-name synonyms can register a calendar;
substring/fuzzy matches, generic 24/7 schedules and product-dependent calendars
cannot. Valid exchanges whose schedules are unsupported remain unresolved;
Finbot records why and retries rather than inventing trading hours.

Discovered mappings and unresolved attempts are stored in
`exchange_calendar_registry.json` beside `FINBOT_DB_PATH` in the existing volume.
Records include broker identity, calendar and library version, timezone, source,
verification time and next retry. Verified mappings refresh after 24 hours,
unresolved attempts after one hour, and library-version changes force
reverification. The process shares directory reads for 15 minutes (five minutes
on directory failure). Atomic writes and file locking preserve records across
worker/web processes and container replacement. This is derived data: it can be
rebuilt if a SQLite-only backup is restored. Without a configured database path,
discovery uses memory only and creates no host database or registry file.

Registration expands calendar coverage; it does not enable an inactive asset,
change symbol ownership, bypass USD/strategy/risk gates or submit an order.
Lifecycle instrument/session evidence includes the discovery result. See
[eToro exchanges](https://api-portal.etoro.com/api-reference/market-data/list-supported-exchanges)
and the [calendar catalog](https://github.com/gerrymanoim/exchange_calendars).

## Fresh dips, rallies and quote retries — 30 October 2026

Workflow `news_reaction_v3` uses one shared direction selector for proposals and
fresh submission. Temporary negative reactions can qualify LONG after measured
recovery; temporary positive reactions can qualify SHORT after measured reversal.
Mixed daily trends are allowed with half risk (`allow_mixed_trend_reversals=true`).
Existing 70% confidence, >1.25 ATR excursion, >=0.2 ATR recovery/turning close and
0.25 ATR stop cushion remain. Established opposing trends retain their higher
countertrend confidence. Missing reversal evidence does not authorize an entry.

`fresh_boundary_entries_enabled=true` allows directional continuation beyond a
recorded monthly/weekly boundary only when at least two completed post-event
closes and the executable entry hold beyond the boundary plus 0.1 ATR, with the
latest close progressing in the trade direction. Direction-aligned confidence
and existing portfolio checks still apply. These entries retain ATR stops and
the <=3 ATR baseline target, capped by an actual opposing level ahead when one
exists. Missing structure is distinct from an observed crossed boundary and
cannot establish breakout confirmation. Rejected target calculations never
present a take-profit on the losing side of entry; their rejection evidence
retains the unusable level. Mean reversion retains its anchor/nearest-level cap.

Reaction anchors persist by symbol and identified catalyst topic in SQLite
`reaction_anchors`, using the first recorded dated report within 24 hours rather
than the newest commentary. Later commentary cannot reset an active anchor.
After expiry, a distinct explicit event-report headline is required to replace
it. First-report time is labeled as such; actual release timing is unverified.
Measured reactions require two completed post-event 5-minute bars and the last
completed bar must be no more than 15 minutes old.

eToro quote acquisition uses the broker's timestamp and realtime type. A stale
quote (>60s) gets two further data reads, after 2s and 5s; each acquisition has
one shared budget across quote and exchange-availability checks. Missing quote,
invalid prices, missing timestamp, future timestamp, delayed/unknown type,
request failure and rate-limit failure have separate codes; none trigger stale
retries. Rate limits are surfaced for deferral without immediate extra polling.
Attempt times, ages and outcomes are retained in `quote_validation`. No broker
order is retried by this mechanism. The held-position quote path also respects
the recorded freshness result; installed broker protection remains active.

Final execution checks both retrieval age and broker quote age. If the quote
expires during cost review, one bounded fresh account/strategy/risk/cost
calculation runs under the same account lease before SUBMITTING; changed prices
therefore recalculate units, stops and targets. Another expiry blocks submission.
Ambiguous broker order results retain existing recovery and no-blind-retry rules.
Existing positions retain saved entry policies and current profit-tracker
settings; no historical rejected candidate is automatically replayed.

## Volume-supported directional qualification — 3 November 2026

New decisions use `news_reaction_v4`; v2/v3 positions retain their saved policies.
RVOL and session VWAP are deterministic optional evidence for both LONG and SHORT.
OBV remains deferred. Yahoo underlying five-minute OHLCV is kept separate from
broker execution prices. Explicit Yahoo-exchange/calendar mappings provide regular
sessions, holidays, early closes and breaks; unsupported exchanges remain unavailable.

RVOL compares cumulative completed regular-session slots with the same elapsed
slots in the last 20 usable sessions (minimum 10). Incomplete comparison sessions
are excluded; incomplete current coverage withholds evidence. VWAP is a labelled
bar approximation using typical price times volume. The current unfinished bar is
excluded. Evidence must be within its session and no more than 15 minutes old.

Research examines volume for at most 40 recent news candidates with at most four
concurrent data reads. Held-company refresh priority stays intact. Half the remaining
analysis slots (rounded up) retain news-recency priority; RVOL ranks the rest.
Unavailable volume preserves recency ordering. This adds Yahoo reads, not model
analyses or additional trading workers. Data latency and provider availability can
limit coverage. Volume observations accompany research and candidate lifecycle data.

Strategist controls:

- `volume_confirmation_enabled=true` enables conditional confidence relaxation.
- `volume_min_rvol=1.5` requires above-normal participation.
- `volume_max_vwap_distance_atr=0.75` bounds entry stretch on the proposed side.
- `volume_confidence_relaxation=0.05` allows at most five percentage points off
  ordinary directional thresholds; countertrend reductions are capped at 0.025.
  An effective threshold never drops below 0.60, and a baseline already below
  that floor is never raised.

LONG requires current entry and latest completed close at/above VWAP and an
upward last-close change; SHORT mirrors these requirements below VWAP and downward.
High RVOL alone never qualifies. Conditional mean-reversion confidence, excursion,
recovery and stop rules remain unchanged, as does reversal direction selection.
The evidence can support a measured reversal but never substitutes for its setup.

Strategy and Risk Manager use the same directional threshold helper. Risk Manager
retains half allocation for mixed/opposing/unavailable trends and all original
portfolio, cost, protection, broker and execution gates. No extra leverage or
position size is authorized. Missing/invalid/stale volume restores baseline rules.
Final submission reloads volume and recomputes qualification; saved approval cannot
bypass a missing fresh confirmation. Gate evidence includes baseline/effective
thresholds, baseline acceptance and whether relaxation actually admitted the candidate;
Risk Manager records confirmation and its unchanged budget multiplier. These are
experimental qualification rules, not calibrated confidence or proven profitability.

## Recent price direction — 3 November 2026

`news_reaction_v5` adds `recent_price_trend_v1` from the **existing completed daily
bars**; no extra provider or history request is added. The daily history read now
precedes AI research. Research receives compact SMA/ATR/recent-direction evidence,
not the full multiyear support/resistance array. The research prompt version is
`news_market_horizon_v3`, invalidating unchanged-neutral reuse from older prompts.

The existing SMA `trend_state` remains unchanged for historical comparisons,
position management and conservative allocation. A separate `recent_trend` records:

- Seven- and fourteen-trading-session close returns and displacement in daily ATR.
- Five-session closing average and its change over five sessions in ATR units.
- High/low ranges of the last five sessions versus the preceding five: both higher
  means bullish structure; both lower means bearish; overlap/conflict means mixed.
- The last five completed weekly closes, current/prior four-week average, and
  weekly direction. Weekly confirmation requires last close > current average >
  prior average for bullish, mirrored for bearish. A Friday resampling label later
  than the last completed daily date is excluded, including conservatively waiting
  on holiday-shortened weeks. The first sampled week is excluded conservatively
  because its coverage can start partway through the week. A weekly high is never substituted for a close.

At least 20 valid chronological daily sessions and a positive ATR14 are required.
Recent direction is bullish/bearish when **both 7/14-session displacements reach
0.5 ATR in that direction**, the five-session-average change agrees, and either
recent high/low ranges or completed weekly direction confirms. Otherwise it is
mixed/unavailable, explicitly recorded rather than forcing a direction. This is
an experimental deterministic classification, not a calibrated probability.

With `strategist.recent_trend_enabled=true` (default), an available directional
recent trend determines LONG/SHORT bias for otherwise directional, eligible news
research. Neutral/irrelevant research still cannot authorize entry. Fundamental
metrics remain visible but neither lower nor raise confidence thresholds on this
path; original confidence is retained as an uncalibrated research-quality
prerequisite, never converted into a probability for the opposite trade direction.

If news opposes the recent bias, fresh direction-aligned RVOL/VWAP and completed-bar
follow-through are required before entry. This timing check uses the existing
volume thresholds independently of whether optional volume *confidence relaxation*
is enabled. A countertrend entry requires temporary-reaction classification, the
existing measured mean-reversion setup and the original side-specific countertrend
confidence threshold (no volume discount). High confidence or company quality
alone cannot authorize reversal. Strategy and Risk Manager share these guards.

Missing/mixed recent direction or a disabled control retains the prior news/SMA
rules with an explicit fallback message. The control is exposed through existing
GUI metadata, reporting and validated Telegram settings. This fallback does not
invent a recent trend or make missing optional evidence an execution failure.

Normal trend-following entries use `RECENT_TREND_CONTINUATION`, with existing ATR
protection, structure targets, cost/quote/broker eligibility and prospective shadow
tracking. Confirmed boundary-breakout and measured-reversal frameworks retain
their own entry requirements. Allocation retains the existing SMA-based multiplier;
an entry against recent direction is additionally capped at half allocation. Recent
analysis never raises the budget solely by reclassifying an old mixed/opposing SMA
state. Final submission reloads daily history and recomputes direction and entry
guards. New decision messages and policies retain recent evidence and direction
source. Existing v2/v3/v4 positions retain their saved policies and remain managed.

Offline META #2052 regression: saved SMA ordering remains mixed, while recent
price direction is bearish (7-session -12.87%, 14-session -13.38%). SHORT becomes
the bias; the original bullish thesis cannot authorize LONG without reversal
confirmation, nor SHORT without fresh opposing-news entry confirmation. The saved
historical lifecycle is not changed or replayed into a broker order.

### Strategist automatic confirmation — 3 November 2026

`strategist.trend_auto_confirm_enabled=true` is the **Auto-confirm trend entries**
checkbox. It automatically accepts a *measured* RVOL/VWAP follow-through or measured
countertrend reversal when required by recent-trend entry rules. Confidence, risk,
cost, fresh broker quote and execution checks still apply. Missing/stale/failed
measurements remain unconfirmed; demo mode does not fabricate confirmation.

OFF holds recent-trend entries needing confirmation for review, including requests
through the manual submission boundary. It does not bypass the measurements or
provide a manual approval override. Ordinary aligned entries needing no additional
confirmation retain their existing rules. Normal research cycles reassess evidence;
there is no standing broker order or replay of an older rejected candidate.

Strategy/Risk decision messages and snapshots identify auto-confirm ON/OFF and
`confirmed`, `awaiting_measurement`, `automatic_confirmation_disabled` or
`not_required`. The shared guard rechecks the current setting at final submission.
GUI and validated Telegram controls can change it later.
