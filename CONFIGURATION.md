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
| `FINBOT_DB_PATH` | repository DB; `/data/finbot.db` in Compose | Active SQLite database. Use local or persistent block storage, never S3 as a mounted SQLite filesystem. |
| `FINBOT_S3_BACKUP_ENABLED` | `false` | Enable periodic consistent SQLite snapshots. |
| `FINBOT_S3_BUCKET`, `FINBOT_S3_PREFIX` | empty, `finbot` | Snapshot destination. |
| `FINBOT_S3_BACKUP_INTERVAL_SECONDS` | `900` | Backup interval; minimum 60 seconds. |
| `FINBOT_S3_RESTORE_MODE` | `if_newer` | `never`, `if_missing`, `if_newer`, or `always`. |
| `FINBOT_S3_RESTORE_REQUIRED` | `false` | Fail startup when the required restore cannot complete. |
| `FINBOT_S3_ENDPOINT_URL` | empty | Optional S3-compatible endpoint. |
| `FINBOT_S3_SERVER_SIDE_ENCRYPTION` | `AES256` | S3 encryption mode. |
| `FINBOT_S3_KMS_KEY_ID` | empty | KMS key when using `aws:kms`. |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | SDK default | S3 region. |
| `STREAMLIT_PORT` | `8501` | Internal Streamlit port. |
| `ENABLE_HTTPS` | `false` | Development self-signed HTTPS. Use a reverse proxy for hosted TLS. |
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
