# Local MVP: Paper Portfolio and Integrations

## Run locally

1. Copy `.env.example` to `.env.config` and set local credentials there. `.env.config` is git-ignored.
2. Run `./start.sh`.
3. Open the local URL printed by Streamlit. HTTPS stays disabled unless `ENABLE_HTTPS=true` is set in `.env`.

The app stores users, sessions, research, trade lifecycles, and portfolio allocations in `finbot.db`. Login uses a signed 30-day browser cookie backed by a revocable SQLite session. Logout revokes the session. Existing SHA-256 password records are upgraded to salted scrypt after a successful login.

## Portfolio semantics

- Starting capital is the configured account balance for the local paper account.
- Available cash is starting capital less open-position allocations plus realized P&L.
- Invested capital is cash allocated as margin to open paper positions.
- Gross exposure is invested capital multiplied by position leverage.
- Each research candidate has its own trade ID and ordered Research, Strategy, Risk, Execution, and optional Exit events.
- Every trade records a default 2% protective stop and a side-aware take-profit target; the selected prices are included in the trade event log.
- Positions remain open across app restarts until manually closed in the trade panel.

The current simulator uses feed confidence and simple deterministic checks. Stop and target prices are recorded but are not automatically triggered because the dummy provider supplies no live price updates. It does not calculate real market P&L or send brokerage orders. The displayed values are operational scaffolding, not a trading recommendation or estimate of real returns.

## Provider scaffolding

The backend uses `yfscreen` to screen U.S. equities and `yfinance` to fetch ticker headlines immediately at startup and once per hour. Articles are deduplicated and stored in SQLite, displayed in the dashboard, and attached to matching trade research logs. Use the sidebar's Manual research actions to fetch headlines only, analyze stored headlines, or fetch and analyze in one action. Screening/news retrieval does not itself open or close trades. Yahoo can rate-limit or return no articles; on those runs the configured ticker list is tried, errors are logged, and the existing `research_feed.json` trading simulation remains available.

OpenAI research is disabled by default and can be enabled in the sidebar. When enabled, each candidate and up to eight related headlines are sent to the configured OpenAI-compatible Chat Completions endpoint. The adapter requires structured JSON for signal, confidence, catalyst, and summary; these results are saved with the research event and feed the Strategist. API/configuration errors are surfaced rather than silently falling back to dummy decisions.

The Strategist still applies deterministic confidence and direction rules, the Risk Manager remains the deterministic final threshold gate, and execution remains paper-only. OpenAI does not place orders and no live brokerage orders are available. Interactive Brokers remains a separate execution/account integration and is not used as a news source. IBKR news/data availability depends on supported services, subscriptions, and account permissions. Keep provider credentials in `.env.config`; no key is required for the Yahoo screener/news packages.

Before using broker credentials, use a dedicated paper account, restrict permissions, never enable withdrawals, and keep `.env.config` out of source control.
