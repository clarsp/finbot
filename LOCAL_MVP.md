# Finbot operating contract

The objective is positive net returns with explicit loss and exposure limits. This implementation is a versioned **demo experiment**, not evidence of profitability. The active strategy version is `news_reaction_v2`.

## Candidate and research workflow

Yahoo Finance news and `yfscreen` discover candidates dynamically. `FINBOT_NEWS_SYMBOL_LIMIT` defaults to 40 screened companies and `FINBOT_RESEARCH_MAX_CANDIDATES` to 8 full analyses per cycle. `FINBOT_NEWS_TICKERS` is an optional explicit discovery fallback. `research_feed.json` is limited to local simulation and clearly marked sample replay tests.

Article/company associations are stored separately so a multi-company URL does not lose earlier ticker associations. Related tickers each receive candidate analysis. Provider association alone is not proof of relevance: OpenAI records direct, indirect, irrelevant, or uncertain company relevance. Irrelevant or non-directional research is rejected before subsequent gates.

Research receives supplied headlines, company financial data, recent price context, and measured post-news OHLC where available. Its output includes direction, confidence, temporary/structural/uncertain reaction assessment, rationale, and forecast horizon. Confidence is an uncalibrated model assessment. Company statement currency and quote currency are distinct; reporting-period dates and retrieval time are recorded. Original statement availability dates and ADR conversions remain unverified rather than being invented.

Completed Yahoo adjusted daily bars supply SMA50, SMA200 and ATR14. Yahoo session-close metadata determines whether today's bar is complete; if metadata is unavailable, today's bar is conservatively excluded. These indicators inform the strategy and risk review. They do not replace the news thesis.

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

Compose uses `./data/finbot.db` mounted at `/data/finbot.db`, plus `./logs`. Local Python defaults to a different `./finbot.db`. Stop services before copying SQLite files. Schema changes are additive; archival is reversible. Audit reports live beside the configured DB in `audit_logs/`.

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
