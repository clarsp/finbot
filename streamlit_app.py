import asyncio
import html
import importlib
import inspect
import os
from datetime import datetime, time, timedelta
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
    get_dashboard_summary,
    get_portfolio_summary,
    revoke_session_token,
    run_trading_cycle,
    set_starting_capital,
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
        events = [event for trade in summary["trades"] for event in trade["events"]]
        if events:
            event_frame = pd.DataFrame(events)
            event_frame["created_at"] = pd.to_datetime(event_frame["created_at"])
            event_frame["accepted"] = (event_frame["decision"] == "accepted").astype(int)
            event_frame["rejected"] = (event_frame["decision"] == "rejected").astype(int)
            trend = event_frame.groupby(pd.Grouper(key="created_at", freq="15min"))[["accepted", "rejected"]].sum()
            st.line_chart(trend, color=["#22c55e", "#ef4444"])
        else:
            st.caption("Decision history will appear after the first research cycle.")


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
                for event in events:
                    st.caption(f"{event['created_at']} · {event['message']}")
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

    with st.expander("Latest research signal", expanded=False):
        if summary["latest_signal"]:
            st.json(summary["latest_signal"])
        else:
            st.caption("No market signals yet.")


def render_time_range() -> tuple[str, str]:
    now = datetime.now().replace(second=0, microsecond=0)
    default_start = now - timedelta(days=30)
    st.subheader("Dashboard time range")
    with st.form("dashboard_time_range"):
        start_col, end_col, apply_col = st.columns([2, 2, 1])
        with start_col:
            st.markdown("**From**")
            start_date = st.date_input("Start date", value=default_start.date(), key="range_start_date")
            start_time = st.time_input("Start time", value=time(0, 0), key="range_start_time")
        with end_col:
            st.markdown("**To**")
            end_date = st.date_input("End date", value=now.date(), key="range_end_date")
            end_time = st.time_input("End time", value=now.time(), key="range_end_time")
        with apply_col:
            st.markdown("&nbsp;", unsafe_allow_html=True)
            apply_range = st.form_submit_button("Apply range", type="primary")

    selected_start = datetime.combine(start_date, start_time)
    selected_end = datetime.combine(end_date, end_time)
    if selected_start > selected_end:
        st.error("The start time must be earlier than the end time.")
        selected_start = default_start
        selected_end = now
    if apply_range:
        st.session_state["dashboard_range_start"] = selected_start
        st.session_state["dashboard_range_end"] = selected_end

    selected_start = st.session_state.get("dashboard_range_start", selected_start)
    selected_end = st.session_state.get("dashboard_range_end", selected_end)
    return selected_start.strftime("%Y-%m-%d %H:%M:%S"), selected_end.strftime("%Y-%m-%d %H:%M:%S")


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
        "get_portfolio_summary", "revoke_session_token", "run_trading_cycle",
        "set_starting_capital", "update_agent_rules", "validate_session_token",
    ):
        globals()[name] = getattr(refreshed, name)


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

    st.title("Trading Monitor")
    st.caption("Paper portfolio · simulated research and execution · no live brokerage orders")
    start_at, end_at = render_time_range()

    if st.sidebar.button("Run research cycle", type="primary"):
        asyncio.run(run_trading_cycle())
        st.rerun()

    summary = get_dashboard_summary(start_at, end_at)

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
