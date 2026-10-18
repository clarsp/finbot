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

Tests use temporary databases and mocked external providers; they submit no actual orders. They verify application behavior, not broker account permissions or profitable predictions. See [LOCAL_MVP.md](LOCAL_MVP.md) for exact strategy rules, [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md) for changes and remaining gaps, and [APPLICATION_AUDIT.md](APPLICATION_AUDIT.md) for the original audit.


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


Fly deployment is prepared in [fly.toml](fly.toml) for `finbot-tbfs1a`.
See [HOSTING.md](HOSTING.md#what-you-need-to-add-to-fly) for the
one-time migration and [CONFIGURATION.md](CONFIGURATION.md#which-settings-live-where)
for GUI/database/runtime-file/Fly Secrets ownership. This preparation does not
deploy the app or move the currently running local scheduler.
