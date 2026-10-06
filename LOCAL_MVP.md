# Local MVP: Paper Portfolio and Integrations

## Run locally

1. Copy `.env.example` to `.env.config` and set local credentials there. `.env.config` is git-ignored.
2. Run `./start.sh`.
3. Open the local URL printed by Streamlit. HTTPS stays disabled unless `ENABLE_HTTPS=true` is set in `.env`.

The app stores users, sessions, research, trade lifecycles, and portfolio allocations in `finbot.db`. Login uses a signed 30-day browser cookie backed by a revocable SQLite session. Logout revokes the session. Existing SHA-256 password records are upgraded to salted scrypt after a successful login.

## Profile and users

The Profile page lets each signed-in user choose an avatar and change their password. Administrators can create accounts, grant or revoke admin access, and delete other users. Password changes require the current password and at least 12 characters. User removal revokes that user's sessions, and the last administrator cannot be removed or demoted. Existing databases gain avatar/admin columns in place; if a database has no administrator, its earliest account is promoted.

## Docker

1. Ensure `.env.config` exists and contains the credentials/settings you want to pass to the container. Compose injects this ignored file at runtime; it is excluded from the image build context.
2. Build and start with `docker compose up --build -d`.
3. Open `http://localhost:8501`. To publish on another host port, run `FINBOT_HOST_PORT=8502 docker compose up --build -d` and open port 8502.

Compose bind-mounts `./data` to `/data` and stores SQLite at `./data/finbot.db`, so database updates survive container rebuilds and restarts. To use an existing local database, stop the local app and copy it before the first container start: `mkdir -p data && cp finbot.db data/finbot.db`. Back up `data/finbot.db` with the container stopped so SQLite writes are complete. `docker compose down` stops the service but leaves the bind-mounted database in place.

The image installs Python dependencies at build time. It runs the existing background worker and Streamlit app together; `FINBOT_SKIP_INSTALL=true` prevents package installation at container startup. This is a local paper-trading deployment, not a live broker integration.

## Portfolio semantics

- Starting capital is the configured account balance for the local paper account.
- Available cash is starting capital less open-position allocations plus realized P&L.
- Invested capital is cash allocated as margin to open paper positions.
- Gross exposure is invested capital multiplied by position leverage.
- The dashboard's Decisions over time chart has an independent quick range: 24 hours, 3 days, 7 days, 1 month, or 3 months.
- Each research candidate has its own trade ID and ordered Research, Strategy, Risk, Execution, and optional Exit events.
- Every trade uses a 2% base stop and 5% base take-profit distance at 1x. Both price distances are divided by leverage, so at 2x the stop is 1% from entry and the target is 2.5% from entry, preserving the same account-level risk/reward thresholds.
- Trade lifecycles and Companies → Related Trades show projected P&L at the take-profit price using the recorded quantity (and therefore leverage); this estimate is before fees and slippage. Realized P&L is separate and is recorded when a paper position closes.
- Positions remain open across app restarts until manually closed in the trade panel.

The current simulator uses feed confidence and simple deterministic checks. Stop and target prices are recorded but are not automatically triggered because the dummy provider supplies no live price updates. It does not calculate real market P&L or send brokerage orders. The displayed estimates are operational scaffolding, not a trading recommendation or estimate of real returns.

## Provider scaffolding

The backend uses `yfscreen` to screen U.S. equities and `yfinance` to fetch ticker headlines immediately at startup and once per hour. Repeat fetches refresh article timestamps and metadata. During an agent analysis cycle, the Researcher marks each stored headline relevant/not relevant using candidate ticker, company, and sector-topic matches, and persists the reason. Only relevant items are passed into matching trade lifecycle evidence. The dashboard shows the classification and reason; use Manual research actions to fetch only, analyze stored items, or do both in one action. Screening/news retrieval does not itself open or close trades. Yahoo can rate-limit or return no articles; on those runs the configured ticker list is tried, errors are logged, and the existing `research_feed.json` trading simulation remains available.

OpenAI research is disabled by default and can be enabled in the sidebar. When enabled, each candidate and up to eight related headlines are sent to the configured OpenAI-compatible Chat Completions endpoint. The adapter requires structured JSON for signal, confidence, catalyst, and summary; these results are saved with the research event and feed the Strategist. API/configuration errors are surfaced rather than silently falling back to dummy decisions.

The **OpenAI API spend** panel uses the organization Costs endpoint and requires an organization admin key (`OPENAI_ADMIN_KEY`) plus organization ID (`OPENAI_ORGANIZATION_ID`) in `.env.config`. It reports month-to-date usage cost, not remaining prepaid credit; the project API key alone cannot retrieve billing costs.

The Strategist still applies deterministic confidence and direction rules, the Risk Manager remains the deterministic final threshold gate, and execution remains paper-only. OpenAI does not place orders and no live brokerage orders are available. Interactive Brokers remains a separate execution/account integration and is not used as a news source. IBKR news/data availability depends on supported services, subscriptions, and account permissions. Keep provider credentials in `.env.config`; no key is required for the Yahoo screener/news packages.

Before using broker credentials, use a dedicated paper account, restrict permissions, never enable withdrawals, and keep `.env.config` out of source control.

## Automatic research and oversight

Use **Research Actions** in the sidebar to choose automatic mode, set its interval, and see the next scheduled run. The panel also shows the latest completed analysis outcome, rejection reasons, and available paper cash. A cycle can run successfully without opening a trade: strategist/risk rules may reject every candidate, or execution may reject new positions when available cash is insufficient. Existing positions remain open until manually closed, so allocated cash may stay unavailable.

The **Oversight** tab displays saved Overwatcher audit reports, including per-trade leverage and stop-loss checks, issues, and audit timestamps. Select an older log to review it, or choose **Run audit now** to create another report. JSONL files are stored in `audit_logs/`. The scheduler daemon writes operational output to `scheduler.log` when started with `run_streamlit.py`.
