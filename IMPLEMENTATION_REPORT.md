# Finbot current implementation and remediation report

**Updated:** 17 October 2026. This replaces the first-audit repair report. It describes what is implemented and verified today, what is deliberately experimental, and what remains to be repaired. Read [APPLICATION_AUDIT.md](APPLICATION_AUDIT.md) for the trading assessment, live evidence and ranked findings.

**Current status:** an operational, monitored eToro demo platform. The application is not a validated profitable strategy or an unattended/live-capital deployment. This report does not mark the new audit findings as implemented fixes.


## Broker recovery follow-up — 18 October 2026

The A1/A2 execution-recovery gaps now have durable `broker_actions` tracking in the existing Execution workflow. Intent is committed before writes; acknowledgments remain pending until broker evidence confirms the result. Existing pending closes are imported without resubmission. Uncertain closes are not blindly repeated: replacement requires a confirmed terminal failure and a fresh remaining position. Undocumented numeric broker statuses are retained without guessing their meaning.

Protection recovery reuses portfolio snapshots, requires spaced mismatches, reapplies only the same fixed levels, and refuses to loosen a tighter broker stop automatically. Default maximum is three write attempts. Each action persists its next check with backoff and jitter; HTTP Retry-After is respected, and weekend close/entry checks slow down. There is no shared API budget. Unresolved actions appear in Oversight and trade details with one attention event. Partial execution requires portfolio convergence; it does not establish full closure or realized P/L.

The regression suite now passes **156 tests**, including timeouts, restarts, duplicate requests, partial execution, terminal rejection, protection convergence and broker waiting periods. This addresses the implementation gaps in A1/A2; it does not prove every existing queued close has executed. The original audit snapshot below remains historical. S3 restore safety, profitability evidence, accounting and the other audit findings remain separate work.

## Verification record

| Check | Current evidence |
|---|---|
| Regression suite | **125 tests passed**, rerun on 17 October; isolated DBs and mocked providers. |
| Running service | One `finbot-finbot-1` container using Compose image `finbot-finbot`; healthy. |
| Deployed image | SHA-256 `506806e6a90c813d1c40450b0f04cf594295c2d0f9396040e7f944c73aa71f94`. |
| Active database | `/data/finbot.db`, Docker named volume; audit reads performed inside Docker; quick check passed. |
| Automatic workflow | AI research enabled, automatic 60-minute research schedule, demo orders enabled. |
| Effective order controls | Both LONG/SHORT; **3 orders/cycle**, 5 units/order and $200 estimated capital/order. Persisted values can differ from source defaults. |
| Broker state at audit | 19 open broker positions: 9 normally open, 10 with pending close requests. Local OPEN counts also contain two non-broker records. |
| Broker-confirmed realized history | 7 closures, reported combined net -$1,048.72; mixed legacy/current policies. |
| Latest target rollout | Six pre-existing positions were retargeted and verified earlier today; this is not six prospective entries under the new rule. |
| Shadow cohort | Enrollment enabled; **0 rows** at the audit snapshot. |
| Notifications | TLS SMTP configured for two recipients; server acceptance and inbox delivery remain different states. |
| Off-site backup | Updated 18 October: Tigris enabled; existing-local preservation replaces unsafe mtime restore. Credentials corrected; actual upload, checksum/integrity download and isolated restore verified. |

The historical paired comparison is exploratory evidence, not an acceptance test for production profitability. Repository HEAD predates substantial uncommitted source changes; the deployed image identifier is therefore necessary to identify this audited build.

## Current architecture

The active path is Researcher → Strategist → Risk Manager → Execution. The Researcher uses external news/company data and an LLM. Strategy and risk stages are predominantly deterministic rules; agent names are not independent votes on whether a trade will win.

Automatic candidates, manual retries and saved-fixture tests share the final fresh submission boundary. Before sending an order, it refreshes account state, positions, quotes, daily indicators and strategy/risk checks, requires full execution readiness, checks costs/capital, saves a durable reference and atomically claims the lifecycle. Broker ambiguity remains unresolved until evidence arrives; it is not automatically treated as permission to send another order.

Independent maintenance refreshes the broker portfolio/history, reconciles known fills and protection, requests policy exits and observes shadows. A renewable account lease serializes broker operations against the same database. This is a one-writer design, not an account-wide distributed lock across separate databases.

## Earlier repair areas: verified status

| Area | What is now implemented | Remaining qualification |
|---|---|---|
| Final submission risk bypass | Requires accepted fresh risk, `execution_ready=True`, no execution errors, valid units/stop/target and enabled execution. Regression coverage rejects changed-account failures. | New audit A5 identifies inconsistent **net-R semantics**, not the old readiness bypass. |
| Direction and leverage | Directional LONG/SHORT, capped integer leverage, direction-aware protection and P/L on actual units. | Leverage amplifies exposure/return on capital, not the probability of success. |
| Position sizing | Verified equity, available capital, aggregate marked stop risk, gross exposure, per-order caps, single-position and gap-stress limits; optional sector gate. | Sector gate is off; legacy holdings are not resized; daily broker-equity drawdown/correlation control is incomplete. |
| Exit policy | Direction-aware targets/stops, holding-time exits, structural thesis invalidation and oversized-fill close requests for current-version positions. | Pending closes lack full terminal-order supervision; legacy positions have explicitly different management. |
| Broker close contract | Demo close includes instrument ID, normalizes position IDs and recognizes nested close-order acknowledgments. | An acknowledged close is not a fill. Ten positions still have pending closes. |
| Broker reconciliation | Confirmed portfolio fills, requested-versus-observed protection, closed history/P&L/fees and honest unknown states. | Partial/multiple executions and persistent protection/close transitions require further work. |
| Concurrency and uncertain orders | Atomic lifecycle claim, stable order reference, renewable account lease, entry-order lookup and uncertain-outcome preservation. | Does not solve split-brain replicas or terminal close-order resolution. |
| Mean reversion | Measured pre-news anchor, completed post-news excursion/recovery, temporary-reaction thesis and minimum confidence. Both directions are tested. | Not validated as profitable; no prospective mean-reversion shadow cohort. |
| Data provenance | Statement versus quote currency, completed daily SMA/ATR data, exchange-local session metadata and multi-company article associations. | Complete publication-time datasets, formal event novelty, ADR/FX reconciliation and full holiday-calendar assurance remain open. |
| Accounting | Broker equity/cash snapshots, reported running P/L, broker closed outcomes and strategy grouping; costs labeled as forecasts where appropriate. | Cash-flow-adjusted returns, full cost ledger and immutable policy-level attribution are incomplete. |
| UI and lifecycle reporting | Executed versus unexecuted history, meaningful duplicate references, current Companies lifecycle analytics, sizing/economics and downloadable sanitized evidence. | Companies counts retained active lifecycles, not archived history; some legacy controls remain. |
| SMTP | Persistent UI overrides, TLS/authentication, multiple recipients, longer DATA timeout, partial-recipient reporting and Message-ID. Notification failure does not change a broker-acknowledged order into failure. | No durable outbox; acceptance does not certify inbox delivery; replacement secondary mailbox not confirmed. |
| Storage/startup | Named Docker volume, one supervised worker, process heartbeat, local TLS, rotating container logs and consistent backup primitives. | Semantic risk-health alerts and safe off-site restore are not complete. |
| Access control | Required initial password, password hashing, persisted/revocable sessions, administrator order/settings controls and redaction. | No public-host penetration assessment; login throttling/session hardening and dependency assurance remain work. |

## Current target policy

Directional production now uses Risk Manager's `structure_target_mode=range_boundary`:

1. Calculate the initial target distance as `min(stop_atr_multiple × target_reward_risk, max_target_atr_multiple) × ATR14`.
2. LONG uses the highest high; SHORT the lowest low across the current month plus `structure_range_months` preceding calendar months. The current value is one preceding month. Only completed daily input bars are used.
3. Apply a 0.1-ATR inward buffer at that outer boundary. The selected target cannot extend the initial ATR target. Record nearer obstacles that the price would need to pass.
4. If monthly range metadata is unavailable, use the configured weekly fallback (currently four weeks), then the previous nearest-level rule if usable dated ranges are absent. Unavailable structure is recorded rather than fabricated.
5. Keep normal new-entry strategy, cost, portfolio and reward/risk gates. Mean-reversion targets retain their separate anchor/nearest-level treatment.

The default stop remains 2 ATR and the initial target ceiling 3 ATR. The monthly rule does not prove a breakout of nearer levels. It is an authorized demo experiment with no additional independently validated breakout-volume gate.

An explicit open-position review uses Risk Manager, fresh quotes from verified broker instrument IDs, the shared protection-update path and broker reconciliation. It preserves broker stops and quantity, requires current-price clearance and positive estimated net payoff, and can extend a previously shortened target within the ATR/range boundary. An expired holding limit is not an excuse to extend a target. It does not automatically retarget every position on every maintenance tick.

Earlier today eToro verified MA #253 at $621.41, NVDA #607 at $235.78, PEP #622 at $123.76, V #643 at $404.02, ECHO #667 at $83.12 and META #718 at $777.60, with unchanged stops. XOM, AAPL and FCEL were skipped by clearance/net-payoff checks. See the [position review](data/experiments/range-targets-20261017/report.md). These are protection settings, not realized profits.

## Current shadow implementation

`shadow_tracking.py` supplies durable comparisons integrated into existing agents:

- Risk Manager freezes a plan only after an eligible directional entry passes final gates.
- Reconciliation initializes rows from verified fill price, quantity and opening time. Existing old positions are not retrospectively enrolled.
- Position Manager maintenance observes fresh broker bid for LONG and ask for SHORT, and continues the hypothetical paths after an actual position closes.
- Variants use 2/3-ATR stops and 1/2-ATR targets across production, 168-hour and 336-hour holding limits. Wider stops reduce alternative units to preserve baseline planned stop risk within quantity caps.
- **The earlier nearest-level 1-ATR shadow remains intact.** All current alternatives retain that older structure rule while new production uses the monthly range rule. Requested ATR targets and effective capped targets are shown separately.
- Completion, observation gaps, late deadlines, modeled costs and net outcomes are persisted. No extra broker orders are sent. Existing plans are immutable across later settings/target changes.

Missing evidence remains missing: zero rows were present at audit time, the quote history is not continuously replayable, and these are quote-observed counterfactuals rather than actual fills. The initial price/time baseline does not replay later thesis invalidation or discretionary actions. Structure and ATR effects currently change together; matched controls are needed for causal interpretation.

## New remediation backlog

Priority is an ordering for follow-up work, not a claim that these changes were performed in this documentation task.

| Priority / audit ID | Required change | Evidence needed to close it |
|---|---|---|
| P1 / A1 | Persist and supervise close-order IDs and terminal states; add market-aware pending-age alerts. | Acknowledged, delayed, filled, rejected, cancelled and uncertain close fixtures; no duplicate blind closes; recovery against actual broker states; queued Saturday requests remain distinguished from failures. |
| P1 / A2 | Replace indefinite signature suppression with status-aware protection convergence. | An acknowledged but unapplied PATCH produces bounded verification/escalation and safe retry after authoritative resolution; applied requests are not repeated. |
| P1 before S3 enablement / A3 | Make restore selection safe for existing DB/WAL state and protect the prior local recovery point. | The reproduced two-row-local/one-row-remote case retains the newer commits; explicit disaster restore into isolation; restored intents reconciled before submission resumes. |
| P1 for strategy claims / A4 | Add immutable source/policy IDs and amendment attribution, then run a fixed prospective cohort. | Legacy, fixtures, prior exits and new monthly entries remain distinct; reproducible reports include unresolved/censored cases and dependence-aware uncertainty. |
| P2 / A5 | Standardize minimum net reward/risk across all new-entry paths after known costs. | An uncapped gross-R=1/net-R≈0.964 order is rejected when minimum **net** R is 1; capped, mean-reversion, what-if-fee and unavailable-cost cases agree. Existing-position positive-profit policy stays separate. |
| P2 / A6 | Implement consistent component-level broker costs and cash-flow-aware performance attribution. | Spread counted once; actual versus modeled financing/fees/FX reconciled; broker net not charged again; compute/infrastructure costs reported separately. |
| P2 / A7 | Add control-success telemetry and a notification outbox. | A fresh heartbeat with stale/failed maintenance or backup raises an alert; slow/unavailable SMTP does not retain the trading-account lease. |
| P2 / A8 | Add active broker-equity loss controls and correlation/inherited-position treatment. | Current controls distinguish entry caps from historical holdings; daily loss/cash-flow cases and concentrated correlated shocks are tested. |
| P2 before public hosting / A9 | Harden access/session controls and rehearse single-writer recovery. | No overlapping active writers through deployment/failover; tested recovery, protected public ingress, access/session testing and dependency provenance. |
| P3 / A10 | Clarify analytics/retention coverage and delivery observability. | Archived-versus-retained counts labeled; growth budget monitored; notification acceptance and confirmed receipt remain separate. |

## Audit reproductions and test limits

The new audit ran the existing suite and additional isolated checks; it did not silently add fixes:

| Isolated check | Observed result |
|---|---|
| Minimum R=1, uncapped target at gross R=1 | Execution errors empty; net R approximately 0.964. |
| Hour-old acknowledged protection request; broker stop still differs | No correction PATCH; reconciliation remained required. |
| Open broker position with local EXIT_PENDING | No close-order lookup; status remained pending. |
| Crash-style local WAL with newer commits; fake S3 snapshot with later upload timestamp | Restore replaced two committed rows with the one-row older snapshot. No live DB or real S3 object was touched. |

The host test environment could run the application suite but did not have `boto3` installed; the S3 reproduction therefore used the deployed Docker Python environment. This is another reason not to equate a green local suite with complete deployment/recovery coverage. External provider contracts, slippage, outages and inbox receipt need separate integration evidence.

## Recommended next operating phase

Continue the authorized demo experiment with visible monitoring of pending closes/protection and a separate inherited-position view. Resolve the P1 reliability/recovery items, freeze identifiable policy cohorts and collect enough completed, attributable net outcomes to assess expectancy and tails. Preserve the 1-ATR shadow, and add matched structure controls before concluding that a target-selection component caused an improvement.

A future live-capital review should require reconciled economics, prospective evidence, tested broker-failure/recovery behavior and operational access controls. The application does not currently meet that standard. No new hard execution lock or settings change was introduced by this audit.

## Six-role architecture — 18 October 2026

Researcher → Strategist → Risk Manager → Execution is the entry path. Execution performs final checks, durable submission and broker acknowledgment. **Position Manager** owns fill confirmation, held-position monitoring, protection, reconciliation, controlled recovery, exits and quote-observed shadow tracking. It uses the existing renewable account lease, adapter and persisted action state; it does not create a second scheduler or repeat orders during migration.

Existing broker-linked active positions are adopted idempotently into `position_management`. Their policies, stop/target levels, holding limits, historical event authors and pending broker-action IDs, attempts and next-check times are preserved. New verified fills receive ownership metadata automatically. Historical trades remain available to analysis; shadow plans are never fabricated retrospectively.

**Performance Analyst** is an advisory, deterministic evidence-review agent. The worker checks locally every 15 minutes and saves one immutable review of the previous completed Stockholm calendar day and previous completed Monday–Monday week. On first start/resume it reviews the latest completed periods; cumulative broker-confirmed results include earlier history. Frozen reviews record available evidence; late broker history is reflected in later reports. It adds no broker/LLM polling and cannot change rules or submit orders.

Reports distinguish broker-reported realized net P/L from current entry exposure, count each broker position once, exclude unknown net P/L and show fees without subtracting them twice. Results are grouped by direction, symbol and captured-policy fingerprint; legacy attribution remains incomplete. Same-trade/same-horizon completed shadow pairs use estimated net costs and disclose observation gaps. The default 20-closure review threshold is a minimum reporting guard, not statistical proof of profitability. Recommendations stay advisory.

Dashboards expose six agent roles, a five-role per-trade overview (Performance Analyst is portfolio-level), optional expanded Position Manager stages, a Position management view and a Performance reviews view. Existing trade details retain full historical events.

Architecture validation: **171 regression tests passed** on 18 October, including idempotent adoption, pending-action preservation, settings ownership migration, shared account locking, unique-position net accounting, Stockholm period boundaries and paired shadow eligibility.

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

Shared-profile permission validation: **210 tests pass**, including direct backend denials, viewer/operator UI boundaries, cached-read authorization, suspension/session revocation, default-profile migration, last-owner protections and concurrent last-admin demotion attempts.


### Tigris database backups (18 October 2026)

S3-compatible Tigris support uses private bucket `datadb`, endpoint
`https://t3.storage.dev`, region `auto`, prefix `finbot/shared-demo`.
Configure credentials in local `.env.config` or Fly Secrets. Periodic validated
SQLite snapshots run every 900 seconds, with a final best-effort shutdown
snapshot. Existing local data wins at startup; only a missing database is
restored automatically. Restored snapshots pause new automated entries.
Admin sidebar **Database backup** provides status and isolated verification.
See [HOSTING.md](HOSTING.md) for settings, recovery and Fly deployment details.

GUI trading rules, schedules, users and profile memberships are database data.
Environment configuration supplies deployment settings and external credentials.
Injected environment values win over missing-value file defaults; GUI credential
updates affect the web process, and SMTP specifically reloads its runtime-file
overrides on send. Restart/recreate processes after changing host environment
settings. A SQLite backup does not carry those separate secrets to Fly.


Backup validation, 18 October: **225 tests passed**. Running image rebuilt and
container recreated; live SQLite integrity passed and supervisor runtime lock
confirmed. After correcting the access-key ID, Tigris accepted a 336,551,936-byte
snapshot at 19:26:34 Europe/Stockholm. A downloaded copy matched SHA256
`43871dbc88c532839efdde7e0fa1a8132d5f0e4a9a3ecb2c83ed75851e156318`
and passed SQLite integrity checks. An actual isolated restore recovered 365
trade records and two users, with new automated entries paused in that temporary
copy. The active database was not replaced; Finbot remained healthy.
Destination: `s3://datadb/finbot/shared-demo/latest/finbot.db`. Periodic backups
remain enabled at 900 seconds; final shutdown backup is best effort.

### Fly deployment preparation and configuration cleanup — 18 October 2026

Added fly.toml for finbot-tbfs1a (ams), with persistent /data, proxy HTTPS,
always-on single-writer deployment guidance, 2 GiB RAM, a 3 GiB volume,
Tigris snapshots and graceful termination. Existing data must restore on an
empty volume; fresh initialization is refused there if the snapshot is absent.
Fly Secrets and volume data persist across deployments; no Fly deployment has
been performed from this workspace.

Documented actual configuration ownership and precedence in CONFIGURATION.md.
Removed nine superseded entries from local .env.config, preserving credentials
and the GUI runtime file. The architecture recommendation keeps one database
and the current fifteen-minute backups; a future event-triggered backup request
and cache retention boundary are described in APPLICATION_AUDIT.md, not enabled.

Validation: TOML parsed successfully and storage/port/TLS/backup/credential
separation checks passed. Local Finbot was recreated after env cleanup; effective
SMTP settings and database starting capital compared identical before/after.
Fly CLI is not installed in this workspace, so Fly server-side validation and
deployment remain unperformed.


### Simplified Fly secret migration — 18 October 2026

HOSTING.md now begins with the exact current credentials and a single staged
import route. scripts/export_fly_secrets.py prepares ignored, mode-600
data/fly-secrets.env from the running container's credential allowlist and
effective SMTPSettings (including GUI runtime-file overrides). Current export
contains seven credentials and ten email settings; no values are printed and
no Fly API call/deployment occurs. This route replaces the earlier instruction
to transfer runtime.env separately for the current deployment. The file uses
Fly's literal import quoting; unrelated env keys and standard TOML hosting
settings are excluded. Four targeted tests passed. Local Finbot configuration
and trading behavior were not changed.
