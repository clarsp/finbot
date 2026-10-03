import asyncio
import os
from typing import Any, Dict

import pandas as pd
import streamlit as st

from makemoney import (
    DEFAULT_AGENT_RULES,
    add_user,
    ensure_db,
    authenticate_user,
    get_dashboard_summary,
    get_decision_timeline,
    run_trading_cycle,
    update_agent_rules,
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
        .sidebar .sidebar-content {
            background: #020817;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


if "authenticated" not in st.session_state:
    st.session_state.authenticated = False


def login_view() -> bool:
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
                st.session_state.authenticated = True
                st.rerun()
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


def render_agent_cards(summary: Dict[str, Any]) -> None:
    agents = summary.get("agents", [])
    if not agents:
        st.info("No agent events recorded yet.")
        return

    st.subheader("Agent status overview")
    cols = st.columns(4)

    for idx, agent in enumerate(agents[:4]):
        name = agent["agent_name"].title()
        status = str(agent["status"]).capitalize()
        accepted = agent["accepted_count"]
        rejected = agent["rejected_count"]
        action = agent["action"]
        decision = agent["decision"]

        status_color = {
            "Active": "#22c55e",
            "Warning": "#f59e0b",
            "Blocked": "#ef4444",
            "Idle": "#94a3b8",
        }.get(status, "#94a3b8")

        with cols[idx]:
            st.markdown(
                f"""
                <div class="agent-card">
                    <div class="agent-header">
                        <span class="agent-name">{name}</span>
                        <span class="agent-pill" style="background: {status_color};">{status}</span>
                    </div>
                    <div class="agent-stats">
                        <div><strong>{accepted}</strong><span>Accepted</span></div>
                        <div><strong>{rejected}</strong><span>Rejected</span></div>
                    </div>
                    <div class="agent-meta">Action: {action}</div>
                    <div class="agent-meta">Decision: {decision}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def render_decision_timeline() -> None:
    rows = get_decision_timeline()
    if not rows:
        st.info("No decision events recorded yet.")
        return

    df = pd.DataFrame(rows)
    df["created_at"] = pd.to_datetime(df["created_at"])
    df["accepted"] = (df["decision"] == "accepted").astype(int)
    df["rejected"] = (df["decision"] == "rejected").astype(int)

    st.subheader("Decision trend over time")
    hourly = (
        df.groupby([pd.Grouper(key="created_at", freq="5min"), "agent_name"])
        .agg(accepted=("accepted", "sum"), rejected=("rejected", "sum"))
        .reset_index()
    )
    if not hourly.empty:
        chart = hourly.pivot(index="created_at", columns="agent_name", values="accepted").fillna(0)
        st.line_chart(chart)
        st.bar_chart(hourly.pivot(index="agent_name", columns="created_at", values="accepted").fillna(0))


def render_agent_flow() -> None:
    st.subheader("Agent interaction flow")
    flow_lines = [
        ("Researcher", "Strategist", "accepted"),
        ("Strategist", "Risk Manager", "accepted"),
        ("Risk Manager", "Execution", "accepted"),
        ("Risk Manager", "Execution", "rejected"),
    ]

    flow_html = ""
    for source, target, decision in flow_lines:
        direction = "accepted" if decision == "accepted" else "rejected"
        flow_html += (
            f'<div class="flow-item {direction}">{source} <span style="color: #94a3b8;">→</span> {target}</div>'
        )

    st.markdown(f'<div class="flow-stream">{flow_html}</div>', unsafe_allow_html=True)

    st.caption("Green arrows represent accepted inputs or safe decisions; red arrows represent rejected or breached conditions.")


def render_summary(summary: Dict[str, Any]) -> None:
    st.subheader("Overview")
    render_agent_cards(summary)
    render_decision_timeline()
    render_agent_flow()

    st.subheader("Detailed signal and trade activity")
    if summary["latest_signal"]:
        with st.expander("Latest market signal", expanded=False):
            st.json(summary["latest_signal"])

    if summary["latest_trade"]:
        with st.expander("Latest trade", expanded=False):
            st.json(summary["latest_trade"])

    if summary["risk_events"]:
        with st.expander("Recent risk events", expanded=False):
            st.json(summary["risk_events"])


def main() -> None:
    ensure_db()

    if not st.session_state.authenticated:
        login_view()
        st.stop()

    st.sidebar.title("Navigation")
    st.sidebar.write(f"HTTPS enabled: {'ON' if ENABLE_HTTPS else 'OFF'}")
    st.sidebar.write("Default local login: admin / admin123")

    render_admin_panel()

    if st.sidebar.button("Logout"):
        st.session_state.authenticated = False
        st.rerun()

    st.title("Trading Monitor")
    st.caption("Local SQLite-backed MVP for testing and analysis.")

    summary = get_dashboard_summary()

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

    if st.button("Run analysis cycle"):
        cycle = asyncio.run(run_trading_cycle())
        st.session_state["last_cycle"] = cycle
        st.success("Trading cycle completed.")

    render_summary(summary)

    cycle = st.session_state.get("last_cycle")
    if cycle:
        st.subheader("Cycle output")
        st.json(cycle)


if __name__ == "__main__":
    main()
