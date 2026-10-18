# Finbot application and trading-viability audit

**Audit date:** 17 October 2026. **Decision:** suitable for a closely monitored eToro demo experiment; positive net expectancy is unproven; not cleared for live capital or unattended production operation.

This report replaces the earlier audit. It assesses the current working tree and running application, rather than treating earlier defects as still present or assuming every remaining concern has been repaired. [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md) records verified capabilities and the remediation backlog.


## Broker recovery follow-up — 18 October 2026

The A1/A2 execution-recovery gaps now have durable `broker_actions` tracking in the existing Execution workflow. Intent is committed before writes; acknowledgments remain pending until broker evidence confirms the result. Existing pending closes are imported without resubmission. Uncertain closes are not blindly repeated: replacement requires a confirmed terminal failure and a fresh remaining position. Undocumented numeric broker statuses are retained without guessing their meaning.

Protection recovery reuses portfolio snapshots, requires spaced mismatches, reapplies only the same fixed levels, and refuses to loosen a tighter broker stop automatically. Default maximum is three write attempts. Each action persists its next check with backoff and jitter; HTTP Retry-After is respected, and weekend close/entry checks slow down. There is no shared API budget. Unresolved actions appear in Oversight and trade details with one attention event. Partial execution requires portfolio convergence; it does not establish full closure or realized P/L.

The regression suite now passes **156 tests**, including timeouts, restarts, duplicate requests, partial execution, terminal rejection, protection convergence and broker waiting periods. This addresses the implementation gaps in A1/A2; it does not prove every existing queued close has executed. The original audit snapshot below remains historical. S3 restore safety, profitability evidence, accounting and the other audit findings remain separate work.

## Scope and evidence

Reviewed research, strategy, sizing, order submission, maintenance, broker reconciliation, target selection, shadow observation, accounting, SMTP, authentication, Docker supervision, SQLite and S3 recovery. The audit used:

- Current source, README, LOCAL_MVP, CONFIGURATION, HOSTING, tests and historical experiment artifacts.
- **125 passing regression tests**, rerun during this audit, using temporary databases and mocked external providers.
- Read-only inspection of the live database **inside Docker**, `PRAGMA quick_check = ok`, the current eToro demo account/portfolio and broker rate responses. The live evidence snapshot began at **14:47 CEST / 12:47 UTC**.
- Isolated reproductions of three control gaps using temporary databases/mocked brokers, and one S3 recovery reproduction using disposable databases and a fake S3 client inside Docker.
- Running container `finbot-finbot-1`, image `finbot-finbot`, image SHA-256 `506806e6a90c813d1c40450b0f04cf594295c2d0f9396040e7f944c73aa71f94`, healthy. Repository HEAD is `d438d73ad31fe3b69c89c924c32791fe6b4de8be`; substantial implemented changes remain uncommitted, so HEAD alone does not identify the deployed strategy.

No orders, closes, protection updates, emails, configuration changes or deployment were performed by this audit. The only requested repository changes are the two replacement reports. Prior broker-confirmed protection updates were inspected as existing evidence. This is not an independent financial-statement audit, penetration test or complete historical news replay.

## Overall assessment

Finbot now has a credible demo execution foundation. Fresh deterministic gates, explicit LONG/SHORT handling, portfolio sizing, durable submission intent, broker-confirmed fills/protection, real closure history and independent maintenance are substantial improvements over an agent demonstration that merely produces trade suggestions. These capabilities make controlled forward testing worthwhile.

Its weakest area is the evidence connecting research to profitable outcomes. The application can implement a thesis consistently without that thesis having an edge. ATR, SMA alignment, LLM confidence and historical range boundaries are inputs to a hypothesis; none establishes a probability of reaching the target before the stop within the allowed holding time.

| Dimension | Assessment |
|---|---|
| Research and strategy architecture | Plausible, inspectable hypothesis generator; predictive value unmeasured. |
| Controlled demo execution | Implemented and exercised against the actual demo broker. |
| Unattended operational reliability | Incomplete: protection convergence, pending-close handling and meaningful health alerts need work. |
| Portfolio controls | Useful limits for new entries; correlation, daily loss control and inherited-position treatment remain incomplete. |
| Performance measurement | Broker observations exist; cash-flow-adjusted strategy returns and isolated policy cohorts do not. |
| Shadow experiment | Implemented prospectively; **zero shadow rows at the audit snapshot**. |
| Hosting/recovery | Single-writer local deployment is working; off-site recovery is disabled and its automatic restore policy has a reproduced flaw. |
| Live-capital readiness | Not established; live eToro order routing remains blocked. |

## What the observed results actually say

The active database contains 362 lifecycle rows, 428 archived analyses, 6,000 lifecycle events, 1,343 broker snapshots, seven stored closed broker positions and zero shadow comparisons. Lifecycle counts are not order counts. Local `OPEN` also includes two non-broker legacy/simulation rows.

The broker returned **19 actual open positions**, all linked to local lifecycles: nine normal open broker positions and ten positions whose close requests remain pending. Effective settings enable AI research, hourly automatic research, demo orders and both directions. Execution currently permits **three orders per cycle**, five units and $200 estimated capital per new order. The three-order value is a persisted preference, not the two-order source default.

Seven broker-history-confirmed closures have a combined reported net P/L of **-$1,048.72**, with one win and six losses:

| Recorded cohort | Closed trades | Reported net P/L |
|---|---:|---:|
| Legacy, no current strategy version | 3 | -$989.69 |
| Legacy protection policy | 3 | -$36.90 |
| `news_reaction_v2`, directional | 1 | -$22.13 |

This is an unfavorable descriptive record, but it is too small and heterogeneous to estimate the expectancy of today's policy. It is also not evidence that today's monthly-range rule has failed: the six range-target records are **retargeted existing positions**, not six new entries under a frozen prospective policy. No prospective shadow-plan entry was present in the broker-order cohort, and no completed new-policy cohort was available.

The portfolio reported approximately **$578.21 unrealized P/L**. MA alone contributed **$383.65**, about two-thirds, and used **$7,908.86** of approximately **$10,884.20** recorded position margin. This inherited position exceeds today's new-entry caps and dominates the result. Its contribution must not be attributed to the small, recently capped automated trades. The latest stored snapshot implies roughly 21% gross exposure/equity and 1.33% mark-to-stop risk/equity, but some pending-close marks fall back to entry prices; these are approximate snapshot metrics, not audited live risk or a loss guarantee.

Account equity was approximately $105,436.29. It is not a cash-flow-adjusted return for Finbot. Initial balances, manual activity, policy changes, deposits/withdrawals and complete account costs must be reconciled before making that claim.

The earlier paired replay covered 26 fills, with only eight completed, coverage-filtered ATR-control pairs. Dollar outcomes were negative across those control policies, while some risk-normalized means were positive; legacy sizing explains why these measures can disagree. This exploratory replay cannot select a winner, and it predates today's monthly-range production rule. See [the historical findings](data/experiments/PAIRED_EXIT_FINDINGS_20261017.md).

## Trading assessment

### Entry quality is still the central unproven hypothesis

The workflow combines news discovery, company fundamentals, an LLM assessment, SMA context and measured reactions. This is a sensible way to organize evidence. It has not established that the chosen news is new information, that the market has not already priced it in, or that the research score predicts subsequent executable returns.

An LLM's 80% confidence is not an observed 80% trade-success probability. The application needs calibration by framework, direction and horizon, plus comparisons with simpler eligible-entry baselines. Fundamentals may support a company thesis while being weak timing evidence for a 24–48-hour leveraged trade. Hourly research and one-minute maintenance also make this a short-horizon position system, not a low-latency news-arbitrage system.

### The monthly target rule is implemented, but remains a trading experiment

Production directional targets start from `min(stop ATR × target reward/risk, maximum target ATR)`; current defaults produce a maximum 3-ATR initial target from a 2-ATR stop. LONG then uses the outer monthly high and SHORT the outer monthly low, buffered inward by 0.1 current ATR. The window is the current month plus one preceding calendar month. Weekly range is a fallback; nearest-level selection is a further fallback when dated ranges are unavailable. See `price_structure.range_boundary_target` and `trading_workflow.size_order`.

Allowing price to pass a nearer obstacle addresses the earlier excessively close cap. It also assumes the nearer obstacle will break. **No separate quantitative breakout-strength or volume-confirmation gate was added.** Existing strategy approval is the assumption, and crossed obstacles are recorded. This is the authorized demo hypothesis, not a demonstrated improvement. A monthly high/low is not a guaranteed reversal, fair value or probability-weighted destination. The rule is symmetric for LONG/SHORT and does not extend the initial ATR target.

The original stop remains part of the risk decision. Holding a wider stop can avoid some premature exits, but also increases the average loss unless quantity is reduced. It cannot be justified only by noticing trades that eventually reached a nearby profit level.

### Leverage does not repair negative expectancy

For a simplified trade with a fixed target and stop, before costs and time exits:

| Stop / target | Break-even win probability |
|---|---:|
| 2 ATR / 3 ATR | 40.0% |
| 2 ATR / 1 ATR | 66.7% |
| 3 ATR / 1 ATR | 75.0% |
| 3 ATR / 2 ATR | 60.0% |

These follow from `loss / (win + loss)`; costs raise the required success rate. Actual capped targets, gaps, time exits and thesis invalidation require empirical net outcomes instead of this binary illustration.

The relevant measure is `P(win) × average net win − P(loss) × average net loss`. More units and leverage scale exposure and outcome; they do not turn negative expectancy positive. The implementation correctly avoids multiplying actual broker units by leverage again when computing P/L.

### Shadowing is useful, but does not yet establish a winner

Plans start only after future approved directional orders fill. They preserve entry-time evidence and compare the production price/time baseline with 1/2-ATR targets, 2/3-ATR stops and production/7-day/14-day horizons. Wider-stop quantities are reduced. The earlier nearest-level 1-ATR alternative is preserved as requested; mean reversion is excluded.

However, production now uses a monthly boundary while alternatives use nearest-level caps. The comparison therefore changes **both target distance and structure selection**. A 1-ATR alternative can be capped below 1 ATR. To isolate the cause of any improvement, retain the existing comparison and add matched structure controls with the same target/stop distances.

Minute-sampled quotes cannot reconstruct continuous first touches, queue priority, stop slippage or unavailable overnight execution. Gap counts and lateness are recorded, but there is no full append-only quote path to independently replay every outcome. These are modeled observations, not broker-executed alternative trades. Frequent parameter changes and selecting rules after seeing their outcomes also increase selection bias; this is a recognized backtest-overfitting problem. [Bailey et al., original paper](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).

## Remaining findings, ranked

### A1 — High: pending close requests have no complete terminal-state supervision

**Evidence:** `makemoney.manage_etoro_demo_positions` skips further management when a matched position is `EXIT_PENDING`. Close order IDs are recorded in events, but the maintenance path does not use them to resolve a rejected/cancelled/expired close order or enforce a pending-age alert. An isolated broker fixture left the position pending with zero close-order lookup calls. Ten real positions were still open roughly 5½ hours after their first close requests, issued around **09:16 CEST** today.

Saturday market closure can legitimately delay execution. The audit does **not** establish that these orders failed or should be blindly resubmitted. The defect is the absence of a complete status/age/escalation process.

**Required:** durable close-order state, authoritative status lookup, market-aware aging, rejected/cancelled handling, and alerts while exposure remains open. Retry only after resolving the prior outcome. Verify pending states still retain broker protection and contribute to risk.

### A2 — High: acknowledged protection corrections can remain permanently unresolved

**Evidence:** `already_requested` and `last_protection_request_signature` in `makemoney.manage_etoro_demo_positions` suppress the same requested protection indefinitely, even after the five-minute window. The isolated reproduction used an hour-old acknowledgment and a continuing broker stop mismatch: zero PATCH calls, final `RECONCILIATION_REQUIRED`.

Deduplicating rapid requests is good. Suppressing correction forever without a terminal modification state or alert is not convergence.

**Required:** distinguish accepted/pending/failed/applied updates, verify the broker result, use bounded status-aware retries and escalate persistent mismatches. Do not indiscriminately PATCH every minute. The six target updates earlier today were verified at the broker; this finding describes a failure path, not a claim that those six remain mismatched.

### A3 — High before enabling hosted recovery: S3 `if_newer` can roll back valid local state

**Evidence:** `s3_backup.S3SQLiteBackup.restore_latest` compares S3 object `LastModified` with the main SQLite file's mtime, then deletes local WAL/SHM/journal sidecars and replaces the database. Main-file mtime does not identify the newest committed WAL state, and object upload time does not identify the newest logical database state.

A disposable, crash-style WAL fixture contained two committed rows locally and one in the fake remote backup. `restore_latest(if_newer)` restored the older backup and left one row. SQLite integrity and checksum validation did not detect logical rollback.

**Current exposure:** S3 is **disabled**. Existing manual recovery copies are on the same Docker volume, so they do not protect against losing that volume. The backup mechanism uploads SQLite only, not `/data/runtime.env` or an entire deployment's secret/configuration inventory.

**Required:** preserve an existing persistent database by default, use explicit disaster-recovery selection rather than mtime inference, retain the previous DB and WAL evidence, validate an isolated restore, and reconcile broker orders before enabling submissions after recovery. Add off-site backup-age alerts and secure recovery of necessary configuration. Do not enable the current automatic overwrite policy merely because uploads work.

### A4 — High for trading claims: missing policy attribution and valid forward evidence

**Evidence:** `trading_workflow.VERSION` remains `news_reaction_v2` across multiple exit-policy changes. Full configuration snapshots and target evidence help, but strategy outcome grouping in `workflow_store.broker_performance` groups broadly by version/framework. It does not identify immutable source/policy revisions or target amendments. Zero shadow rows and one closed directional-v2 trade cannot validate the latest rules.

**Required:** immutable policy/source identifiers, entry and amendment timestamps, separate legacy/fixture/manual/current cohorts, and a fixed prospective evaluation protocol. Preserve canceled, rejected, censored and unresolved outcomes. Do not aggregate legacy position sizes into a claim of new-strategy skill.

### A5 — Medium: net reward/risk is enforced inconsistently

**Evidence:** `trading_workflow.size_order` requires minimum net reward/risk only when structure adjusted the target; otherwise it checks gross reward/risk and positive net payoff. The final broker-cost check has the same conditional structure. A pure reproduction with configured minimum R=1, stop=2 ATR and target=2 ATR returned no execution errors with **net R≈0.964**, below the configured minimum.

**Required:** define the setting as gross or net and enforce that meaning across all new-entry paths after all known costs. Keep existing-position profit-taking distinct: taking a smaller positive profit on an existing position need not meet the original entry R threshold.

### A6 — Medium: cost and return accounting are estimates rather than a complete ledger

Observed spread, configured 10-bps round-trip costs, 8% annual financing, broker what-if costs and reported fees have useful evidence labels. They are not a reconciled economic ledger. Entry sizing adds spread explicitly and may also add broker `marketSpread` within opening costs; a unit-level cost taxonomy is needed to avoid counting overlapping components twice. Shadow executable quotes already include spread and use a different modeling convention.

Stock CFD financing and borrow costs vary by instrument, direction and conditions; weekend charging can differ from a smooth annual accrual. A universal 8% forecast is not an actual eToro fee schedule. [eToro overnight fees](https://www.etoro.com/trading/fees/cfd-overnight-fees/), [stock CFD fee calculation](https://help.etoro.com/s/article/how-are-the-overnight-fees-on-stocks-calculated?language=en_GB).

Stored closed fees were numeric zero for all seven closures. This means the field was reported/normalized, not that all economic costs have been independently verified absent. Broker net P/L must not have those fees subtracted again. AI/infrastructure costs, FX, cash flows and full financing attribution remain outside demonstrated strategy net returns.

**Required:** cost-component reconciliation, actual-versus-forecast records, equity/cash-flow ledger and consistent net metrics across execution, economics, historical replay and shadows.

### A7 — Medium: liveness health is not risk-control health

`heartbeat_loop` runs independently. `maintenance_loop` catches errors and continues. A healthy heartbeat/frontend can coexist with repeatedly failing maintenance, unresolved protection or a backup daemon repeatedly failing uploads. SMTP is awaited inside the serialized submission helper, potentially holding the account lease while waiting for message acceptance.

**Required:** timestamps and error streaks for successful portfolio refresh, protection verification, close-state resolution, research completion, shadow observation and off-site backup. Alert outside the trading process. Move notification work behind a durable outbox so mail latency does not delay risk operations. Test exception/restart and lease-contention scenarios.

### A8 — Medium: portfolio controls do not yet cover all relevant loss modes

Implemented: entry stop budgets, gross exposure, cash/margin, position count, per-order caps, single-position exposure and a configured gap stress. Sector checking is available but currently disabled. There is no demonstrated correlation-factor budget, cash-flow-adjusted daily loss/drawdown circuit breaker, or calibrated liquidity/short-borrow/earnings-event risk model. The legacy `max_drawdown` setting is not an active broker-equity daily-loss kill switch.

Legacy holdings intentionally retain their original sizing and management treatment. MA is the clearest example. Current new-entry limits do not certify that every inherited position complies. Stops and a fixed gap scenario are not guaranteed maximum losses.

**Required:** explicit inherited-position policy, market/sector factor aggregation, operationally tested daily-loss controls and execution-availability/liquidity criteria appropriate to the chosen holding period. Preserve the current useful entry checks.

### A9 — Medium before public hosting: operational and security assurance is incomplete

One supervised worker, persistent Docker volume, rotating stdout logs, local TLS, password hashing, admin controls and credential redaction are present. This is materially better than the earlier local setup. It is not a tested high-availability deployment. SQLite leases coordinate one shared database; separate replicas with separate volumes can still both trade the account. No Fly deployment, failover rehearsal or complete restore-to-broker reconciliation was demonstrated.

Authentication uses custom 30-day browser sessions. No application login throttling was found; a browser-set session cookie is not a server-issued HttpOnly session. Direct dependencies are pinned, but there is no complete transitive lock or security assessment. Treat public exposure separately from localhost operation.

**Required:** single active writer enforced operationally, protected ingress/admin access, session/access-control testing, dependency provenance and restore/worker-failure drills. These are readiness gaps, not claims of a demonstrated remote exploit.

### A10 — Low/medium: visibility and evidence retention need clearer boundaries

Companies now reads retained lifecycle evidence, so its date slider does not recover archived analyses. Archival preserves data but does not purge physical storage. Snapshot/event/archive growth needs a storage budget. Overwatcher and some legacy/local controls remain alongside the active deterministic workflow; five agent names do not imply five independent validations. Reporting-period currency provenance improved, but original publication-time datasets and full ADR/FX conversion remain incomplete.

SMTP acceptance is correctly distinguished from inbox receipt. Earlier delivery was confirmed to one mailbox; the replacement secondary mailbox has not been confirmed end to end. No new email was sent in this audit. There is no durable notification outbox or inbox-delivery ledger.

**Required:** label analytics coverage, expose the effective active policy, define retention/export budgets, and trace notification delivery independently of trade submission.

## What would make it commercially credible?

1. Repair A1/A2 and add meaningful control-health alerts; preserve ambiguous broker outcomes rather than guessing.
2. Repair/test recovery before enabling hosted off-site automation. Demonstrate a restore without losing or duplicating a broker intent.
3. Freeze and identify a prospective policy cohort. Keep the requested demo strategy and earlier 1-ATR shadow distinguishable; add matched controls before attributing improvement to one parameter.
4. Accumulate completed, net, attributable outcomes across different conditions. A milestone such as 100–200 completed trades over several months is a planning target, **not statistical proof**; confidence depends on effect size, dependence and tail losses.
5. Report net expectancy, average win/loss, profit factor, risk-normalized outcomes, drawdown, time in trade, slippage/costs, exposure and unresolved-order rates. Group uncertainty by trading day/symbol/event rather than treating correlated observations as independent.
6. Compare with a simple eligible-entry baseline, the prior exit policy and an appropriate cash-flow-adjusted benchmark. Quantify whether the LLM adds value beyond its costs.
7. Reassess live readiness only after reliable operations, reconciled economics and prospective evidence support it. Passing tests or a profitable demo week is insufficient on its own.

The useful next step is disciplined operation and measurement of the implemented demo system, alongside targeted reliability repairs. Additional entry signals or wider targets should earn their place through attributable results.

## Architecture follow-up — 18 October 2026

Position Manager now owns post-submission maintenance and the durable A1/A2 recovery controls, preserving existing position policies and pending-action evidence on adoption. Performance Analyst produces scheduled, advisory daily/weekly evidence reports with unique broker-position net outcomes, policy grouping and matched shadow observations. This supplies a repeatable review mechanism; it does not establish strategy expectancy, complete missing legacy provenance or repair the other outstanding audit findings. See IMPLEMENTATION_REPORT for the current responsibility split.


### A3 follow-up — 18 October 2026

Tigris integration replaces unsafe mtime-based restoration: existing local DB/WAL
state wins; validated cold overwrite requires an explicit flag and preserves
previous evidence. Downloads require SHA256 plus SQLite checks. Recovery pauses
new demo entries. Runtime and backup locks prevent overlapping local operations.
The supervisor now handles termination and stops writers before final backup;
Compose allows 120 seconds. Admin backup status surfaces age and errors.
Deployment verification succeeded at 19:27 Europe/Stockholm on 18 October:
Tigris accepted the snapshot; a download passed SHA256 and SQLite checks; an
isolated restore recovered 365 trades and two users and paused new entries in
that copy. The active database remained untouched and the service healthy.
This establishes the remote backup/restore path, not guaranteed recovery during
every failure. Secrets remain outside database backups.

### Backup architecture review — 18 October 2026

Keep one SQLite database for now and retain the existing 900-second snapshot
interval. A sampled dbstat inventory showed agent_events ~165.32 MiB,
signals ~67.62 MiB, trade_events ~39.59 MiB, broker_snapshots ~36.39 MiB, and
news_articles ~3.71 MiB. Splitting news storage alone would barely reduce the
current snapshot size. Much of the weight is repeated analysis and broker
evidence, rather than a simple cache.

Critical durable state includes users/profiles/permissions, configuration,
pre-submission intent and request IDs, order acknowledgements, fills, protection
changes, pending recovery/close actions, and the decision/policy/economic
evidence linked to a trade. Unsubmitted analysis is lower operational priority,
but valuable for evaluating rejected opportunities and avoiding selection bias.
News and historical provider data cannot always be reproduced exactly later;
retain the evidence used to authorize submitted trades.

A future split should separate a coherent operational ledger and immutable
trade evidence from disposable caches/raw diagnostic payloads. Define retention
and references first; two writable DBs introduce cross-database consistency and
recovery ordering. Do not simply omit current tables from backups or purge
history in the name of reconstruction.

A future improvement is a durable backup request after critical commits,
including pre-submission intent, with debouncing/coalescing and bounded retries
through the existing workflow. Keep periodic full snapshots as a fallback.
This is not implemented by the hosting manifest. Uploading synchronously on
every broker action would couple execution to object storage latency; uploading
after acknowledgement alone leaves an intent/acknowledgement failure window.
The present off-site recovery point may lag by fifteen minutes, or longer after
failed backups, even though local committed transactions survive normal restarts.
No backup design eliminates the need for broker reconciliation after recovery.
