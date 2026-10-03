# Local MVP: Paper Portfolio and Integrations

## Run locally

1. Copy `.env.example` to `.env` and replace `FINBOT_PASSWORD` with a private password.
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

`.env.example` contains separate placeholders for a news source, an OpenAI-compatible analysis endpoint, and Interactive Brokers. The current researcher reads `research_feed.json`; it does not use IBKR news or OpenAI. A future news adapter retrieves licensed headlines/data, an AI adapter analyzes that input, and IBKR is configured separately for brokerage execution. IBKR data/news availability depends on supported services, subscriptions, and account permissions, so it should not be assumed as the research source. `integrations.py` defines provider protocols and adapter placeholders. Providers default to `dummy`; selecting a real provider currently raises `NotImplementedError` rather than making network requests. No credential needs to be entered until a reviewed adapter is implemented.

Before using broker credentials, use a dedicated paper account, restrict permissions, never enable withdrawals, and keep `.env` out of source control.
