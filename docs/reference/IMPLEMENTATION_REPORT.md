# Finbot

Finbot pursues positive **net returns** through news research, explicit strategy rules, and portfolio risk controls. It is a demo research and execution application. Positive net expectancy has **not** been established.

The active eToro workflow discovers companies from Yahoo news/screens, enriches them with company fundamentals, asks OpenAI for evidence-based research, and runs the Strategist, Risk Manager, and Execution gates. It supports positive-news LONG trades, negative-news SHORT trades, and a separate measured conditional mean reversion track. An actual rejection stops that candidate; unavailable optional data is recorded separately from execution eligibility.

After submission, **Position Manager** owns fill confirmation, protection, reconciliation, recovery, exits and shadow observations. Existing positions are adopted without changing their saved policies. **Performance Analyst** produces advisory daily/weekly outcome reviews from local broker evidence; it does not change trading settings. Use Position management and Performance reviews in the dashboard; expand the lifecycle chart to inspect Position Manager steps.

Market timing is calculated in each instrument's primary-exchange timezone rather than a global ET clock. Research and lifecycle evidence distinguish the underlying regular session from eToro quote availability, retain local weekday/session context through every agent, and recalculate it before submission. These fields are observational until strategy evaluation shows that a specific session interaction improves net results.

- **Local simulation:** `FINBOT_BROKER_PROVIDER=dummy`; local simulated records, no broker orders.
- **eToro demo:** `FINBOT_BROKER_PROVIDER=etoro`, `ETORO_DEMO_TRADING=true`; automatic demo submission when enabled and all execution gates pass.
- **Live orders:** blocked in the eToro adapter.

## Run

Set credentials and an initial administrator password in the ignored `.env.config`, then:

```sh
docker compose up -d --build --force-recreate
docker compose logs -f finbot
```

Open https://localhost:8501. Local Compose uses a self-signed certificate, so the browser initially shows a certificate warning; trust the local certificate to remove it. Compose persists the database at `/data/finbot.db` in the Docker-managed `finbot_data` volume and operational logs in `./logs`. Rebuilding does not erase the volume. The local Python database `./finbot.db` is separate. Run active-database operations inside Docker; do not open its live SQLite files from macOS. Do not run `docker compose down -v`, which deletes the database volume. See [HOSTING.md](HOSTING.md) for local volume operations and backups.

## Hosted database backups

For hosting, keep SQLite on local SSD or a persistent block volume and run one
writable container. Optional S3 support restores a validated snapshot before
startup, uploads consistent versioned snapshots periodically, and makes a final
backup during a normal shutdown. Do not mount an S3 bucket as the live SQLite
filesystem. See [HOSTING.md](HOSTING.md) for configuration, IAM permissions,
restore modes, and the point at which PostgreSQL becomes necessary.
See [CONFIGURATION.md](CONFIGURATION.md) for the complete environment, persisted setting, agent rule, email, storage, and hosting map.

## Main controls

Under the sidebar agent configuration:

- **Execution:** `max_order_units=5`, `max_order_spend_usd=200`, and `analysis_retention_days=3` by default. Both sizing caps apply. Spend is account margin/capital with estimated costs reserved; leverage 2 can produce nearly $400 gross exposure from $200 capital. Actual market fills can differ; an excessive confirmed fill triggers a close request.
- **Risk Manager:** position count, aggregate stop risk, gross exposure, single-position exposure, gap-stress scenario, ATR stop/target, estimated costs, and maximum holding time.
- **Strategist:** confidence/fundamental gates, integer leverage, and conditional mean reversion thresholds.

In **Research Actions**, enable automated eToro demo orders and configure the research interval. Maintenance runs independently every minute; historical evaluation does not gate demo automation. Administrators control trading and settings; other users have read-only dashboard access.

The main dashboard's Trade Lifecycles distinguish executed/submitted orders from analysis. Each new sized lifecycle explains its unit calculation. **Analysis cleanup and restore** provides manual archival and restoration alongside hourly scheduled archival.

Successful eToro demo submissions can be emailed through configurable SMTP.
Open **Trade email notifications** in the sidebar to configure the recipient,
server, authentication, STARTTLS or implicit TLS, and the sanitized lifecycle
evidence attachment. Notification failure is recorded on the lifecycle but
never changes a broker-acknowledged order into a failed trade.

Every expanded trade lifecycle also provides **Download JSON report** and
**Download HTML report** controls. Both formats include the stored agent stages
and supporting data after credential redaction. SMTP settings provide a
connection/authentication test that sends no message and an explicitly labeled
sample-report delivery button.

## Tests and limitations

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary databases and mocked external providers; they submit no actual orders. They verify application behavior, not broker account permissions or profitable predictions. See [LOCAL_MVP.md](LOCAL_MVP.md) for exact strategy rules, [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md) for changes and remaining gaps, and [the archive inventory](WORKSPACE.md) for the original audit.


Is the core application sufficiently complete?
I broadly agree with you. It is now sufficiently complete to run as a monitored eToro demo experiment while you gather evidence and make smaller adjustments.
The core capabilities are present:
- Dynamic news discovery
- Company-specific OpenAI research
- LONG and SHORT news reactions
- Conditional mean reversion
- SMA and ATR evidence
- Exchange-local session context
- Portfolio-level risk and sizing
- Automatic demo submission
- Fill and protection reconciliation
- Lifecycle evidence
- Notifications and reports
- Data retention and backups
The next phase should emphasize observation:
- Actual win/loss and net return
- Time to target or stop
- Cost and financing impact
- Results by strategy framework
- Results by market session and weekday
- Confidence calibration
- Reconciliation reliability
- OpenAI and infrastructure costs
It is not yet evidence that the system makes money, and I would not call it ready for live capital. Those are evaluation conclusions rather than missing core features.

Strategies yet to test
- Trades based on resistance levels at entry, how do they factor in.
- Every profit should be higher than average stop loss to compensate for bad stop losses. 
- What if we attempt an inverse bet against the market for the market course corrects in three phases, the dip, the recovery, the true victory. 


Prospective exit comparisons are available in **Shadow comparison**. New approved
directional fills are monitored through the existing agents without additional
broker orders. See [LOCAL_MVP.md](LOCAL_MVP.md#prospective-shadow-exit-tracking)
for variants, observation limits and the explicit target ATR ceiling.

Weekend workload and entry permissions are configurable under **Research Actions**. By default, news/research run six-hourly on Saturday/Sunday in Europe/Stockholm, new entries are blocked, and routine position checks slow to 15 minutes. Protection, exits and uncertain-order recovery stay enabled. Normal weekday cadence resumes automatically. This weekend policy supplements existing exchange-session and broker checks; it is not a holiday calendar.

Configuration audits and recovery evidence are consolidated under **Position management**. On-demand historical path analysis, its batch tool and saved results are under **Performance reviews**, with shortcuts on individual trades. Historical scenarios remain separate from broker-confirmed net outcomes.

User onboarding supports shared trading-profile memberships (viewer/operator/owner) and platform-admin suspension controls. The current **Finbot shared demo** profile remains the sole active trading runtime; additional profiles are drafts. See [MULTITENANCY.md](MULTITENANCY.md) for current permissions and the copy/paste prompt for independent broker/account runtimes.


### Tigris database backups (18 October 2026)

S3-compatible Tigris support uses private bucket `datadb`, endpoint
`https://t3.storage.dev`, region `auto`, prefix `finbot/shared-demo`.
Configure credentials in local `.env.config` or Fly Secrets. Periodic validated
Compressed SQLite snapshots run every 3600 seconds, with a final best-effort shutdown
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


Current hosting: the local Docker Compose container owns trading; Fly compute and volume were retired on 22 October 2026. See [HOSTING.md](HOSTING.md). Historical deployment evidence is archived under `data/archive/`.

### Closure notifications and weekly closed-trade dashboard — 19 October 2026

Added broker-confirmed closure mail, a Monday 09:00 Europe/Stockholm weekly
summary of the previous calendar week, and a Closed trades dashboard with
gains/losses/net, counts, chart, broker-position details and CSV. Totals use
broker-reported net outcomes once per position; unknown P/L and invalid dates
are flagged instead of fabricated. Existing closure history is baselined without
sending old alerts. Additive notification schema and preferences live in SQLite.

Position Manager queues closure events in the ledger transaction. Performance
Analyst queues weekly evidence only after a recent complete post-window history
refresh, using the existing account lease. A separate notification service
dispatches recipient-level persisted work outside broker maintenance; it is not
an additional trading agent. Shared SMTP transport preserves existing submission
mail. Retry, refusal, partial-recipient success and ambiguous-DATA handling are
tracked without blind resends. Viewer/operator permissions and dashboard
rendering are tested. No test mail was sent.

Validation before deployment: 252 tests passed, including 23 new report/outbox/UI
regressions. Live Fly remained running during the remote image build. The deployed update
reused its volume and credentials.


Live verification on 19 October: deployed image
registry.fly.io/finbot-tbfs1a@sha256:1b46169a5f5087ef4cb6f14999e582db99504616b9a2a0d59c687ccbd2e3df72
to the existing Machine. Pre-update recovery copy:
 /data/recovery/before-outcome-emails-20261019T173656Z.db.
Worker/frontend health, public HTTPS and SQLite integrity passed.
Three users and enabled automatic demo/AI workflow persisted.
The initial migration baselined 25 prior closures without alerts.
A complete broker-history refresh was recorded and Performance Analyst queued
the authorized catch-up report for 12–18 October using the account lease.
The normal notification worker delivered it to both configured recipients:
two SENT rows, one attempt each, no errors. This records SMTP acceptance,
not confirmed inbox arrival. Report totals: 7 closures; winning gains USD 132.75,
losing losses USD 1,181.47; net realized P/L USD -1,048.72.
Future weekly reports are scheduled Monday 09:00 Europe/Stockholm.
No new environment variables or secrets are required for this feature.

### Compact shadow lifecycle evidence (19 October 2026)

Repeated shadow experiment outcomes now appear in one Shadow tracking step per
trade, with a dated outcome table and expandable original evidence. Existing
stored events and downloads remain intact. Each variant/horizon completes once;
these lifecycle records do not represent additional broker submissions. Trading,
shadow sampling, and execution policies are unchanged.

### Bounded hourly backups (20 October 2026)

Backups now use gzip and a single upload per snapshot with verified server-side
rotation into latest and previous. Retention deletes only recognized Finbot
snapshot keys after both retained objects validate. Cadence is hourly, with
startup and graceful shutdown snapshots unchanged. Legacy raw SQLite restores
remain supported. A one-time private local recovery copy is retained separately.

Live cleanup verified: the entire `datadb` bucket contains only latest and
previous backup objects, totaling 130,538,731 bytes (about 131 MB), each roughly
65 MB compressed. Both retained copies passed SHA256 and SQLite checks before
older snapshots were removed. The separate Mac snapshot is 65,250,213 bytes
and passed local checksum and SQLite validation. Full isolated suite: 258 tests
passed; live hourly cadence confirmed at 3600 seconds.


### Forecast reviews and holding limits (20 October 2026)

AI forecast_horizon_hours now sets review_after_hours, not an automatic exit.
The captured risk-manager news_max_hold_hours (48h by default) or
mean_reversion_max_hold_hours (24h) sets max_hold_hours/holding_hours. Stops,
targets and thesis invalidation still apply at all times. A review point does
not launch a new AI request or change protection; normal Position Manager
checks continue. Lifecycle details distinguish review timing and hard expiry.
Existing verified open news positions adopt their captured cap once, retaining
original opening times and shadow comparisons. Closed trades, legacy positions,
and pending close requests are excluded. Recovery repairs empty position IDs
only from matching trade and broker linkage, backs off pretransport failures,
and retains a tighter verified broker stop without widening it. Configuration
audits now include active recovery state and report active issues separately.

Live verification: ten broker-linked open positions received versioned timing
policies, including six whose AI-shortened 24h hold was restored to the captured
48h directional cap. Original opening times were retained. NVDA #996 and META
#1271 protection corrections were each submitted once and broker-confirmed.
GOOGL #1088 adopted its tighter broker stop without any broker write. Newly
observed BMY #1285 retained broker SL 57.93 while its saved TP was aligned after
explicit review of a never-submitted escalated intent; one correction confirmed.
Recovery actions 19,31,40,43 are APPLIED; old BMY action42 is SUPERSEDED. Full
isolated suite: 269 tests passed. Private pre-migration evidence is retained on
Fly at /data/recovery/before-timing-recovery-20261020.json.


### Scheduled position reassessment (20 October 2026)

Elapsed time never authorizes a close. Forecast horizons set the first review;
review intervals schedule subsequent Position Manager reassessments. Historical
`news_max_hold_hours` and `mean_reversion_max_hold_hours` setting keys remain
compatible, but the GUI labels them review intervals (48h / 24h defaults).
`holding_hours` remains a cost-estimation/shadow horizon, not an enforced expiry.

Due reviews reuse the normal quote and broker protection snapshot, saved company
research, and fresh completed daily bars. Risk Manager reviews target alignment;
current structural opposing research can invalidate a thesis through the existing
exit path. A trend mismatch alone is flagged for review, not automatic closure.
Stale or missing evidence cannot authorize a thesis close, and schedules a bounded
follow-up. No extra AI cycle or automatic SL/TP movement is introduced. Stops,
targets, fresh thesis invalidation, and sizing/risk breaches remain active exits.

Verified open directional and mean-reversion trades migrate once to timing v3;
closed/closing and legacy trades are preserved. The old age-only exit is removed
from execution even for restored older policies. Reassessments appear in one
lifecycle step with a dated evidence trail. Frozen shadow time-exit experiments
remain unchanged and are counterfactual observations, not live close orders.

This policy supersedes the earlier 48h forced-exit rollout.

Reassessment rollout verified live: all ten broker-linked open positions use
policy v3 with time_exit_enabled=false and max_hold_hours=null. All remained
OPEN, automated trading stayed enabled, and no pending close actions existed.
A prospective pure-function check for every position confirmed that even a
one-year age cannot generate a close between its protective levels. No reviews
were overdue at deployment; BE/MU's first forecast reviews remain due at about
22:03 Stockholm on 20 October. Full isolated suite: 271 tests passed; the added
reassessment-grouping regression also passed. Original SL/TP and frozen shadow
plans were preserved. Pre-migration evidence remains on Fly at
/data/recovery/before-reassessment-20261020.json.
