import asyncio
import html
import importlib
import inspect
import os
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict

import extra_streamlit_components as stx
import makemoney as backend_module
import pandas as pd
import streamlit as st

from makemoney import (
    DEFAULT_AGENT_RULES,
    add_user,
    ensure_db,
    authenticate_user,
    close_paper_trade,
    create_session_token,
    get_ai_research_enabled,
    get_dashboard_summary,
    get_portfolio_summary,
    revoke_session_token,
    run_hourly_news_fetch,
    run_trading_cycle,
    set_starting_capital,
    set_ai_research_enabled,
    update_agent_rules,
    validate_session_token,
)

st.set_page_config(page_title="Finbot Local MVP", layout="wide")
ENABLE_HTTPS = os.getenv("ENABLE_HTTPS", "false").lower() in {"1", "true", "yes", "on"}

st.markdown(
    """
    <style>
        .stApp {
            background: #0f172a;
            color: #e2e8f0;
        }
        .main .block-container {
            padding-top: 1rem;
            padding-bottom: 2rem;
        }
        .agent-card {
            background: linear-gradient(180deg, #111827 0%, #0f172a 100%);
            border: 1px solid rgba(148, 163, 184, 0.25);
            border-radius: 18px;
            padding: 1rem 1rem 0.9rem;
            min-height: 200px;
            box-shadow: 0 10px 30px rgba(15, 23, 42, 0.35);
            margin-bottom: 1rem;
        }
        .agent-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.5rem;
            margin-bottom: 1rem;
        }
        .agent-name {
            font-size: 1.05rem;
            font-weight: 700;
            color: #f8fafc;
        }
        .agent-pill {
            border-radius: 999px;
            padding: 0.3rem 0.7rem;
            font-size: 0.72rem;
            font-weight: 700;
            color: #fff;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }
        .agent-stats {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 0.75rem;
            margin-top: 0.75rem;
            margin-bottom: 0.9rem;
        }
        .agent-stats div {
            background: rgba(15, 23, 42, 0.8);
            border: 1px solid rgba(148, 163, 184, 0.15);
            border-radius: 10px;
            padding: 0.6rem 0.5rem;
            text-align: center;
        }
        .agent-stats strong {
            display: block;
            font-size: 1.35rem;
            font-weight: 800;
            color: #f8fafc;
        }
        .agent-stats span {
            display: block;
            font-size: 0.68rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: #94a3b8;
        }
        .agent-meta {
            color: #cbd5e1;
            font-size: 0.82rem;
            line-height: 1.5;
        }
        .flow-stream {
            display: flex;
            flex-wrap: wrap;
            gap: 0.6rem;
            margin: 0.8rem 0 1rem;
        }
        .flow-item {
            padding: 0.6rem 0.8rem;
            border-radius: 12px;
            background: rgba(30, 41, 59, 0.75);
            border: 1px solid rgba(148, 163, 184, 0.2);
            color: #e2e8f0;
            font-weight: 600;
        }
        .flow-item.accepted {
            border-color: rgba(34, 197, 94, 0.6);
            box-shadow: inset 0 0 0 1px rgba(34, 197, 94, 0.3);
        }
        .flow-item.rejected {
            border-color: rgba(239, 68, 68, 0.6);
            box-shadow: inset 0 0 0 1px rgba(239, 68, 68, 0.25);
        }
        .flow-item.pending {
            border-color: rgba(148, 163, 184, 0.4);
        }
        .flow-arrow {
            align-self: center;
            color: #94a3b8;
            font-size: 1.25rem;
        }
        .sidebar .sidebar-content {
            background: #020817;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

SESSION_COOKIE = "finbot_session"


def get_cookie_manager():
    return stx.CookieManager(key="finbot_cookies")


def login_view(cookie_manager) -> bool:
    st.title("Finbot Local MVP")
    st.caption(
        "HTTPS is disabled by default for local testing. Set ENABLE_HTTPS=true when running through run_streamlit.py to enable TLS."
    )

    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submit = st.form_submit_button("Login")

        if submit:
            if authenticate_user(username, password):
                token = create_session_token(username)
                cookie_manager.set(
                    SESSION_COOKIE,
                    token,
                    expires_at=datetime.now() + timedelta(days=30),
                    secure=ENABLE_HTTPS,
                    same_site="strict",
                )
                st.session_state.authenticated = True
                st.session_state.username = username
            else:
                st.error("Invalid username or password.")

    return st.session_state.authenticated


def render_admin_panel() -> None:
    with st.sidebar.expander("Admin", expanded=False):
        st.subheader("Create Local User")
        with st.form("register_form"):
            username = st.text_input("New username", key="new_username")
            password = st.text_input("New password", type="password", key="new_password")
            if st.form_submit_button("Create user"):
                if add_user(username, password):
                    st.success(f"User '{username}' created.")
                else:
                    st.warning("User already exists or missing username/password.")


def render_agent_cards(agents: list[Dict[str, Any]]) -> None:
    st.subheader("Agent decisions in selected period")
    if not agents:
        st.info("No agent data for this time range.")
        return

    columns = st.columns(4)
    for column, agent in zip(columns, agents):
        with column:
            st.markdown(f"**{agent['agent_name'].replace('_', ' ').title()}** · {agent['status'].title()}")
            accepted, rejected = st.columns(2)
            accepted.metric("Accepted", agent["accepted_count"])
            rejected.metric("Rejected", agent["rejected_count"])


def render_portfolio(summary: Dict[str, Any]) -> None:
    portfolio = summary["portfolio"]
    open_trades = [trade for trade in summary["trades"] if trade["status"] == "OPEN"]
    st.subheader("Portfolio")
    metrics = st.columns(6)
    metrics[0].metric("Starting capital", f"${portfolio['starting_capital']:,.2f}")
    metrics[1].metric("Available cash", f"${portfolio['cash_available']:,.2f}")
    metrics[2].metric("Invested", f"${portfolio['invested_capital']:,.2f}")
    metrics[3].metric("Gross exposure", f"${portfolio['gross_exposure']:,.2f}")
    metrics[4].metric("Net equity", f"${portfolio['account_equity']:,.2f}")
    metrics[5].metric("Open positions", str(len(open_trades)))

    chart_col, trend_col = st.columns(2)
    with chart_col:
        st.markdown("#### Capital by open position")
        if open_trades:
            exposure = pd.DataFrame(open_trades).groupby("symbol")["allocated_capital"].sum()
            st.bar_chart(exposure, color="#38bdf8")
        else:
            st.caption("No open positions yet.")
    with trend_col:
        st.markdown("#### Decisions over time")
        events = [
            event for event in summary.get("decision_timeline", [])
            if event["decision"] in {"accepted", "rejected"}
        ]
        if events:
            event_frame = pd.DataFrame(events)
            event_frame["created_at"] = pd.to_datetime(event_frame["created_at"])
            event_frame["count"] = 1
            trend = (
                event_frame.groupby([pd.Grouper(key="created_at", freq="15min"), "agent_name"])["count"]
                .sum()
                .unstack("agent_name", fill_value=0)
                .reindex(columns=["researcher", "strategist", "risk_manager", "execution"], fill_value=0)
            )
            trend.columns = ["Researcher", "Strategist", "Risk Manager", "Execution"]
            st.line_chart(trend, color=["#38bdf8", "#f59e0b", "#a78bfa", "#22c55e"])
        else:
            st.caption("Agent decision history will appear after the first research cycle.")


def render_event_evidence(event: Dict[str, Any]) -> None:
    details = event.get("details") or {}
    st.markdown(f"**Outcome: {event['decision'].title()}**")
    st.write(event["message"])

    decision_data = {
        key: value for key, value in details.items()
        if key not in {"related_news", "news_context", "ai_analysis"}
    }
    if decision_data:
        st.markdown("**Analyzed values**")
        st.json(decision_data)

    headlines = details.get("related_news") or details.get("news_context") or []
    if headlines:
        st.markdown("**Headline input**")
        headline_frame = pd.DataFrame([
            {
                "title": item.get("title", ""),
                "ticker": item.get("ticker", ""),
                "source": item.get("source", ""),
                "published_at": item.get("published_at", ""),
                "relevance": item.get("relevance_status", "unreviewed"),
                "relevance_reason": item.get("relevance_reason", ""),
                "url": item.get("url", ""),
            }
            for item in headlines
        ])
        st.dataframe(
            headline_frame,
            hide_index=True,
                width="stretch",
            column_config={"url": st.column_config.LinkColumn("Article")},
        )
    elif event["agent_name"] in {"researcher", "strategist", "risk_manager"}:
        st.caption("No stored headlines were attached to this decision.")

    ai_analysis = details.get("ai_analysis")
    if ai_analysis:
        st.markdown("**OpenAI research response**")
        st.write(ai_analysis.get("summary", "No summary returned."))
        st.json({
            key: ai_analysis.get(key)
            for key in ("provider", "model", "signal", "confidence", "catalyst")
        })
    elif "ai_analysis" in details:
        st.caption("OpenAI research was disabled for this cycle; the decision used the configured rules and dummy baseline.")


def render_trade_lifecycles(trades: list[Dict[str, Any]]) -> None:
    st.subheader("Trade lifecycles")
    if not trades:
        st.info("No research cycles recorded yet. Run a cycle to create the first trade lifecycles.")
        return

    for trade in trades:
        company_name = trade.get("company_name") or trade["symbol"]
        label = f"#{trade['id']} · {company_name} ({trade['symbol']}) · {trade['side']} · {trade['status']}"
        with st.expander(label, expanded=trade["status"] == "OPEN"):
            metrics = st.columns(6)
            metrics[0].metric("Allocated", f"${trade['allocated_capital']:,.2f}")
            metrics[1].metric("Entry", f"${(trade['entry_price'] or 0):,.4f}")
            metrics[2].metric("Quantity", f"{trade['quantity']:,.4f}")
            metrics[3].metric("Leverage", f"{trade['leverage']:,.1f}x")
            metrics[4].metric("Stop loss", f"${trade['stop_loss_price']:,.4f}" if trade["stop_loss_price"] else "N/A")
            metrics[5].metric("Take profit", f"${trade['take_profit_price']:,.4f}" if trade["take_profit_price"] else "N/A")

            events = trade["events"]
            if events:
                flow = ""
                for index, event in enumerate(events):
                    decision = event["decision"]
                    status_class = "accepted" if decision == "accepted" else "rejected" if decision == "rejected" else "pending"
                    flow += (
                        f'<div class="flow-item {status_class}"><strong>{html.escape(event["stage"].title())}</strong>'
                        f'<br>{html.escape(event["agent_name"].replace("_", " ").title())}'
                        f'<br><small>{html.escape(decision.title())}</small></div>'
                    )
                    if index < len(events) - 1:
                        flow += '<div class="flow-arrow">→</div>'
                st.markdown(f'<div class="flow-stream">{flow}</div>', unsafe_allow_html=True)
                evidence_tabs = st.tabs([
                    f"{event['stage'].title()} · {event['decision'].title()}"
                    for event in events
                ])
                for tab, event in zip(evidence_tabs, events):
                    with tab:
                        st.caption(event["created_at"])
                        render_event_evidence(event)
            else:
                st.caption("Trade has no recorded lifecycle events.")

            if trade["notes"]:
                st.write(trade["notes"])
            if trade["status"] == "OPEN":
                exit_price = st.number_input(
                    "Paper exit price", min_value=0.000001,
                    value=float(trade["current_price"] or trade["entry_price"] or 1),
                    key=f"exit_price_{trade['id']}",
                )
                if st.button("Close paper position", key=f"close_trade_{trade['id']}"):
                    close_paper_trade(int(trade["id"]), float(exit_price))
                    st.rerun()


def render_summary(summary: Dict[str, Any]) -> None:
    render_portfolio(summary)
    render_agent_cards(summary["agents"])

    st.subheader("Research headlines")
    if summary.get("news"):
        headlines = pd.DataFrame(summary["news"])
        visible_columns = [
            "title", "ticker", "source", "published_at", "relevance_status",
            "relevance_reason", "url",
        ]
        st.dataframe(
            headlines[visible_columns],
            hide_index=True,
            width="stretch",
            column_config={"url": st.column_config.LinkColumn("Article")},
        )
    else:
        st.info("No dynamic headlines in this range yet. The backend checks Yahoo Finance hourly; dummy research data remains available.")


def render_time_range() -> tuple[str, str]:
    now = datetime.now().replace(second=0, microsecond=0)
    default_start = now - timedelta(days=30)
    selected_start = st.session_state.get("dashboard_range_start", default_start)
    selected_end = st.session_state.get("dashboard_range_end", now)
    range_label = (
        f"Time range · {selected_start:%b %d %H:%M} – {selected_end:%b %d %H:%M}"
    )
    with st.expander(range_label, expanded=False):
        with st.form("dashboard_time_range"):
            start_col, end_col, apply_col = st.columns([2, 2, 1])
            with start_col:
                start_date = st.date_input("From date", value=selected_start.date(), key="range_start_date")
                start_time = st.time_input("From time", value=selected_start.time(), key="range_start_time")
            with end_col:
                end_date = st.date_input("To date", value=selected_end.date(), key="range_end_date")
                end_time = st.time_input("To time", value=selected_end.time(), key="range_end_time")
            with apply_col:
                apply_range = st.form_submit_button("Apply", type="primary")

        if "start_date" in locals():
            candidate_start = datetime.combine(start_date, start_time)
            candidate_end = datetime.combine(end_date, end_time)
            if candidate_start > candidate_end:
                st.error("Start must be earlier than end.")
            elif apply_range:
                st.session_state["dashboard_range_start"] = candidate_start
                st.session_state["dashboard_range_end"] = candidate_end
                selected_start = candidate_start
                selected_end = candidate_end

    start_utc = selected_start.astimezone(timezone.utc)
    end_utc = selected_end.astimezone(timezone.utc)
    return start_utc.strftime("%Y-%m-%d %H:%M:%S"), end_utc.strftime("%Y-%m-%d %H:%M:%S")


def refresh_dashboard_end_time() -> None:
    st.session_state["dashboard_range_end"] = datetime.now()


def render_trade_search(trades: list[Dict[str, Any]]) -> None:
    st.subheader("Trade lifecycles")
    search_col, status_col = st.columns([3, 1])
    with search_col:
        search_text = st.text_input("Search company, ticker, or decision", key="trade_search")
    with status_col:
        statuses = ["All"] + sorted({trade["status"] for trade in trades})
        selected_status = st.selectbox("Status", statuses)

    query = search_text.strip().casefold()
    filtered = [
        trade for trade in trades
        if (selected_status == "All" or trade["status"] == selected_status)
        and (
            not query
            or query in trade.get("company_name", "").casefold()
            or query in trade["symbol"].casefold()
            or query in trade["side"].casefold()
            or any(query in event["message"].casefold() for event in trade["events"])
        )
    ]
    st.caption(f"{len(filtered)} trade(s) in the selected time range")
    render_trade_lifecycles(filtered)


def refresh_stale_backend_import() -> None:
    if inspect.signature(get_dashboard_summary).parameters:
        return
    refreshed = importlib.reload(backend_module)
    for name in (
        "DEFAULT_AGENT_RULES", "add_user", "ensure_db", "authenticate_user",
        "close_paper_trade", "create_session_token", "get_dashboard_summary",
        "get_ai_research_enabled", "get_portfolio_summary", "revoke_session_token",
        "run_hourly_news_fetch", "run_trading_cycle", "set_ai_research_enabled",
        "set_starting_capital", "update_agent_rules", "validate_session_token",
    ):
        globals()[name] = getattr(refreshed, name)


def run_agent_cycle_for_ui() -> Dict[str, Any] | None:
    try:
        return asyncio.run(run_trading_cycle())
    except Exception as error:
        st.session_state["manual_research_error"] = str(error)
        return None


def main() -> None:
    refresh_stale_backend_import()
    ensure_db()
    cookie_manager = get_cookie_manager()

    if not st.session_state.authenticated:
        session_token = cookie_manager.get(SESSION_COOKIE) or st.context.cookies.get(SESSION_COOKIE)
        username = validate_session_token(session_token) if session_token else None
        if username:
            st.session_state.authenticated = True
            st.session_state.username = username
        elif not login_view(cookie_manager):
            st.stop()

    st.sidebar.title("Navigation")
    st.sidebar.write(f"HTTPS enabled: {'ON' if ENABLE_HTTPS else 'OFF'}")
    st.sidebar.write(f"Signed in as: {st.session_state.get('username', 'user')}")

    render_admin_panel()

    if st.sidebar.button("Logout"):
        session_token = cookie_manager.get(SESSION_COOKIE) or st.context.cookies.get(SESSION_COOKIE)
        if session_token:
            revoke_session_token(session_token)
        cookie_manager.delete(SESSION_COOKIE)
        st.session_state.authenticated = False
        st.session_state.pop("username", None)
        login_view(cookie_manager)
        st.stop()

    current_capital = get_portfolio_summary()["starting_capital"]
    with st.sidebar.expander("Portfolio settings"):
        capital = st.number_input("Starting capital", min_value=1.0, value=float(current_capital), step=500.0)
        if st.button("Save capital", key="save_starting_capital"):
            try:
                set_starting_capital(capital)
                st.rerun()
            except ValueError as error:
                st.error(str(error))

    with st.sidebar.expander("AI research settings", expanded=False):
        ai_enabled = get_ai_research_enabled()
        with st.form("ai_research_settings"):
            requested_ai_enabled = st.checkbox(
                "Use OpenAI to analyze research",
                value=ai_enabled,
                help="When enabled, the selected dummy signal and recent headlines are sent to the configured OpenAI-compatible endpoint.",
            )
            st.caption("Disabled uses the existing dummy research signal. Risk and execution gates remain deterministic.")
            if st.form_submit_button("Save AI setting"):
                try:
                    set_ai_research_enabled(requested_ai_enabled)
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))

    st.title("Trading Monitor")
    st.caption("Paper portfolio · simulated research and execution · no live brokerage orders")
    start_at, end_at = render_time_range()

    with st.sidebar.expander("Manual research actions", expanded=False):
        if st.button("Fetch news only", key="manual_fetch_news"):
            with st.spinner("Fetching screened Yahoo Finance headlines..."):
                news_result = asyncio.run(run_hourly_news_fetch())
            st.session_state["manual_research_result"] = {"news": news_result, "cycle": None}
            refresh_dashboard_end_time()
            st.rerun()

        if st.button("Run agent analysis", key="manual_agent_analysis"):
            with st.spinner("Running researcher, strategist, risk, and execution rules..."):
                cycle_result = run_agent_cycle_for_ui()
            if cycle_result:
                st.session_state["manual_research_result"] = {"news": None, "cycle": cycle_result}
            refresh_dashboard_end_time()
            st.rerun()

        if st.button("Fetch news + analyze", type="primary", key="manual_fetch_and_analyze"):
            with st.spinner("Fetching news and running the agent cycle..."):
                news_result = asyncio.run(run_hourly_news_fetch())
                cycle_result = run_agent_cycle_for_ui()
            st.session_state["manual_research_result"] = {"news": news_result, "cycle": cycle_result}
            refresh_dashboard_end_time()
            st.rerun()

    summary = get_dashboard_summary(start_at, end_at)

    manual_result = st.session_state.pop("manual_research_result", None)
    manual_error = st.session_state.pop("manual_research_error", None)
    if manual_error:
        st.error(f"Agent analysis failed: {manual_error}")
    if manual_result:
        with st.expander("Latest manual research run", expanded=True):
            news_result = manual_result["news"]
            cycle_result = manual_result["cycle"]
            if news_result is not None:
                st.write(
                    f"News fetch: {news_result['fetched']} headlines received, "
                    f"{news_result['inserted']} new and {news_result['updated']} existing articles refreshed."
                )
            if cycle_result:
                headlines = cycle_result["market_data"].get("recent_news", [])
                relevance = cycle_result["market_data"].get("headline_relevance", {})
                st.write(
                    f"Analyzed {len(headlines)} headlines: {relevance.get('relevant', 0)} relevant, "
                    f"{relevance.get('not_relevant', 0)} not relevant."
                )
                research_mode = "OpenAI research" if cycle_result["ai_research_enabled"] else "Dummy research"
                st.write(f"Research mode: {research_mode}")
                st.dataframe(pd.DataFrame(cycle_result["trades"]), hide_index=True, width="stretch")

    st.sidebar.subheader("Agent rule editor")
    for agent_name, default_rules in DEFAULT_AGENT_RULES.items():
        agent_rules = next((agent["rules"] for agent in summary.get("agents", []) if agent["agent_name"] == agent_name), default_rules)
        with st.sidebar.expander(agent_name.title()):
            with st.form(f"edit_rules_{agent_name}"):
                updated = {}
                for key, value in agent_rules.items():
                    if isinstance(value, bool):
                        updated[key] = st.checkbox(key, value=value)
                    elif isinstance(value, int):
                        updated[key] = st.number_input(key, value=int(value), step=1)
                    elif isinstance(value, float):
                        updated[key] = st.number_input(key, value=float(value), step=0.01)
                    else:
                        updated[key] = st.text_input(key, value=str(value))
                if st.form_submit_button(f"Save {agent_name} rules"):
                    update_agent_rules(agent_name, updated)
                    st.sidebar.success(f"{agent_name.title()} rules updated.")
                    st.rerun()

    overview_tab, lifecycle_tab = st.tabs(["Overview", "Trade lifecycles"])
    with overview_tab:
        render_summary(summary)
    with lifecycle_tab:
        render_trade_search(summary["trades"])


if __name__ == "__main__":
    main()
