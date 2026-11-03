# Finbot

**Active hosting:** this Mac runs the sole trading container; the cPanel dashboard remains online. Fly compute and its volume were removed on 22 October 2026. See [HOSTING.md](docs/reference/HOSTING.md) for current operation.

Finbot pursues positive **net returns** through news research, explicit strategy rules, and portfolio risk controls. It is a demo research and execution application. Positive net expectancy has **not** been established.

The active eToro workflow discovers companies from Yahoo news/screens, enriches them with company fundamentals, asks OpenAI for evidence-based research, and runs the Strategist, Risk Manager, and Execution gates. It supports positive-news LONG trades, negative-news SHORT trades, and a separate measured conditional mean reversion track. An actual rejection stops that candidate; unavailable optional data is recorded separately from execution eligibility.

After submission, **Position Manager** owns fill confirmation, protection, reconciliation, recovery, exits and shadow observations. Existing positions are adopted without changing their saved policies. **Performance Analyst** produces advisory daily/weekly outcome reviews from local broker evidence; it does not change trading settings. Use Position management and Performance reviews in the dashboard; expand the lifecycle chart to inspect Position Manager steps.

Fresh temporary dips/rallies can qualify for measured reversals in mixed daily
trends at half risk. Confirmed two-bar range breakouts have a separate
continuation path with ATR protection. Stale broker quotes get two data retries;
missing quotes and request failures are reported separately. See the
[fresh-move and quote rules](docs/reference/LOCAL_MVP.md#fresh-dips-rallies-and-quote-retries--30-october-2026).

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

Open https://localhost:8501. Local Compose uses a self-signed certificate, so the browser initially shows a certificate warning; trust the local certificate to remove it. Compose persists the database at `/data/finbot.db` in the external Docker volume `finbot_migrated_20261022` and operational logs in `./logs`. Rebuilding does not erase the volume. Historical host-side databases are archived under `data/archive/`; they are not the active database. A direct Python launch defaults to creating `./finbot.db`, so use Compose for the active runtime. Run active-database operations inside Docker; do not open its live SQLite files from macOS. Do not run `docker compose down -v`, which deletes the database volume. See [HOSTING.md](docs/reference/HOSTING.md) for local volume operations and backups.

## Hosted database backups

For hosting, keep SQLite on local SSD or a persistent block volume and run one
writable container. Optional S3 support restores a validated snapshot before
startup, uploads consistent versioned snapshots periodically, and makes a final
backup during a normal shutdown. Do not mount an S3 bucket as the live SQLite
filesystem. See [HOSTING.md](docs/reference/HOSTING.md) for configuration, IAM permissions,
restore modes, and the point at which PostgreSQL becomes necessary.
See [CONFIGURATION.md](docs/reference/CONFIGURATION.md) for the complete environment, persisted setting, agent rule, email, storage, and hosting map.

## Main controls

Under the sidebar agent configuration:

- **Execution:** `max_order_units=5`, `max_order_spend_usd=200`, and `analysis_retention_days=3` by default. Both sizing caps apply. Spend is account margin/capital with estimated costs reserved; leverage 2 can produce nearly $400 gross exposure from $200 capital. Actual market fills can differ; an excessive confirmed fill raises a review; discretionary closure additionally requires the conservative net-profit guard.
- **Risk Manager:** position count, aggregate stop risk, gross exposure, single-position exposure, gap-stress scenario, ATR stop/target, estimated costs, and reassessment timing (no automatic age-based exits).
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

Tests use temporary databases and mocked external providers; they submit no actual orders. They verify application behavior, not broker account permissions or profitable predictions. See [LOCAL_MVP.md](docs/reference/LOCAL_MVP.md) for exact strategy rules, [IMPLEMENTATION_REPORT.md](docs/reference/IMPLEMENTATION_REPORT.md) for changes and remaining gaps.


Prospective exit comparisons are available in **Shadow comparison**. New approved
directional fills are monitored through the existing agents without additional
broker orders. See [LOCAL_MVP.md](docs/reference/LOCAL_MVP.md#prospective-shadow-exit-tracking)
for variants, observation limits and the explicit target ATR ceiling.

Weekend workload and entry permissions are configurable under **Research Actions**. By default, news/research run six-hourly on Saturday/Sunday in Europe/Stockholm, new entries are blocked, and routine position checks slow to 15 minutes. Protection, exits and uncertain-order recovery stay enabled. Normal weekday cadence resumes automatically. This weekend policy supplements existing exchange-session and broker checks; it is not a holiday calendar.

Configuration audits and recovery evidence are consolidated under **Position management**. On-demand historical path analysis, its batch tool and saved results are under **Performance reviews**, with shortcuts on individual trades. Historical scenarios remain separate from broker-confirmed net outcomes.

User onboarding supports shared trading-profile memberships (viewer/operator/owner) and platform-admin suspension controls. The current **Finbot shared demo** profile remains the sole active trading runtime; additional profiles are drafts. See [MULTITENANCY.md](docs/reference/MULTITENANCY.md) for current permissions and the copy/paste prompt for independent broker/account runtimes.


### Tigris database backups (18 October 2026)

S3-compatible Tigris support uses private bucket `datadb`, endpoint
`https://t3.storage.dev`, region `auto`, prefix `finbot/shared-demo`.
Configure credentials in local `.env.config`. Periodic validated
Compressed SQLite snapshots run every 3600 seconds, with a final best-effort shutdown
snapshot. Existing local data wins at startup; only a missing database is
restored automatically. Restored snapshots pause new automated entries.
Admin sidebar **Database backup** provides status and isolated verification.
See [HOSTING.md](docs/reference/HOSTING.md) for settings, recovery and recovery details.

GUI trading rules, schedules, users and profile memberships are database data.
Environment configuration supplies deployment settings and external credentials.
Injected environment values win over missing-value file defaults; GUI credential
updates affect the web process, and SMTP specifically reloads its runtime-file
overrides on send. Restart/recreate processes after changing host environment
settings. A SQLite backup does not carry those separate secrets to another host.


**Closed trades** now includes realized gains, losses and net P/L, a period chart,
trade details and CSV export. Closure emails and reports include the reason, its
evidence source, and trailing-profit trigger details when recorded; unknown
causes are explicitly labeled. Outcome notifications use the existing SMTP
recipients: broker-confirmed closure alerts and a Monday 09:00 Europe/Stockholm
email for the previous week. Preferences and delivery status are available in
that view; existing historical closures are not emailed on upgrade.

Position Manager profit-only discretionary exits, held-company research refresh, pending-entry expiry and tighter-stop recovery are documented in [POSITION_MANAGEMENT_UPDATE.md](docs/reference/POSITION_MANAGEMENT.md).

## Documentation

See [the documentation index](docs/README.md) for current references. See [directory cleanup and data inventory](docs/reference/WORKSPACE.md) for archived databases and restore instructions.

New entries use a positive estimated net target payoff gate (`minimum_reward_risk=0`)
while retaining structure-capped targets, stops and portfolio checks. The active
shared demo enables broker trailing with 15-minute reconfirmation. Arming now
defaults to broker-reported profit: above $10 prime / above $20 major. Select
ATR arming instead to use above 0.5 / 1 entry ATR. Retreat distances remain
0.15 / 0.25 entry ATR in both modes. LONG follows bid highs; SHORT follows ask lows. SL/TP remain active.
See [Position management](docs/reference/POSITION_MANAGEMENT.md#entry-atr-price-trailing--28-october-2026).

Optional **Telegram bot** setup is available in the local administrator sidebar.
It supports private questions, validated agent-setting changes and pause/resume
of new demo entries over outbound polling. It stays disabled until configured;
see [Telegram setup](docs/reference/TELEGRAM.md).


### Entry ATR price tracking — introduced 28 October 2026

The 30 October arming selector defaults to amount ($10 / $20), with ATR arming
available as an alternative. The following price calculations and retreat
distances apply to both; size independence describes the ATR arming option.
Current reconfirmation is 15 minutes; 30 minutes is the installation default.

Position Manager now uses `profit_trailing_prime_arm_atr=0.5`,
`profit_trailing_major_arm_atr=1.0`, `profit_trailing_prime_retreat_atr=0.15`
and `profit_trailing_major_retreat_atr=0.25`, with configurable
`profit_trailing_interval_minutes=30`. Arming is strictly above the threshold;
a retreat at or beyond the threshold requests closure after a fresh identity
and executable quote check. LONG measures bid minus entry and follows the
highest bid; SHORT measures entry minus ask and follows the lowest ask, then
closes on an upward retreat. All distances use the ATR14 saved in the entry
policy, frozen alongside actual broker entry in `price_trailing`. Size, margin,
leverage and cash P/L do not affect the price trigger. Missing/invalid entry ATR
or quotes withhold this discretionary exit; broker protection remains active.
Existing open positions start new observed price peaks at activation; legacy
`profit_trailing` dollar history and closure evidence remain unchanged. Current
settings replace the four legacy USD trailing fields. The independent news-exit
net USD/margin floors and broker SL/TP retain their existing behavior.

Streamlit shows ATR field tooltips and recorded tracking state. Telegram exposes
the new ATR settings and recorded price triggers. The reporting export includes
shared labels/help/units; finbotdashboard shows snapshot time, current imported
ATR rules and accessible tooltips, identifying older snapshots explicitly.

Position Manager also provides an **ATR exit mechanism** selector. **Broker trailing stop** is selected in the shared demo following explicit
user activation on 28 October. Scheduled ATR exits remains the default for new
installations. Broker mode still performs scheduled
reconfirmation, retains take profit and tighter existing stops, and uses durable
broker-update verification. See [native broker trailing](docs/reference/POSITION_MANAGEMENT.md#optional-native-broker-trailing--28-october-2026).

Optional RVOL/VWAP evidence now prioritizes part of the news research queue and
provides bounded direction-aware confidence relaxation for LONG and SHORT.
Capital risk limits and measured reversal requirements remain unchanged; missing
volume retains baseline rules. See [volume qualification](docs/reference/LOCAL_MVP.md#volume-supported-directional-qualification--3-november-2026).
