import asyncio
import html
import importlib
import inspect
import os
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict

import extra_streamlit_components as stx
import integrations as integrations_module
import makemoney as backend_module
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

integrations_refresh_needed = not hasattr(integrations_module, "validate_trade_price_path")
if integrations_refresh_needed:
    integrations_module = importlib.reload(integrations_module)

if (
    integrations_refresh_needed
    or not hasattr(backend_module, "get_openai_usage_summary")
    or not hasattr(backend_module, "validate_trade_outcome")
    or not hasattr(backend_module, "get_company_analytics")
):
    backend_module = importlib.reload(backend_module)

from makemoney import (
    DEFAULT_AGENT_RULES,
    PROFILE_AVATARS,
    add_user,
    ensure_db,
    authenticate_user,
    close_paper_trade,
    create_session_token,
    get_ai_research_enabled,
    get_company_analytics,
    get_company_analysis_history,
    get_dashboard_summary,
    get_least_analyzed_companies,
    get_openai_usage_summary,
    get_portfolio_summary,
    get_research_schedule,
    get_user_profile,
    list_users,
    revoke_session_token,
    run_hourly_news_fetch,
    run_trading_cycle,
    set_starting_capital,
    set_ai_research_enabled,
    configure_research_schedule,
    change_user_password,
    delete_user,
    set_user_admin,
    update_user_avatar,
    suspend_research_schedule,
    sync_openai_token_allowance,
    update_agent_rules,
    validate_pending_trade_outcomes,
    validate_trade_outcome,
    validate_session_token,
)

st.set_page_config(page_title="Finbot Local MVP", layout="wide")
ENABLE_HTTPS = os.getenv("ENABLE_HTTPS", "false").lower() in {"1", "true", "yes", "on"}

AGENT_RULE_HELP = {
    "enabled": "Whether this agent participates in analysis cycles.",
    "min_confidence": "Minimum confidence, from 0.0 to 1.0, needed to pass this decision gate.",
    "allowed_sectors": "Research candidates outside these sectors are rejected.",
    "max_fetch_per_cycle": "Maximum number of unique feed candidates analyzed per cycle. The pool rotates across cycles; with OpenAI enabled, each candidate may use a research call and a separate risk trend-review call.",
    "include_market_fundamentals": "Fetch Yahoo Finance company statements for strategist checks and include them in OpenAI research when enabled.",
    "fundamental_confirmation_min_confidence": "Minimum confidence when at least three company metrics and a directly matched headline support the proposed direction.",
    "countertrend_short_min_confidence": "Minimum confidence for a short that conflicts with strong fundamentals or company-specific bullish news.",
    "countertrend_long_min_confidence": "Minimum confidence for a long that conflicts with weak fundamentals or company-specific bearish news.",
    "strong_fundamentals_min_revenue_growth_pct": "Revenue growth threshold for one of the metrics used to identify strong company fundamentals.",
    "strong_fundamentals_min_net_profit_margin_pct": "Net margin threshold for one of the metrics used to identify strong company fundamentals.",
    "strong_fundamentals_max_debt_to_equity": "Maximum debt/equity ratio for one of the metrics used to identify strong company fundamentals.",
    "weak_fundamentals_max_revenue_growth_pct": "Revenue growth at or below this value counts as one of five weak-company indicators; at least three are required.",
    "weak_fundamentals_max_net_profit_margin_pct": "Net margin at or below this value counts as one of five weak-company indicators; EPS and cash flow are also considered.",
    "weak_fundamentals_min_debt_to_equity": "Debt/equity at or above this value counts as one of five weak-company indicators.",
    "require_company_specific_news_for_fundamental_confirmation": "Require a company-specific headline in the same direction before lowering the confidence threshold for a fundamentals-aligned trade.",
    "require_company_specific_news_for_countertrend_short": "Require a directly company-linked bearish headline before shorting against strong fundamentals or bullish news.",
    "require_company_specific_news_for_countertrend_long": "Require a directly company-linked bullish headline before going long against weak fundamentals or bearish news.",
    "require_current_market_quote": "Block paper trade progression unless a current Yahoo Finance quote is available.",
    "max_leverage": "Upper leverage limit for the strategist's proposed position.",
    "position_limit": "Maximum fraction of starting capital allocated to one trade.",
    "max_drawdown": "Risk rejection threshold for modeled account drawdown.",
    "max_volatility": "Risk rejection threshold for modeled volatility.",
    "stop_loss_pct": "Default stop-loss distance from entry price as a fraction.",
    "exit_on_risk_breach": "Reject/exit if a configured risk threshold is breached. This safety gate is required.",
    "openai_trend_review_enabled": "After strategy approval, request an independent OpenAI review of Yahoo price trends over 1 hour, 24 hours, this week, this year, and 5 years. This uses additional API tokens.",
    "trend_opposition_min_confidence": "Reject a proposed position when the OpenAI trend review strongly opposes it at or above this confidence.",
    "paper_trading_only": "Execution remains simulated; live broker orders are not implemented.",
    "max_trade_value": "Maximum paper capital allocation for one trade.",
    "max_trades_per_cycle": "Maximum number of risk-approved candidates the paper execution stage may open in one cycle; highest direction-neutral evidence scores are selected first.",
    "allowed_side": "Trade directions permitted by paper execution.",
}

st.markdown(
    """
    <style>
        .stApp {
            background: #0f172a;
            color: #e2e8f0;
        }
        .main .block-container {
            padding-top: 0.25rem;
            padding-bottom: 2rem;
        }
        .main h1 {
            font-size: 1.45rem;
            margin: 0.1rem 0 0.35rem;
        }
        header[data-testid="stHeader"]::before {
            content: "Trading Monitor";
            position: absolute;
            left: 1rem;
            top: 0.55rem;
            color: #e2e8f0;
            font-size: 0.875rem;
            font-weight: 600;
            line-height: 2rem;
            pointer-events: none;
        }
        .st-key-app_header_actions {
            position: fixed;
            top: 0.25rem;
            right: 8rem;
            z-index: 1000000;
            width: 8rem;
        }
        .st-key-app_header_actions [data-testid="stHorizontalBlock"] {
            flex-wrap: nowrap;
            gap: 0.25rem;
        }
        .st-key-app_header_actions [data-testid="column"] {
            min-width: 0;
        }
        div[data-testid="stMetricLabel"] p {
            font-size: 0.72rem;
        }
        div[data-testid="stMetricValue"] {
            font-size: 1.15rem;
        }
        @media (min-width: 761px) {
            .st-key-dashboard_portfolio_figures,
            .st-key-dashboard_agent_figures {
                border-right: 1px solid rgba(148, 163, 184, 0.28);
                padding-right: 0.8rem;
            }
        }
        @media (max-width: 760px) {
            header[data-testid="stHeader"]::before {
                left: 0.5rem;
                font-size: 0.75rem;
            }
            .st-key-app_header_actions {
                right: 6.5rem;
                width: 7rem;
            }
            .st-key-dashboard_portfolio_figures,
            .st-key-dashboard_agent_figures {
                border-right: 0;
                border-bottom: 1px solid rgba(148, 163, 184, 0.28);
                padding: 0 0 0.65rem;
            }
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


def render_header_actions(cookie_manager) -> None:
    with st.container(key="app_header_actions"):
        profile_col, menu_col = st.columns([1, 1], gap="small")
        with profile_col:
            current_user = get_user_profile(st.session_state.get("username", "")) or {}
            avatar_icon = current_user.get("avatar", "account_circle")
            with st.popover("Profile", icon=f":material/{avatar_icon}:", help="Account profile and logout"):
                st.caption(f"Signed in as {st.session_state.get('username', 'user')}")
                if st.button("Open profile", key="open_profile_from_header"):
                    st.session_state["main_navigation"] = "Profile"
                    st.rerun()
                if st.button("Logout", icon=":material/logout:", key="header_logout"):
                    session_token = cookie_manager.get(SESSION_COOKIE) or st.context.cookies.get(SESSION_COOKIE)
                    if session_token:
                        revoke_session_token(session_token)
                    cookie_manager.delete(SESSION_COOKIE)
                    st.session_state.authenticated = False
                    st.session_state.pop("username", None)
                    st.rerun()
        with menu_col:
            with st.popover("More", icon=":material/more_vert:", help="Administration"):
                st.caption(f"HTTPS enabled: {'ON' if ENABLE_HTTPS else 'OFF'}")
                current_user = get_user_profile(st.session_state.get("username", "")) or {}
                if current_user.get("is_admin") and st.button("Manage users", key="manage_users_from_header"):
                    st.session_state["main_navigation"] = "Profile"
                    st.rerun()


def render_agent_cards(agents: list[Dict[str, Any]]) -> None:
    st.markdown("#### Agent decisions")
    if not agents:
        st.caption("No agent data for this time range.")
        return

    columns = st.columns(2, gap="small")
    for index, agent in enumerate(agents):
        with columns[index % 2]:
            st.metric(
                agent["agent_name"].replace("_", " ").title(),
                f"{agent['accepted_count']:,} / {agent['rejected_count']:,}",
            )
            st.caption(f"Accepted / rejected · {agent['status'].title()}")


@st.fragment(run_every="10s")
def render_openai_usage() -> None:
    usage = get_openai_usage_summary()
    latest = usage.get("latest")
    st.markdown("#### OpenAI usage")

    token_metrics = st.columns(2, gap="small")
    token_metrics[0].metric("Tokens this period", f"{usage['used']:,}")
    token_metrics[1].metric("Latest request", f"{latest['total_tokens']:,}" if latest else "—")

    if usage["synced_at"]:
        synced_at = datetime.fromisoformat(usage["synced_at"]).astimezone()
        period_label = f"Period started {synced_at:%b %d %H:%M %Z}"
    else:
        period_label = "Usage period not synchronized"
    request_tokens = (
        f"Prompt {latest['prompt_tokens']:,} · completion {latest['completion_tokens']:,}"
        if latest else "Usage details appear after the first API response."
    )
    st.caption(f"{period_label} · {request_tokens}")

    rate_limits = latest.get("rate_limits", {}) if latest else {}
    rate_metrics = st.columns(2, gap="small")
    for column, kind, title in (
        (rate_metrics[0], "requests", "Requests remaining"),
        (rate_metrics[1], "tokens", "Tokens remaining"),
    ):
        remaining = rate_limits.get(f"x-ratelimit-remaining-{kind}")
        limit = rate_limits.get(f"x-ratelimit-limit-{kind}")
        reset = rate_limits.get(f"x-ratelimit-reset-{kind}")
        if remaining is not None:
            try:
                display_value = f"{int(remaining):,} / {int(limit):,}" if limit is not None else f"{int(remaining):,}"
            except (TypeError, ValueError):
                display_value = str(remaining)
        else:
            display_value = "—"
        column.metric(title, display_value)
        if reset:
            column.caption(f"Reset {reset}")
    if latest:
        st.caption(f"Latest response · {latest['model']} · {latest['created_at']}")

    if usage["history"]:
        with st.expander("Token usage history", expanded=False):
            st.dataframe(
                pd.DataFrame(usage["history"]).rename(columns={
                    "period_started_at": "Period started",
                    "period_ended_at": "Synchronized at",
                    "tokens_used": "Tokens used",
                }),
                hide_index=True,
                width="stretch",
            )


def render_research_actions() -> None:
    schedule = get_research_schedule()
    with st.sidebar.expander("Research Actions", expanded=False):
        mode_options = ["Manual", "Automatic"]
        current_mode = schedule["mode"].title()
        with st.form("research_schedule_settings"):
            requested_mode = st.radio(
                "Research mode", mode_options,
                index=mode_options.index(current_mode) if current_mode in mode_options else 0,
                horizontal=True,
            )
            interval = st.number_input(
                "Automatic schedule interval (minutes)",
                min_value=1,
                max_value=1440,
                value=schedule["interval_minutes"],
                step=15,
                disabled=requested_mode != "Automatic",
            )
            if st.form_submit_button("Save research mode"):
                configure_research_schedule(requested_mode.lower(), int(interval))
                st.rerun()

        if schedule["mode"] == "automatic":
            next_run = datetime.fromisoformat(schedule["next_run"]).astimezone()
            st.caption(f"Schedule: every {schedule['interval_minutes']} minutes")
            st.caption(f"Next run: {next_run:%Y-%m-%d %H:%M:%S %Z}")
            if schedule["last_run"]:
                last_run = datetime.fromisoformat(schedule["last_run"]).astimezone()
                st.caption(f"Last run: {last_run:%Y-%m-%d %H:%M:%S %Z}")
            if schedule["suspended"]:
                resume_at = datetime.fromisoformat(schedule["suspended_until"]).astimezone()
                st.warning(f"Suspended until {resume_at:%Y-%m-%d %H:%M:%S %Z}")
            with st.form("suspend_research_schedule"):
                resume_date = st.date_input("Resume date")
                resume_time = st.time_input("Resume time")
                suspend_col, resume_col = st.columns(2)
                suspend = suspend_col.form_submit_button("Suspend until")
                resume = resume_col.form_submit_button("Resume now")
            if suspend:
                local_resume = datetime.combine(resume_date, resume_time).astimezone()
                if local_resume <= datetime.now().astimezone():
                    st.error("Choose a resume date and time in the future.")
                else:
                    suspend_research_schedule(local_resume.astimezone(timezone.utc).isoformat())
                    st.rerun()
            if resume:
                suspend_research_schedule(None)
                st.rerun()
            if st.button("Stop automatic schedule", key="stop_research_schedule"):
                configure_research_schedule("manual", schedule["interval_minutes"])
                suspend_research_schedule(None)
                st.rerun()
        else:
            st.caption("Automatic research is stopped. Manual actions are available below.")
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


def render_agent_decision_trend(events: list[Dict[str, Any]]) -> None:
    stage_order = ["researcher", "strategist", "risk_manager", "execution"]
    stage_titles = ["Researcher", "Strategist", "Risk Manager", "Execution"]
    figure = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        subplot_titles=stage_titles,
        vertical_spacing=0.07,
    )
    frame = pd.DataFrame(events)
    if not frame.empty:
        frame = frame[frame["decision"].isin(["accepted", "rejected"])].copy()
        frame["created_at"] = pd.to_datetime(frame["created_at"])
        frame["count"] = 1
        for row, agent in enumerate(stage_order, start=1):
            subset = frame[frame["agent_name"] == agent]
            for decision, color, dash in (
                ("accepted", "#22c55e", "solid"),
                ("rejected", "#ef4444", "dash"),
            ):
                counts = (
                    subset[subset["decision"] == decision]
                    .groupby(pd.Grouper(key="created_at", freq="15min"))["count"]
                    .sum()
                )
                if counts.empty:
                    continue
                figure.add_trace(
                    go.Scatter(
                        x=counts.index,
                        y=counts.values,
                        mode="lines+markers",
                        name=decision.title(),
                        legendgroup=decision,
                        showlegend=row == 1,
                        line={"color": color, "dash": dash},
                        hovertemplate=f"Agent: {stage_titles[row - 1]}<br>Decision: {decision.title()}<br>Time: %{{x}}<br>Count: %{{y}}<extra></extra>",
                    ),
                    row=row,
                    col=1,
                )
    figure.update_layout(
        height=540,
        margin={"l": 10, "r": 10, "t": 35, "b": 15},
        legend={"orientation": "h", "y": 1.04, "x": 1, "xanchor": "right"},
        hovermode="x unified",
    )
    figure.update_yaxes(title_text="Count", rangemode="tozero")
    figure.update_xaxes(title_text="Time", row=4, col=1)
    st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})


def render_portfolio(summary: Dict[str, Any]) -> None:
    portfolio = summary["portfolio"]
    open_trades = [trade for trade in summary["trades"] if trade["status"] == "OPEN"]
    st.markdown("#### Portfolio")
    metrics = st.columns(2, gap="small")
    values = (
        ("Starting capital", f"${portfolio['starting_capital']:,.2f}"),
        ("Available cash", f"${portfolio['cash_available']:,.2f}"),
        ("Invested", f"${portfolio['invested_capital']:,.2f}"),
        ("Gross exposure", f"${portfolio['gross_exposure']:,.2f}"),
        ("Net equity", f"${portfolio['account_equity']:,.2f}"),
        ("Open positions", f"{len(open_trades):,}"),
    )
    for index, (label, value) in enumerate(values):
        with metrics[index % 2]:
            st.metric(label, value)


def render_portfolio_charts(summary: Dict[str, Any]) -> None:
    open_trades = [trade for trade in summary["trades"] if trade["status"] == "OPEN"]
    chart_col, trend_col = st.columns([1, 2])
    with chart_col:
        st.markdown("#### Capital by open position")
        if open_trades:
            exposure = pd.DataFrame(open_trades).groupby("symbol")["allocated_capital"].sum()
            st.bar_chart(exposure, color="#38bdf8")
        else:
            st.caption("No open positions yet.")
    with trend_col:
        st.markdown("#### Decisions over time · by agent")
        events = [
            event for event in summary.get("decision_timeline", [])
            if event["decision"] in {"accepted", "rejected"}
        ]
        if events:
            render_agent_decision_trend(events)
        else:
            st.caption("Agent decision history will appear after the first research cycle.")


def render_event_evidence(event: Dict[str, Any]) -> None:
    details = event.get("details") or {}
    st.markdown(f"**Outcome: {event['decision'].title()}**")
    st.write(event["message"])

    decision_data = {
        key: value for key, value in details.items()
        if key not in {"related_news", "news_context", "ai_analysis", "trend_review", "validation_type"}
    }
    if decision_data:
        with st.expander("Decision inputs and rule checks", expanded=False):
            st.json(decision_data)

    headlines = details.get("related_news") or details.get("news_context") or []
    if headlines:
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
        with st.expander(f"Headline evidence · {len(headlines)} article(s)", expanded=False):
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
        st.write(
            f"OpenAI · {str(ai_analysis.get('signal', 'unknown')).title()} · "
            f"{float(ai_analysis.get('confidence', 0)):.0%} confidence · "
            f"{ai_analysis.get('catalyst', 'No catalyst returned')}"
        )
        with st.expander("OpenAI research details", expanded=False):
            st.write(ai_analysis.get("summary", "No summary returned."))
            fundamentals = {
                key: "Unavailable" if value is None else value
                for key, value in (ai_analysis.get("fundamentals") or {}).items()
            }
            st.json({
                "provider": ai_analysis.get("provider"),
                "model": ai_analysis.get("model"),
                "fundamentals": fundamentals,
                "fundamental_analysis": ai_analysis.get("fundamental_analysis"),
                "fundamental_grade": ai_analysis.get("fundamental_grade", "unknown"),
                "fundamental_grades": ai_analysis.get("fundamental_grades", {}),
            })
    elif "ai_analysis" in details:
        st.caption("OpenAI research was disabled for this cycle; the decision used the configured rules and dummy baseline.")

    trend_review = details.get("trend_review")
    if trend_review:
        st.markdown("**OpenAI market-trend risk review**")
        if trend_review.get("status") != "complete":
            st.caption(trend_review.get("reason", "Trend review unavailable; deterministic risk checks remained active."))
        else:
            st.write(
                f"Overall: {trend_review['overall_direction'].title()} · "
                f"{trend_review['confidence']:.0%} confidence · {trend_review['rationale']}"
            )
            windows = trend_review.get("market_trends", {}).get("windows", {})
            assessments = trend_review.get("horizon_analysis", {})
            horizon_labels = {
                "last_1h": "Last hour",
                "last_24h": "Last 24 hours",
                "week_to_date": "This week",
                "year_to_date": "This year",
                "last_5_years": "Last 5 years",
            }
            trend_rows = []
            for horizon, label in horizon_labels.items():
                window = windows.get(horizon, {})
                assessment = assessments.get(horizon, {})
                change = window.get("change_pct")
                trend_rows.append({
                    "Period": label,
                    "Price change": f"{change:+.2f}%" if change is not None else "Unavailable",
                    "Trend": assessment.get("direction", "unknown").title(),
                    "Confidence": f"{assessment.get('confidence', 0):.0%}",
                    "Evidence": assessment.get("rationale", window.get("reason", "")),
                })
            st.dataframe(pd.DataFrame(trend_rows), hide_index=True, width="stretch")

    if details.get("validation_type") == "historical_trade_outcome":
        st.markdown("**Historical trade-path validation**")
        if not details.get("available"):
            st.warning(details.get("reason", "Historical price data is unavailable."))
        else:
            if details.get("validation_scope") == "counterfactual_opportunity":
                st.warning(
                    f"Counterfactual opportunity: this candidate was not executed; it was stopped at the "
                    f"{details.get('decision_stage', 'decision')} stage ({details.get('trade_status_at_validation', 'rejected')})."
                )
            else:
                st.caption("Executed simulated paper trade; this check replays the post-entry price path.")
            outcome_col, tp_col, sl_col, hold_col = st.columns(4)
            outcome_col.metric("First outcome", details["first_exit"].replace("_", " ").title())
            outcome_col.caption(details.get("first_exit_at") or "No stop or target hit")
            tp_col.metric("Take-profit return", f"{details['take_profit_return_pct']:+.2f}%")
            sl_col.metric("Stop-loss return", f"{details['stop_loss_return_pct']:+.2f}%")
            held_return = details.get("return_if_held_to_window_end_pct")
            hold_col.metric("Held to window end", f"{held_return:+.2f}%" if held_return is not None else "Unavailable")
            if details.get("first_exit", "").startswith("ambiguous_same_bar"):
                st.warning("Both stop and target were inside the same 5-minute bar. Order is unknowable; validation conservatively assumes stop-loss first.")
            if details.get("first_exit") == "take_profit":
                if details.get("stop_loss_breached_after_take_profit"):
                    extension_text = (
                        "The original stop level was later crossed after take-profit. "
                        f"Holding until that stop would have returned {details['return_if_held_to_original_stop_after_target_pct']:+.2f}%."
                    )
                else:
                    extension_text = "The original stop level was not crossed after take-profit within the sampled window."
                st.write(extension_text)
            st.caption(
                f"Sampled {details['bars_analyzed']:,} five-minute bars from {details['placed_at']} "
                f"through {details['sampled_through']}. "
                f"{'Full 14-day window.' if details['window_complete'] else 'Window is still in progress.'} "
                "Historical path only; excludes fees, slippage, spreads, and live fill ordering."
            )


def render_trade_processing_chart(trades: list[Dict[str, Any]]) -> None:
    agent_rows = {
        "researcher": "Researcher",
        "strategist": "Strategist",
        "risk_manager": "Risk Manager",
        "execution": "Execution",
    }
    y_order = ["Execution", "Risk Manager", "Strategist", "Researcher"]
    trade_labels = {}
    plotted_events = []

    for trade in sorted(trades, key=lambda item: (item["created_at"], item["id"])):
        created_at = pd.to_datetime(trade["created_at"]).strftime("%b %d %H:%M")
        trade_label = f"#{trade['id']} · {created_at} · {trade['symbol']}"
        trade_labels[trade["id"]] = trade_label
        latest_by_agent = {}
        for event in trade["events"]:
            stage = agent_rows.get(event["agent_name"])
            if not stage or event["status"] == "skipped" or event["decision"] not in {"accepted", "rejected"}:
                continue
            latest_by_agent[event["agent_name"]] = event

        stages = []
        for agent_name, event in latest_by_agent.items():
            stage = agent_rows[agent_name]
            stages.append((y_order.index(stage), stage, event))
        stages.sort(key=lambda item: item[0], reverse=True)
        for _, stage, event in stages:
            details = event.get("details") or {}
            hover_parts = [
                f"<b>Trade {trade['id']} · {html.escape(trade.get('company_name') or trade['symbol'])} ({html.escape(trade['symbol'])})</b>",
                f"<b>{html.escape(stage)} · {html.escape(event['decision'].title())}</b>",
                html.escape(event["message"]),
            ]
            headlines = details.get("related_news") or details.get("news_context") or []
            for headline in headlines[:5]:
                title = html.escape(str(headline.get("title", "")))
                relevance = html.escape(str(headline.get("relevance_status", "")))
                reason = html.escape(str(headline.get("relevance_reason", "")))
                url = html.escape(str(headline.get("url", "")), quote=True)
                source = html.escape(str(headline.get("source", "")))
                hover_parts.append(f"Headline ({source}, {relevance}): {title}. {reason} <a href='{url}'>Article</a>")
            ai_analysis = details.get("ai_analysis")
            if ai_analysis:
                hover_parts.append(
                    "OpenAI: " + html.escape(
                        f"{ai_analysis.get('signal')} ({ai_analysis.get('confidence')}); {ai_analysis.get('summary')}"
                    )
                )
            if details.get("thresholds"):
                hover_parts.append("Risk thresholds: " + html.escape(str(details["thresholds"])))
            plotted_events.append({
                "trade_id": trade["id"],
                "x": trade_label,
                "y": stage,
                "decision": event["decision"],
                "letter": "A" if event["decision"] == "accepted" else "R",
                "hover": "<br><br>".join(hover_parts),
            })

    if not plotted_events:
        st.info("No accepted or rejected trade-stage decisions in this time range.")
        return

    figure = go.Figure()
    for trade_id, trade_label in trade_labels.items():
        trade_points = [point for point in plotted_events if point["trade_id"] == trade_id]
        if len(trade_points) > 1:
            figure.add_trace(go.Scatter(
                x=[point["x"] for point in trade_points],
                y=[point["y"] for point in trade_points],
                mode="lines",
                line={"color": "#475569", "width": 1},
                hoverinfo="skip",
                showlegend=False,
            ))

    for decision, color in (("accepted", "#22c55e"), ("rejected", "#ef4444")):
        points = [point for point in plotted_events if point["decision"] == decision]
        if points:
            figure.add_trace(go.Scatter(
                x=[point["x"] for point in points],
                y=[point["y"] for point in points],
                mode="markers+text",
                name="Accepted (A)" if decision == "accepted" else "Rejected (R)",
                text=[point["letter"] for point in points],
                textposition="middle center",
                textfont={"color": "#ffffff", "size": 12},
                customdata=[point["hover"] for point in points],
                marker={"symbol": "square", "size": 34, "color": color, "line": {"color": "#0f172a", "width": 1}},
                hovertemplate="%{customdata}<extra></extra>",
            ))

    figure.update_layout(
        title="Trade processing by agent",
        height=max(380, min(760, 330 + 22 * len(trade_labels))),
        margin={"l": 20, "r": 20, "t": 50, "b": 90},
        hovermode="closest",
        legend={"orientation": "h", "y": 1.08, "x": 1, "xanchor": "right"},
        xaxis={"title": "Trade number · research time · ticker", "type": "category", "categoryorder": "array", "categoryarray": list(trade_labels.values()), "tickangle": -35},
        yaxis={"title": "Agent stage", "type": "category", "categoryorder": "array", "categoryarray": y_order},
    )
    st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})


def render_trade_agent_flow(trades: list[Dict[str, Any]]) -> None:
    st.subheader("Trade processing by agent")
    agent_rows = {
        "researcher": "Researcher",
        "strategist": "Strategist",
        "risk_manager": "Risk Manager",
        "execution": "Execution",
    }
    plotted = []
    trade_labels = []

    for trade in sorted(trades, key=lambda item: (item["created_at"], item["id"])):
        execution_event = next(
            (event for event in reversed(trade["events"]) if event["agent_name"] == "execution" and event["decision"] in {"accepted", "rejected"}),
            None,
        )
        time_value = execution_event["created_at"] if execution_event else trade["created_at"]
        label = f"#{trade['id']} · {pd.to_datetime(time_value).strftime('%b %d %H:%M')} · {trade['symbol']}"
        trade_labels.append(label)

        latest_by_agent = {}
        for event in trade["events"]:
            if event["agent_name"] not in agent_rows or event["status"] == "skipped":
                continue
            if event["decision"] in {"accepted", "rejected"}:
                latest_by_agent[event["agent_name"]] = event

        ordered_events = [
            (agent, latest_by_agent[agent])
            for agent in ("researcher", "strategist", "risk_manager", "execution")
            if agent in latest_by_agent
        ]
        for agent, event in ordered_events:
            details = event.get("details") or {}
            hover_parts = [
                f"<b>Trade #{trade['id']} · {html.escape(trade.get('company_name') or trade['symbol'])} ({html.escape(trade['symbol'])})</b>",
                f"{html.escape(event['created_at'])} · {html.escape(agent_rows[agent])} · {html.escape(event['decision'].title())}",
                html.escape(event["message"]),
            ]
            for headline in (details.get("related_news") or details.get("news_context") or [])[:5]:
                title = html.escape(str(headline.get("title", "")))
                reason = html.escape(str(headline.get("relevance_reason", "")))
                source = html.escape(str(headline.get("source", "")))
                hover_parts.append(f"Headline · {source} · {reason}: {title}")
            ai = details.get("ai_analysis")
            if ai:
                hover_parts.append("OpenAI · " + html.escape(str(ai.get("summary", ""))))
            if details.get("thresholds"):
                hover_parts.append("Risk thresholds · " + html.escape(str(details["thresholds"])))
            plotted.append({
                "trade_id": trade["id"],
                "trade_label": label,
                "agent": agent_rows[agent],
                "decision": event["decision"],
                "mark": "A" if event["decision"] == "accepted" else "R",
                "hover": "<br><br>".join(hover_parts),
            })

    if not plotted:
        st.info("No decisions to plot for this range.")
        return

    figure = go.Figure()
    for label in trade_labels:
        points = [item for item in plotted if item["trade_label"] == label]
        if len(points) > 1:
            figure.add_trace(go.Scatter(
                x=[label] * len(points),
                y=[item["agent"] for item in points],
                mode="lines",
                line={"color": "#475569", "width": 1},
                hoverinfo="skip",
                showlegend=False,
            ))

    for decision, color, marker_text in (
        ("accepted", "#22c55e", "A · Accepted"),
        ("rejected", "#ef4444", "R · Rejected"),
    ):
        points = [item for item in plotted if item["decision"] == decision]
        if points:
            figure.add_trace(go.Scatter(
                x=[item["trade_label"] for item in points],
                y=[item["agent"] for item in points],
                mode="markers+text",
                name=marker_text,
                text=[item["mark"] for item in points],
                textposition="middle center",
                textfont={"color": "white", "size": 12},
                customdata=[item["hover"] for item in points],
                marker={"symbol": "square", "size": 34, "color": color, "line": {"color": "#0f172a", "width": 1}},
                hovertemplate="%{customdata}<extra></extra>",
            ))

    figure.update_layout(
        height=max(390, min(760, 330 + 24 * len(trade_labels))),
        margin={"l": 30, "r": 20, "t": 20, "b": 110},
        hovermode="closest",
        legend={"orientation": "h", "y": 1.08, "x": 1, "xanchor": "right"},
        xaxis={"title": "Trade number · execution time · ticker", "type": "category", "categoryorder": "array", "categoryarray": trade_labels, "tickangle": -35},
        yaxis={"title": "Agent", "type": "category", "categoryorder": "array", "categoryarray": ["Execution", "Risk Manager", "Strategist", "Researcher"]},
    )
    st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})


def render_trade_validation_chart(trades: list[Dict[str, Any]]) -> None:
    points = []
    latest_validation_by_trade = {}
    for trade in trades:
        validations = [
            event for event in trade["events"]
            if event.get("details", {}).get("validation_type") == "historical_trade_outcome"
        ]
        if not validations:
            continue
        validation = validations[-1]["details"]
        latest_validation_by_trade[trade["id"]] = validation
        if not validation.get("available"):
            continue
        simulated_result = validation.get("simulated_barrier_exit_return_pct")
        planned_target = validation.get("take_profit_return_pct")
        if simulated_result is None or planned_target is None:
            continue
        scope = "Executed paper" if validation.get("validation_scope") == "executed_paper_trade" else "Counterfactual"
        points.append({
            "label": f"#{trade['id']} · {trade['symbol']}",
            "planned_target": planned_target,
            "historical_result": simulated_result,
            "hover": (
                f"{scope} · {trade['side']} · {validation.get('first_exit', 'unknown')} · "
                f"sampled through {validation.get('sampled_through', 'unknown')}"
            ),
        })

    st.subheader("Historical validation: planned vs result")
    if not points:
        if latest_validation_by_trade:
            unavailable_count = sum(
                not result.get("available")
                for result in latest_validation_by_trade.values()
            )
            latest_reason = next(
                (
                    result.get("reason") for result in latest_validation_by_trade.values()
                    if not result.get("available") and result.get("reason")
                ),
                "Historical prices are unavailable for the selected validation window.",
            )
            st.warning(
                f"No chartable outcomes yet: {unavailable_count} of "
                f"{len(latest_validation_by_trade)} validated trade(s) lack usable price bars. {latest_reason}"
            )
        else:
            st.info("No successful historical validations in this dashboard time range yet. Use an individual or batch validation action; the chart appears when usable price history is returned.")
        return

    figure = go.Figure()
    for key, name, color in (
        ("planned_target", "Planned take-profit", "#38bdf8"),
        ("historical_result", "Historical barrier result", "#22c55e"),
    ):
        figure.add_trace(go.Bar(
            x=[point["label"] for point in points],
            y=[point[key] for point in points],
            name=name,
            marker_color=color,
            customdata=[point["hover"] for point in points],
            hovertemplate="%{customdata}<br><b>%{fullData.name}:</b> %{y:+.2f}%<extra></extra>",
        ))
    figure.add_hline(y=0, line_color="#94a3b8", line_width=1)
    figure.update_layout(
        title="Planned target vs historical result",
        barmode="group",
        height=360,
        margin={"l": 20, "r": 20, "t": 45, "b": 90},
        xaxis={"title": "Trade", "type": "category", "tickangle": -35},
        yaxis_title="Return from entry (%)",
        legend={"orientation": "h", "y": 1.12, "x": 1, "xanchor": "right"},
    )
    st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
    st.caption(
        "**Blue bars** = Planned target return you set. **Green bars** = Actual result from historical price data "
        "(what would have happened). Rejected/deferred trades are counterfactual outcomes. Results apply stop-loss and "
        "take-profit rules to Yahoo 5-minute bars; they are not actual fills or net returns."
    )


def render_trade_lifecycles(trades: list[Dict[str, Any]]) -> None:
    if not trades:
        st.info("No research cycles recorded yet. Run a cycle to create the first trade lifecycles.")
        return
    for trade in trades:
        company_name = trade.get("company_name") or trade["symbol"]
        label = f"#{trade['id']} · {company_name} ({trade['symbol']}) · {trade['side']} · {trade['status']}"
        with st.expander(label, expanded=trade["status"] == "OPEN"):
            validation_statuses = {
                "OPEN", "CLOSED", "REJECTED_RESEARCH", "REJECTED_STRATEGY",
                "REJECTED_RISK", "REJECTED_EXECUTION", "DEFERRED_CAPACITY",
            }
            has_reference_prices = all(
                trade.get(key) is not None
                for key in ("entry_price", "stop_loss_price", "take_profit_price")
            )
            if trade["status"] in validation_statuses and has_reference_prices:
                has_validation = any(
                    event.get("details", {}).get("validation_type") == "historical_trade_outcome"
                    for event in trade["events"]
                )
                validation_label = "Revalidate 14-day path" if has_validation else "Validate 14-day path"
                if st.button(validation_label, key=f"validate_trade_{trade['id']}"):
                    try:
                        with st.spinner("Checking Yahoo 5-minute price history, up to 14 days..."):
                            validation = asyncio.run(validate_trade_outcome(int(trade["id"]), max_days=14))
                        if validation.get("available"):
                            st.success(f"✓ Validation complete: {validation.get('first_exit', 'N/A')} exit over {validation.get('window_days', 14)} days ({validation.get('bars_analyzed', 0)} bars).")
                        else:
                            st.warning(validation.get("reason", "Historical validation is unavailable."))
                    except Exception as e:
                        st.error(f"Validation failed: {str(e)}")
                    st.rerun()

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
    with st.container(key="dashboard_figures_band"):
        portfolio_col, agents_col, openai_col = st.columns(3, gap="small", vertical_alignment="top")
        with portfolio_col:
            with st.container(key="dashboard_portfolio_figures"):
                render_portfolio(summary)
        with agents_col:
            with st.container(key="dashboard_agent_figures"):
                render_agent_cards(summary["agents"])
        with openai_col:
            render_openai_usage()


def render_graphs(summary: Dict[str, Any]) -> None:
    st.divider()
    render_portfolio_charts(summary)
    filter_col, count_col = st.columns([2, 1])
    statuses = ["All"] + sorted({trade["status"] for trade in summary["trades"]})
    selected_status = filter_col.selectbox("Trade status", statuses, key="trade_flow_status")
    entry_limit = count_col.selectbox(
        "Trades shown", [24, 48, 96, "All"], index=0, key="trade_flow_limit",
        help="The chart uses the selected dashboard time range and shows the most recent entries.",
    )
    trades = sorted(
        [
            trade for trade in summary["trades"]
            if selected_status == "All" or trade["status"] == selected_status
        ],
        key=lambda trade: (trade["created_at"], trade["id"]),
        reverse=True,
    )
    if entry_limit != "All":
        trades = trades[:entry_limit]
    render_trade_agent_flow(trades)
    st.divider()
    render_trade_validation_chart(summary["trades"])


def render_research_headlines(summary: Dict[str, Any]) -> None:
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


def render_companies_dashboard() -> None:
    st.subheader("Companies Analytics")
    st.caption("Aggregated analysis history and performance per company")
    
    col1, col2 = st.columns([3, 1])
    with col1:
        days_filter = st.slider("Show analytics from last N days", min_value=1, max_value=365, value=30)
    with col2:
        sort_by = st.selectbox("Sort by", ["Times Analyzed", "Acceptance Rate", "Avg Confidence"])
    
    analytics = get_company_analytics(days=days_filter)
    
    if not analytics:
        st.info("No company analytics available yet. Run trading cycles to gather data.")
        return
    
    df = pd.DataFrame(analytics)
    df["Times Analyzed"] = df["times_analyzed"]
    df["Acceptance Rate"] = (df["acceptance_rate"] * 100).round(1).astype(str) + "%"
    df["Avg Confidence"] = (df["avg_confidence"] * 100).round(0).astype(int).astype(str) + "%"
    df["Trades"] = df["trade_count"].astype(int)
    df["Accepted"] = df["trades_accepted"].astype(int)
    df["Last Signal"] = df["last_signal"]
    df["Last Analyzed"] = pd.to_datetime(df["last_analyzed_at"]).dt.strftime("%Y-%m-%d %H:%M")
    
    sort_map = {
        "Times Analyzed": ("times_analyzed", False),
        "Acceptance Rate": ("acceptance_rate", False),
        "Avg Confidence": ("avg_confidence", False),
    }
    sort_col, ascending = sort_map[sort_by]
    df = df.sort_values(by=sort_col, ascending=ascending)
    
    display_cols = ["ticker", "company_name", "sector", "Times Analyzed", "Trades", "Accepted", 
                    "Acceptance Rate", "Avg Confidence", "Last Signal", "Last Analyzed"]
    
    st.dataframe(
        df[display_cols],
        hide_index=True,
        width="stretch",
        height=400,
    )
    
    st.divider()
    st.subheader("Company Deep Dive")
    
    col1, col2 = st.columns([2, 1])
    with col1:
        selected_ticker = st.selectbox(
            "Select a company to view analysis history",
            options=[row["ticker"] for row in analytics],
            format_func=lambda x: next((f"{r['ticker']} - {r['company_name']}" for r in analytics if r["ticker"] == x), x),
        )
    with col2:
        history_days = st.slider("Show history from last N days", min_value=1, max_value=365, value=30, key="history_days")
    
    if selected_ticker:
        history = get_company_analysis_history(selected_ticker, days=history_days)
        
        if history["company"]:
            company = history["company"]
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Times Analyzed", company["times_analyzed"])
            col2.metric("Avg Confidence", f"{company['avg_confidence']:.0%}")
            col3.metric("Acceptance Rate", f"{company['acceptance_rate']:.0%}")
            col4.metric("Trades Count", company["trade_count"])
        
        if history["analysis_cycles"]:
            st.write("**Analysis Cycles**")
            cycles_df = pd.DataFrame(history["analysis_cycles"])
            cycles_df["Confidence"] = (cycles_df["confidence"] * 100).round(0).astype(int).astype(str) + "%"
            cycles_df["Timestamp"] = pd.to_datetime(cycles_df["cycle_timestamp"]).dt.strftime("%Y-%m-%d %H:%M")
            st.dataframe(
                cycles_df[["Timestamp", "signal", "Confidence"]],
                hide_index=True,
                width="stretch",
            )
        
        if history["trades"]:
            st.write("**Related Trades**")
            trades_df = pd.DataFrame(history["trades"])
            trades_df["Created"] = pd.to_datetime(trades_df["created_at"]).dt.strftime("%Y-%m-%d %H:%M")
            trades_df["Entry"] = trades_df["entry_price"].apply(lambda x: f"${x:.2f}" if x else "N/A")
            trades_df["Current"] = trades_df["current_price"].apply(lambda x: f"${x:.2f}" if x else "N/A")
            if "leverage" in trades_df.columns:
                trades_df["Leverage"] = trades_df["leverage"].apply(lambda x: f"{x:.1f}x" if x else "1.0x")
            else:
                trades_df["Leverage"] = "1.0x"
            trades_df["P&L"] = trades_df["realized_pnl"].apply(lambda x: f"${x:.2f}" if x else "-")
            st.dataframe(
                trades_df[["Created", "side", "Leverage", "status", "Entry", "Current", "P&L"]],
                hide_index=True,
                width="stretch",
            )
    
    st.divider()
    st.subheader("Least Analyzed Companies")
    st.caption("Companies with the fewest analysis cycles (prioritize to reduce repetition)")
    
    least_analyzed = get_least_analyzed_companies(limit=15)
    if least_analyzed:
        least_df = pd.DataFrame(least_analyzed)
        least_df["Times Analyzed"] = least_df["times_analyzed"]
        least_df["Last Analyzed"] = pd.to_datetime(least_df["last_analyzed_at"]).dt.strftime("%Y-%m-%d %H:%M")
        
        st.dataframe(
            least_df[["ticker", "company_name", "sector", "Times Analyzed", "Last Analyzed"]],
            hide_index=True,
            width="stretch",
            height=300,
        )


def render_how_it_works() -> None:
    st.subheader("How it works")
    st.caption("Research informs a proposal; strategy and risk gates decide whether the paper simulator records it.")
    st.warning(
        "This application is paper-only. It does not submit brokerage orders, attach exchange stop/target orders, "
        "or continuously monitor live positions."
    )

    agent_columns = st.columns(4)
    agent_content = (
        (
            "Researcher",
            "Builds candidates from the configured research feed, classifies relevant headlines, and optionally asks OpenAI for a company signal. Yahoo Finance supplies fundamentals, a quote, and adjusted price history when available.",
        ),
        (
            "Strategist",
            "Maps the research signal to a proposed long or short, checks confidence and quote availability, and applies the configured countertrend-short gate when company fundamentals are strong.",
        ),
        (
            "Risk Manager",
            "Applies deterministic drawdown and volatility thresholds. When enabled, a separate OpenAI review examines Yahoo price trends over the last hour, 24 hours, week-to-date, year-to-date, and five years; a sufficiently confident opposing trend can block the proposal.",
        ),
        (
            "Execution",
            "Records an accepted paper position and its reference entry, stop, and target values in SQLite. Those reference levels are not sent to a broker or automatically triggered.",
        ),
    )
    for column, (agent, description) in zip(agent_columns, agent_content):
        with column:
            st.markdown(f"**{agent}**")
            st.write(description)

    st.divider()
    st.subheader("What runs")
    run_columns = st.columns(3)
    run_columns[0].markdown("**Research inputs**")
    run_columns[0].write(
        "Yahoo headlines are fetched hourly by the background worker. Manual actions can fetch news, run analysis, or do both. Automatic research cycles use the interval and suspension settings in Research Actions."
    )
    run_columns[1].markdown("**OpenAI calls**")
    run_columns[1].write(
        "Company research is optional. The independent risk trend review is controlled by the Risk Manager rule and may make an additional OpenAI request for each strategy-approved candidate. Both calls contribute to tracked token usage."
    )
    run_columns[2].markdown("**Trade evidence**")
    run_columns[2].write(
        "Each candidate has a trade ID with ordered researcher, strategy, risk, execution, and optional exit events. Inputs, headlines, model output, quote snapshots, and rejection reasons are stored in SQLite for lifecycle review."
    )

    st.divider()
    st.subheader("Current limits")
    st.markdown(
        "- Risk checks are not connected to a live account: the current cycle uses simulated P&L and volatility inputs.\n"
        "- Open positions are not continuously repriced. Stops and targets are stored reference values; positions are closed manually in the dashboard.\n"
        "- Yahoo data can be delayed, incomplete, or unavailable. OpenAI analyzes only the data supplied to it; it is not a market-data feed.\n"
        "- A trend review or research signal is not a profit guarantee and does not replace testing for gaps, spread, slippage, fees, or event risk."
    )

    st.divider()
    st.subheader("Path to trading APIs")
    st.markdown(
        "1. Connect a broker’s **paper-trading API** and verify quotes, order acknowledgements, fills, and position reconciliation.\n"
        "2. Replace simulated risk inputs with account equity, current positions, real-time prices, and measured volatility.\n"
        "3. Add idempotent order handling, partial-fill/cancel recovery, native bracket orders, stale-data checks, and a kill switch.\n"
        "4. Run extended paper and shadow tests with fees and slippage, then review operational and risk results before considering any live account.\n\n"
        "Live trading is not implemented or enabled in this application. A future broker adapter must be explicitly built and validated; changing the paper-trading rule alone cannot enable it."
    )


def render_profile_page(username: str) -> None:
    profile = get_user_profile(username)
    if not profile:
        st.error("The signed-in account could not be loaded.")
        return

    st.subheader("Profile")
    avatar_col, identity_col = st.columns([1, 5], vertical_alignment="center")
    with avatar_col:
        st.markdown(f"### :material/{profile['avatar']}: ")
    with identity_col:
        st.markdown(f"**{profile['username']}** · {'Administrator' if profile['is_admin'] else 'User'}")
        st.caption(f"Account created {profile['created_at']}")

    avatar_col, password_col = st.columns(2, gap="large")
    with avatar_col:
        st.markdown("#### Avatar")
        with st.form("profile_avatar_form"):
            selected_avatar = st.selectbox(
                "Choose an avatar",
                options=list(PROFILE_AVATARS),
                index=list(PROFILE_AVATARS).index(profile["avatar"]),
                format_func=lambda avatar: PROFILE_AVATARS[avatar],
            )
            if st.form_submit_button("Save avatar"):
                update_user_avatar(username, selected_avatar)
                st.rerun()

    with password_col:
        st.markdown("#### Change password")
        with st.form("profile_password_form"):
            current_password = st.text_input("Current password", type="password", key="profile_current_password")
            new_password = st.text_input("New password", type="password", key="profile_new_password")
            confirm_password = st.text_input("Confirm new password", type="password", key="profile_confirm_password")
            if st.form_submit_button("Update password"):
                if new_password != confirm_password:
                    st.error("New password and confirmation do not match.")
                else:
                    try:
                        change_user_password(username, current_password, new_password)
                        st.success("Password updated.")
                    except ValueError as error:
                        st.error(str(error))

    if not profile["is_admin"]:
        return

    st.divider()
    st.subheader("User management")
    st.dataframe(
        pd.DataFrame([
            {
                "Username": user["username"],
                "Role": "Administrator" if user["is_admin"] else "User",
                "Avatar": PROFILE_AVATARS.get(user["avatar"], "Classic"),
                "Created": user["created_at"],
            }
            for user in list_users()
        ]),
        hide_index=True,
        width="stretch",
    )

    create_col, manage_col = st.columns(2, gap="large")
    with create_col:
        st.markdown("#### Create user")
        with st.form("profile_create_user_form"):
            new_username = st.text_input("Username", key="profile_new_username")
            new_password = st.text_input("Temporary password", type="password", key="profile_create_password")
            grant_admin = st.checkbox("Administrator access", value=False, key="profile_new_user_admin")
            if st.form_submit_button("Create account"):
                if len(new_password) < 12:
                    st.error("Temporary password must contain at least 12 characters.")
                elif add_user(new_username, new_password, is_admin=grant_admin):
                    st.success(f"User '{new_username}' created.")
                    st.rerun()
                else:
                    st.error("Username is missing or already exists.")

    other_users = [user for user in list_users() if user["username"] != username]
    with manage_col:
        st.markdown("#### Manage user")
        if not other_users:
            st.caption("Create another account to manage users.")
        else:
            target = st.selectbox(
                "Account",
                options=other_users,
                format_func=lambda user: user["username"],
                key="profile_managed_user",
            )
            with st.form("profile_manage_user_form"):
                grant_admin = st.checkbox(
                    "Administrator access",
                    value=bool(target["is_admin"]),
                    key=f"profile_admin_{target['username']}",
                )
                save_role, delete_account = st.columns(2)
                save_role_clicked = save_role.form_submit_button("Save role")
                delete_clicked = delete_account.form_submit_button("Delete user")
            if save_role_clicked:
                try:
                    set_user_admin(target["username"], grant_admin)
                    st.success("User role updated.")
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
            if delete_clicked:
                try:
                    delete_user(target["username"], username)
                    st.success("User deleted and their sessions revoked.")
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))


def render_time_range() -> tuple[str, str]:
    now = datetime.now().replace(second=0, microsecond=0)
    default_start = now - timedelta(days=30)
    selected_start = st.session_state.get("dashboard_range_start", default_start)
    selected_end = st.session_state.get("dashboard_range_end", now)
    range_label = "Time"
    range_help = (
        f"Dashboard range: {selected_start:%b %d %H:%M} – {selected_end:%b %d %H:%M}. "
        "Change dashboard date and time range."
    )
    with st.popover(range_label, icon=":material/calendar_month:", help=range_help):
        with st.form("dashboard_time_range"):
            start_date_col, start_time_col, end_date_col, end_time_col, apply_col = st.columns([1.6, 1, 1.6, 1, 0.8])
            with start_date_col:
                start_date = st.date_input("From", value=selected_start.date(), key="range_start_date")
            with start_time_col:
                start_time = st.time_input("Time", value=selected_start.time(), key="range_start_time")
            with end_date_col:
                end_date = st.date_input("To", value=selected_end.date(), key="range_end_date")
            with end_time_col:
                end_time = st.time_input("Time", value=selected_end.time(), key="range_end_time")
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
    batch_col, batch_help = st.columns([2, 5], vertical_alignment="center")
    with batch_col:
        if st.button(
            "Batch validate 4–18 day opportunities",
            icon=":material/fact_check:",
            key="batch_validate_trade_paths",
        ):
            with st.spinner("Validating unvalidated opportunities against up to 14 days of Yahoo 5-minute history..."):
                st.session_state["trade_validation_batch_result"] = asyncio.run(
                    validate_pending_trade_outcomes(
                        min_age_days=4,
                        max_age_days=18,
                        max_validation_days=14,
                    )
                )
            st.rerun()
    with batch_help:
        st.caption("Runs across all eligible records, regardless of the current dashboard date or status filters.")

    batch_result = st.session_state.pop("trade_validation_batch_result", None)
    if batch_result:
        st.info(
            f"Batch complete: {batch_result['selected_count']} selected · "
            f"{batch_result['available_count']} validated · "
            f"{batch_result['unavailable_count']} unavailable."
        )

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
    adapter = getattr(backend_module, "OpenAIResearchAdapter", None)
    recorder_supported = (
        adapter is not None
        and "usage_recorder" in inspect.signature(adapter.__init__).parameters
    )
    researcher_defaults = getattr(backend_module, "DEFAULT_AGENT_RULES", {}).get("researcher", {})
    strategist_defaults = getattr(backend_module, "DEFAULT_AGENT_RULES", {}).get("strategist", {})
    risk_defaults = getattr(backend_module, "DEFAULT_AGENT_RULES", {}).get("risk_manager", {})
    execution_defaults = getattr(backend_module, "DEFAULT_AGENT_RULES", {}).get("execution", {})
    researcher_type = getattr(backend_module, "ResearcherAgent", None)
    candidate_pool_supported = (
        researcher_type is not None
        and "max_candidates" in inspect.signature(researcher_type.run_data_fetch).parameters
    )
    fundamentals_supported = (
        hasattr(backend_module, "fetch_company_fundamentals")
        and "include_market_fundamentals" in researcher_defaults
    )
    strategy_gate_supported = (
        hasattr(backend_module, "fetch_market_quote")
        and hasattr(backend_module, "evaluate_strategist_gate")
        and "countertrend_short_min_confidence" in strategist_defaults
        and "countertrend_long_min_confidence" in strategist_defaults
        and "fundamental_confirmation_min_confidence" in strategist_defaults
        and "weak_fundamentals_max_revenue_growth_pct" in strategist_defaults
        and "require_current_market_quote" in strategist_defaults
        and "max_trades_per_cycle" in execution_defaults
        and "strong_fundamentals_min_revenue_growth_pct" in strategist_defaults
    )
    trend_review_supported = (
        hasattr(backend_module, "fetch_market_trends")
        and adapter is not None
        and hasattr(adapter, "review_market_trends")
        and "openai_trend_review_enabled" in risk_defaults
    )
    if (
        recorder_supported
        and fundamentals_supported
        and candidate_pool_supported
        and hasattr(backend_module, "calculate_candidate_success_score")
        and strategy_gate_supported
        and trend_review_supported
        and hasattr(backend_module, "get_openai_usage_summary")
        and hasattr(backend_module, "validate_pending_trade_outcomes")
        and hasattr(backend_module, "validate_trade_outcome")
        and hasattr(backend_module, "get_user_profile")
        and hasattr(backend_module, "change_user_password")
    ):
        return
    integrations_module = importlib.import_module("integrations")
    importlib.reload(integrations_module)
    refreshed = importlib.reload(backend_module)
    for name in (
        "DEFAULT_AGENT_RULES", "add_user", "ensure_db", "authenticate_user",
        "close_paper_trade", "create_session_token", "get_dashboard_summary",
        "get_ai_research_enabled", "get_portfolio_summary", "revoke_session_token",
        "get_openai_usage_summary", "get_research_schedule", "run_hourly_news_fetch",
        "get_user_profile", "list_users", "update_user_avatar", "change_user_password",
        "run_trading_cycle", "set_ai_research_enabled", "set_starting_capital",
        "configure_research_schedule", "suspend_research_schedule",
        "sync_openai_token_allowance", "update_agent_rules", "validate_trade_outcome",
        "validate_pending_trade_outcomes", "validate_session_token", "set_user_admin",
        "delete_user",
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

    render_header_actions(cookie_manager)
    st.sidebar.title("Navigation")

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
        if st.button("Synchronize usage counter", icon=":material/sync:", key="ai_settings_sync_usage"):
            sync_openai_token_allowance()
            st.rerun()

    st.caption("Paper portfolio · simulated research and execution · no live brokerage orders")

    render_research_actions()

    navigation_col, range_col, refresh_col = st.columns([8, 1, 1], vertical_alignment="center", gap="small")
    with navigation_col:
        selected_view = st.segmented_control(
            "Main navigation",
            ["Dashboard", "Trade lifecycles", "Research", "Companies", "Profile", "How it works"],
            default="Dashboard",
            label_visibility="collapsed",
            key="main_navigation",
        )
    with range_col:
        start_at, end_at = render_time_range()
    with refresh_col:
        if st.button("🔄", help="Refresh page", key="page_refresh_button", use_container_width=True):
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
                opened_count = sum(trade.get("status") == "OPEN" for trade in cycle_result["trades"])
                deferred_count = sum(trade.get("status") == "DEFERRED_CAPACITY" for trade in cycle_result["trades"])
                st.write(
                    f"Candidate pool: {cycle_result['market_data'].get('candidate_pool_size', 0)} · "
                    f"analyzed: {cycle_result.get('research_candidate_count', 0)} · "
                    f"risk-approved: {cycle_result.get('risk_approved_candidate_count', 0)} · "
                    f"opened: {opened_count} of {cycle_result.get('max_trades_per_cycle', 0)} allowed · "
                    f"deferred by quota: {deferred_count}."
                )
                st.caption(
                    "Selection score ranks research confidence and directional evidence corroboration; "
                    "it is a heuristic, not a calibrated probability of profit."
                )
                st.dataframe(pd.DataFrame(cycle_result["trades"]), hide_index=True, width="stretch")

    st.sidebar.subheader("Agent rule editor")
    for agent_name, default_rules in DEFAULT_AGENT_RULES.items():
        agent_rules = next((agent["rules"] for agent in summary.get("agents", []) if agent["agent_name"] == agent_name), default_rules)
        with st.sidebar.expander(agent_name.title()):
            with st.form(f"edit_rules_{agent_name}"):
                updated = {}
                for key, value in agent_rules.items():
                    if key == "status":
                        continue
                    label = key.replace("_", " ").title()
                    help_text = AGENT_RULE_HELP.get(key, "Rule applied by this agent.")
                    if isinstance(value, bool):
                        if agent_name == "risk_manager" and key == "enabled":
                            st.checkbox("Enabled (required safety gate)", value=True, disabled=True, help=help_text)
                            updated[key] = True
                        elif agent_name == "execution" and key == "paper_trading_only":
                            st.checkbox("Paper trading only (locked)", value=True, disabled=True, help=help_text)
                            updated[key] = True
                        else:
                            updated[key] = st.checkbox(label, value=value, help=help_text)
                    elif isinstance(value, list):
                        defaults = {
                            "allowed_sectors": ["Technology", "Energy"],
                            "allowed_side": ["LONG", "SHORT"],
                        }.get(key, value)
                        options = list(dict.fromkeys([*defaults, *value]))
                        updated[key] = st.multiselect(label, options=options, default=value, help=help_text)
                    elif isinstance(value, int):
                        updated[key] = st.number_input(label, value=int(value), step=1, help=help_text)
                    elif isinstance(value, float):
                        updated[key] = st.number_input(label, value=float(value), step=0.01, help=help_text)
                    else:
                        updated[key] = st.text_input(label, value=str(value), help=help_text)
                if st.form_submit_button(f"Save {agent_name} rules"):
                    update_agent_rules(agent_name, updated)
                    st.sidebar.success(f"{agent_name.title()} rules updated.")
                    st.rerun()

    if selected_view == "Dashboard":
        render_summary(summary)
        render_graphs(summary)
    elif selected_view == "Trade lifecycles":
        render_trade_search(summary["trades"])
    elif selected_view == "Research":
        render_research_headlines(summary)
    elif selected_view == "Companies":
        render_companies_dashboard()
    elif selected_view == "Profile":
        render_profile_page(st.session_state.get("username", ""))
    else:
        render_how_it_works()


if __name__ == "__main__":
    main()
