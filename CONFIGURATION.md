# Finbot configuration reference

Finbot has three configuration layers. Values later in this list do not generally override earlier layers; they control different parts of the application.

1. **Environment and `.env.config`** configure providers, credentials, storage and process behavior.
2. **Persisted application settings** in SQLite configure automation, scheduling, AI research and API logging.
3. **Agent rules** in SQLite configure research, strategy, risk and execution decisions.

Secrets belong in the hosting provider's secret store or an uncommitted `.env.config`. Restart the workload after changing host-injected environment variables. Settings saved through the interface are read at runtime where supported.

## Execution modes

| Setting | Default | Purpose |
|---|---:|---|
| `FINBOT_BROKER_PROVIDER` | `dummy` | `dummy` runs local simulation; `etoro` enables the eToro demo workflow. |
| `ETORO_DEMO_TRADING` | `true` | Required for eToro demo access. Live order routing remains blocked in code. |
| Persisted `demo_automated_trading_enabled` | enabled on new databases | Allows approved candidates to proceed automatically to fresh broker checks and demo submission. |
| `FINBOT_PORTFOLIO_ACCOUNT_SOURCE` | `paper` | Selects the account used for displayed capital snapshots. |

## Research and AI

| Setting | Default | Purpose |
|---|---:|---|
| `FINBOT_NEWS_PROVIDER` | `yfinance` | News provider used by the active workflow. |
| `FINBOT_RESEARCH_PROVIDER` | `yfscreen` | Candidate discovery provider. |
| `FINBOT_NEWS_INTERVAL_SECONDS` | `3600` | Background Yahoo news-fetch interval; minimum 60 seconds. |
| `FINBOT_NEWS_SYMBOL_LIMIT` | `40` | Maximum screened symbols used for news discovery. |
| `FINBOT_NEWS_TICKERS` | empty | Optional comma-separated fallback discovery symbols. |
| `FINBOT_RESEARCH_MAX_CANDIDATES` | `8` | Maximum companies receiving full analysis in one cycle; range 1–20 in the UI. |
| `OPENAI_API_KEY` | empty | Enables OpenAI research when the persisted AI switch is also enabled. |
| `OPENAI_BASE_URL` | OpenAI API | OpenAI-compatible endpoint. |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model used for research analysis. |
| `OPENAI_ADMIN_KEY` | empty | Optional organization usage/cost synchronization. |
| `OPENAI_ORGANIZATION_ID` | empty | Organization used with the admin key. |

The research schedule, suspension time and AI-enabled switch are persisted in SQLite and managed under **Research Actions** and **AI research settings**.

## eToro

| Setting | Default | Purpose |
|---|---:|---|
| `ETORO_API_BASE_URL` | `https://public-api.etoro.com` | Public API host. |
| `ETORO_API_KEY` | empty | Public API key. |
| `ETORO_USER_KEY` | empty | User key sent with authenticated requests. |
| `ETORO_USER_AGENT` | browser-compatible value | User-Agent sent on every eToro request. |
| `ETORO_DEMO_ACCOUNT_ID` | empty | Optional explicit demo account identifier. |
| `ETORO_MAIN_ACCOUNT_ID` | empty | Read-only main-account identifier; live execution stays disabled. |
| `ETORO_API_DEBUG_LOGGING` | `false` | Initial masked request/response logging preference. The UI persists its current value in SQLite. |

## Risk and portfolio environment limits

| Setting | Default | Purpose |
|---|---:|---|
| `FINBOT_RISK_PER_TRADE_PCT` | `0.005` | Planned stop risk per new trade as a fraction of verified equity. |
| `FINBOT_MAX_TOTAL_RISK_PCT` | `0.02` | Maximum aggregate planned stop risk. |
| `FINBOT_MAX_GROSS_EXPOSURE_PCT` | `1.0` | Maximum gross notional exposure relative to equity. |
| `FINBOT_MAX_OPEN_POSITIONS` | `30` | Maximum verified broker positions. |
| `FINBOT_STARTING_CAPITAL` | `10000` | Local simulation starting capital. Broker mode uses verified broker snapshots where available. |

## Agent rules

Agent rules are edited in the sidebar and persisted in SQLite. The interface displays the current value and help text.

### Researcher

- `enabled`, `min_confidence`, `allowed_sectors`, `max_fetch_per_cycle`
- `include_market_fundamentals`, `default_leverage`

The active eToro news workflow discovers candidates dynamically. Some legacy researcher fields are hidden there because they apply only to local simulation.

### Strategist

- Base and fundamentals-confirmed confidence thresholds
- Countertrend LONG and SHORT confidence thresholds
- Conditional mean-reversion excursion, reversal, confidence and stop-cushion thresholds
- Strong/weak fundamental thresholds
- Company-specific news requirements
- Integer default and maximum leverage

The Strategist supports directional news LONG/SHORT and conditional mean reversion. Exchange-local market session, SMA trend and ATR are evidence; session timing does not independently create direction.

### Risk Manager

- `risk_per_trade_pct`, `max_total_risk_pct`, `max_gross_exposure_pct`, `max_open_positions`
- Single-position, sector and stressed-gap exposure limits
- `stop_atr_multiple` and `target_reward_risk`
- `structure_targets_enabled` (default true): cap targets before daily swing zones and weekly/monthly highs/lows; preserve stops and enforce net reward/risk for new entries
- Directional and mean-reversion maximum holding periods
- Estimated round-trip and financing costs

### Execution

- `enabled`, `allowed_side`, `max_trades_per_cycle`
- `max_order_units` (default 5)
- `max_order_spend_usd` (default $200 account capital/margin)
- `max_research_age_hours`
- `analysis_retention_days`

## SMTP notifications

| Setting | Default | Purpose |
|---|---:|---|
| `FINBOT_TRADE_EMAIL_ENABLED` | `false` | Send after an eToro demo submission is acknowledged. |
| `FINBOT_SMTP_HOST`, `FINBOT_SMTP_PORT` | empty, `587` | SMTP server. |
| `FINBOT_SMTP_SECURITY` | `starttls` | `starttls`, `tls`, or `none`. |
| `FINBOT_SMTP_USERNAME`, `FINBOT_SMTP_PASSWORD` | empty | Optional SMTP authentication. |
| `FINBOT_SMTP_FROM` | empty | Sender address. |
| `FINBOT_TRADE_EMAIL_TO` | empty | Comma, semicolon or line-separated recipients. |
| `FINBOT_TRADE_EMAIL_ATTACH_JSON` | `true` | Attach sanitized lifecycle evidence. |
| `FINBOT_SMTP_TIMEOUT_SECONDS` | `20` | Connection timeout. |
| `FINBOT_TRADE_EMAIL_SUBJECT_PREFIX` | `[Finbot]` | Email subject prefix. |

## Database, backup and hosting

| Setting | Default | Purpose |
|---|---:|---|
| `FINBOT_DB_PATH` | repository DB; `/data/finbot.db` in Compose | Active SQLite database. Compose uses Docker-managed `finbot_data` at `/data`; repository `data/` is not the live database. Use local or persistent block storage, never S3 as a mounted SQLite filesystem. |
| `FINBOT_S3_BACKUP_ENABLED` | `false` | Enable periodic consistent SQLite snapshots. |
| `FINBOT_S3_PROVIDER` | `s3` | `s3` or `tigris`; Tigris defaults to its S3 endpoint and region `auto`. |
| `FINBOT_S3_BUCKET`, `FINBOT_S3_PREFIX` | empty, `finbot` | Snapshot destination. |
| `FINBOT_S3_BACKUP_INTERVAL_SECONDS` | `900` | Backup interval; minimum 60 seconds. |
| `FINBOT_S3_RESTORE_MODE` | `if_missing` | Preserve existing DB/WAL. Legacy `if_newer` now behaves identically; `always` requires explicit cold overwrite. `never` disables restore. |
| `FINBOT_S3_RESTORE_REQUIRED` | `false` | Fail startup when the required restore cannot complete. |
| `FINBOT_S3_ENDPOINT_URL` | empty | Optional S3-compatible endpoint. |
| `FINBOT_S3_SERVER_SIDE_ENCRYPTION` | `AES256` | S3 encryption mode. |
| `FINBOT_S3_KMS_KEY_ID` | empty | KMS key when using `aws:kms`. |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | SDK default | S3 region. |
| `STREAMLIT_PORT` | `8501` | Internal Streamlit port. |
| `ENABLE_HTTPS` | Compose: `true`; image/Python: `false` | Direct local TLS with a persistent self-signed certificate. On Fly, keep false and terminate TLS at Fly Proxy. |
| `FINBOT_TLS_CERT_DIR` | Compose: `/data/certs`; Python: `./certs` | Local certificate/key directory; certificates include localhost and loopback IPs. |
| `FINBOT_PUBLIC_HTTPS` | `false` | Set true behind a trusted HTTPS ingress such as Fly; enables Secure session cookies even when internal Streamlit uses HTTP. |
| `FINBOT_LOG_TO_STDOUT` | Compose: `true` | Send scheduler logs to container stdout. |
| `FINBOT_LOG_LEVEL` | `INFO` | Backup/logger verbosity. |

## Authentication

| Setting | Purpose |
|---|---|
| `FINBOT_USERNAME` | Initial administrator username. |
| `FINBOT_PASSWORD` | Required initial administrator password. Use a hosting secret. |

Additional users, administrator roles, passwords and login sessions are stored in SQLite.

## Historical evaluation cost inputs

`FINBOT_BACKTEST_SPREAD_BPS_PER_SIDE`, `FINBOT_BACKTEST_COMMISSION_BPS_PER_SIDE`, `FINBOT_BACKTEST_FINANCING_ANNUAL_PCT`, `FINBOT_BACKTEST_FX_BPS_PER_SIDE`, and `FINBOT_BACKTEST_MAX_DRAWDOWN_PCT` configure historical evaluation assumptions. Historical evaluation remains informational and does not authorize or block demo orders.

## Deprecated or incomplete integrations

`NEWS_API_BASE_URL` and `NEWS_API_KEY` belong to the inactive generic news boundary. IBKR host, port, client and account fields are retained for future/read-only configuration; the active strategy does not submit IBKR orders.

## Shadow comparison and target ceiling

These are Risk Manager agent rules, edited in the existing Agent rule editor.
They are captured in each new plan; changing settings does not rewrite an active
comparison or existing broker protection.

| Rule | Default | Meaning |
|---|---|---|
| `max_target_atr_multiple` | `3.0` | Initial directional target ceiling, before support/resistance caps. Initial distance is the smaller of this and stop ATR × target reward/risk. |
| `shadow_tracking_enabled` | `true` | Enroll new approved directional fills. Existing comparisons continue when enrollment is disabled. |
| `shadow_stop_atr_multiples` | `[2, 3]` | Alternative stop distances; quantity is reduced for wider stops to preserve initial stop risk. |
| `shadow_target_atr_multiples` | `[1, 2]` | Alternative target distances before saved entry-time structure caps. |
| `shadow_horizons_hours` | `[168, 336]` | Additional calendar holding limits; production holding time is always included. |

SMTP uses the configured timeout for connection/authentication and at least 60
seconds while transferring a report and waiting for DATA acceptance. Notification
records distinguish all/partial recipient acceptance. An accepted message has a
Message-ID for mail-server tracing; SMTP acceptance does not confirm inbox
placement. Ambiguous timeouts are not automatically retried, to avoid duplicate
messages.

In local Compose, `FINBOT_ENV_CONFIG_PATH=/data/runtime.env` persists UI-saved
SMTP overrides on the named volume. Host `.env.config` still supplies initial
secrets through Compose. SMTP reloads persisted overrides at send time, so a
rebuild does not silently revert recipient changes. Keep this file private;
UI writes use mode 0600 and atomic replacement.

Directional production now defaults to `structure_target_mode=range_boundary`.
Risk Manager's `structure_range_months=1` includes the current month and one
preceding calendar month. LONG uses the highest high in that range; SHORT uses
the lowest low, buffered inward by 0.1 ATR. `structure_range_weeks=4` supplies a
weekly fallback. Earlier nearby levels remain visible in target evidence.
Selecting `nearest` restores the previous production cap. These controls do not
raise the initial maximum ATR target or bypass entry checks. The 1/2 ATR shadow
alternatives deliberately retain the earlier nearest-level selection; saved
plans are not rewritten.

## Broker action recovery

Position Manager rules `recovery_max_attempts` (3), `recovery_initial_check_seconds` (60), `recovery_max_check_seconds` (900), and `recovery_escalate_hours` (2 business hours) control durable per-action recovery. The maximum check interval precedes jitter; weekend close/entry checks wait at least 900 seconds, and broker Retry-After can extend any wait. These are per-action controls, not a shared API budget. Changing them does not bypass unknown-outcome protection or authorize repeat closes. Attention is displayed in Oversight and trade details; it does not send an additional email.

## Position Manager and Performance Analyst rules

Recovery controls now belong to `position_manager`. On the first upgrade, saved `execution.recovery_*` values move to the new owner; subsequent startups preserve Position Manager settings. Position Manager is required for broker safety and cannot be disabled through the rule editor. Execution retains entry limits, allowed sides and freshness controls. Legacy clients writing recovery-only Execution settings are redirected to Position Manager.

`performance_analyst.enabled` enables local daily/weekly advisory reports; `minimum_closed_positions` defaults to 20 (integer 5–1000). Review periods use Europe/Stockholm calendar boundaries. A 15-minute worker check makes completed-period reviews available shortly after midnight or restart. Reports do not tune settings, place orders or send additional email. There is still no shared API budget.

## Weekend controls

Under **Research Actions**, configure `weekend_pacing_enabled` (true), `weekend_trading_enabled` (false), `weekend_research_minutes` (360), `weekend_news_minutes` (360), `weekend_maintenance_minutes` (15), and `timezone` (Europe/Stockholm). Values persist as `research_schedule_*` keys in SQLite, alongside the existing automatic interval and suspension. Minute intervals accept integers 1–1440; the timezone must be an IANA name. Turning off reduced pacing does not turn on weekend entries. Saving mode/weekday interval alone preserves weekend options. Urgent protection/uncertain-order states retain one-minute maintenance even if a longer routine interval is configured.

Configuration audits are now owned by Position Manager (daily local check and manual action under Position management), with latest-50 scope. Historical path analysis and its batch controls are under Performance reviews; individual trades retain shortcuts. These historical-data requests are on demand only and do not bypass weekend entry controls or send broker orders. Existing frozen reports remain unchanged; new reviews consume saved audit/analysis evidence.

## Shared profiles and login permissions — 18 October 2026

The active trading runtime is now named **Finbot shared demo**. User memberships grant viewer, operator or owner access independently of platform-admin status. Existing admins retain shared-profile owner membership; new normal users default to viewers, with an explicit shared-access selection during registration. Platform admins can suspend/reactivate accounts; suspension revokes sessions without stopping the shared trading workflow. The last active platform admin and last active profile owner are protected.

The active profile is visible in the dashboard. All signed-in users can manage their own login, create inactive draft profile/account/connection metadata, and share owned profiles. Broker/SMTP/AI credentials remain platform-admin-only. Backend action checks and guarded cached reads enforce roles, account suspension and profile access. Existing trading records are tagged to profile 1 without changing their policies or broker actions.

Only the original shared profile can run or display trading data. Draft profiles are deliberately blocked from independent trading and from showing the shared profile's data as their own. Full broker/account isolation, additional adapters, per-profile workers and secret storage remain parked. [MULTITENANCY.md](MULTITENANCY.md) records the implemented boundary and contains a copy/paste prompt for that uplift.


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

## Which settings live where?

A GUI field is not necessarily stored in SQLite. Current storage is:

| Data | Local source of truth | Fly equivalent | Included in SQLite backup? |
|---|---|---|---|
| Users, password hashes, memberships, session signing state | SQLite | Same restored SQLite on volume | Yes |
| Agent rules, research schedule, AI/order toggles, starting capital | SQLite | Same SQLite | Yes |
| GUI-saved broker/API settings, selected portfolio, environment-based strategy fields | Runtime file; web process is updated on save | `/data/runtime.env` on volume, or injected app configuration | No |
| GUI-saved SMTP settings | Runtime file, reloaded for every send | Same runtime file, with missing values supplied by Fly Secrets/env | No |
| Locally supplied API keys and SMTP password | Ignored `.env.config`, injected by Compose | Fly Secrets | No |
| Standard ports, paths, TLS, backup bucket and interval | Compose / env configuration | Versioned `fly.toml` | No |

Under local Docker, the GUI runtime file is **/data/runtime.env**, not the host
repository's **.env.config**. It is a mode-600 plaintext file on the volume.
In a direct host run, the default GUI path is the repository `.env.config`.
The live GUI does not edit Fly's secret vault or the host Compose env file.

General backend startup precedence: injected environment wins; runtime-file
values fill missing keys. Saving GUI fields updates the web process immediately,
but the scheduler is a separate process. For general broker/strategy settings,
restart/recreate it and ensure a conflicting injected value is not masking the
runtime preference. SMTP is an explicit exception: runtime-file SMTP/email
values override injected values and are reloaded for each send.
Agent-rule/schedule controls saved in SQLite are read through their database APIs.

Fly Secrets override same-name TOML env entries. Do not configure the same key
in multiple places unnecessarily. Set stable hosting defaults in TOML, credentials
in Fly Secrets, and trading controls in their existing GUI/database workflow.
Do not remove an environment key merely because a similar GUI field exists:
some of those GUI fields save to runtime.env, and some file keys still supply
fallback credentials to that file.

Local cleanup on 18 October removed only these superseded host entries:
`FINBOT_STARTING_CAPITAL`, `FINBOT_TRADE_EMAIL_ENABLED`,
`FINBOT_TRADE_EMAIL_TO`, `FINBOT_TRADE_EMAIL_ATTACH_JSON`,
`FINBOT_SMTP_HOST`, `FINBOT_SMTP_PORT`, `FINBOT_SMTP_SECURITY`,
`FINBOT_SMTP_USERNAME`, `FINBOT_SMTP_FROM`.
Their effective values already exist in SQLite or runtime.env.
All actual API/SMTP credentials and still-consumed environment settings remain.
See [HOSTING.md](HOSTING.md) for the prepared Fly migration.
