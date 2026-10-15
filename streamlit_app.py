import asyncio
import html
import importlib
import inspect
import json
import math
import os
from pathlib import Path
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional

import extra_streamlit_components as stx
import integrations as integrations_module
import makemoney as backend_module
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from email_notifications import (
    build_trade_report_html,
    load_trade_evidence,
    parse_recipient_addresses,
    send_trade_execution_email,
    test_smtp_connection,
)

DISPLAY_TIMEZONE = ZoneInfo("Europe/Stockholm")

integrations_refresh_needed = (
    not hasattr(integrations_module, "validate_trade_price_path")
    or not hasattr(integrations_module, "validate_etoro_credentials")
    or not hasattr(integrations_module, "get_portfolio_account_snapshot")
    or not hasattr(integrations_module, "EtoroAPIError")
)
if integrations_refresh_needed:
    integrations_module = importlib.reload(integrations_module)

if (
    integrations_refresh_needed
    or not hasattr(backend_module, "get_openai_usage_summary")
    or not hasattr(backend_module, "validate_trade_outcome")
    or not hasattr(backend_module, "get_company_analytics")
    or not hasattr(backend_module, "get_latest_research_cycle_status")
    or not hasattr(backend_module, "get_etoro_api_debug_logging")
    or not hasattr(backend_module, "set_etoro_api_debug_logging")
        or not hasattr(backend_module, "run_strategy_evaluation")
        or not hasattr(backend_module, "get_strategy_evaluation")
        or not hasattr(backend_module, "run_demo_test_workflow")
        or not hasattr(backend_module, "get_sample_research_inputs")
        or not hasattr(backend_module, "place_analysis_candidate_demo_order")
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
    get_etoro_api_debug_logging,
    get_strategy_evaluation,
    get_demo_automated_trading_enabled,
    get_test_workflow_runs,
    get_sample_research_inputs,
    get_company_analytics,
    get_company_analysis_history,
    get_dashboard_summary,
    get_least_analyzed_companies,
    get_openai_usage_summary,
    get_portfolio_summary,
    get_research_schedule,
    get_latest_research_cycle_status,
    get_user_profile,
    list_users,
    revoke_session_token,
    run_hourly_news_fetch,
    run_trading_cycle,
    run_strategy_evaluation,
    place_analysis_candidate_demo_order,
    request_etoro_demo_close,
    run_demo_test_workflow,
    reconcile_demo_test_workflow,
    close_demo_test_workflow,
    set_starting_capital,
    set_ai_research_enabled,
    set_etoro_api_debug_logging,
    set_demo_automated_trading_enabled,
    configure_research_schedule,
    change_user_password,
    delete_user,
    set_user_admin,
    update_user_avatar,
    suspend_research_schedule,
    sync_openai_token_allowance,
    update_etoro_demo_protection,
    update_agent_rules,
    validate_pending_trade_outcomes,
    OverwatcherAgent,
    format_json_readable,
    validate_trade_outcome,
    validate_session_token,
)

st.set_page_config(page_title="Finbot Local MVP", layout="wide")
ENABLE_HTTPS = os.getenv("ENABLE_HTTPS", "false").lower() in {"1", "true", "yes", "on"}

AGENT_RULE_HELP = {
    "risk_per_trade_pct": "Maximum planned entry-to-stop risk for one new position as a fraction of verified account equity.",
    "max_total_risk_pct": "Maximum aggregate planned stop risk across verified open positions and the proposed order.",
    "max_gross_exposure_pct": "Maximum total gross notional exposure as a fraction of verified account equity.",
    "max_open_positions": "Maximum number of verified broker positions allowed before another order is deferred.",
    "max_single_position_exposure_pct": "Maximum gross notional for one position as a fraction of account equity.",
    "stress_gap_pct": "Additional adverse gap assumption, as a fraction of price, added to nominal stop risk. This is a scenario, not a loss guarantee.",
    "max_stressed_loss_pct": "Maximum portfolio loss under the configured stop-plus-gap stress scenario, as a fraction of equity.",
    "stop_atr_multiple": "Distance from entry to stop loss in ATR14 units. The current default is 2× ATR14.",
    "target_reward_risk": "Take-profit distance as a multiple of stop distance. At 1.5 with a 2× ATR stop, take profit is 3× ATR14 from entry.",
    "sector_exposure_enabled": "Optional sector concentration gate. Requires verified sector labels for every broker holding; unknown sectors block new orders when enabled.",
    "max_sector_exposure_pct": "Maximum gross notional per sector as a fraction of equity when the sector gate is enabled.",
    "max_order_units": "Hard quantity ceiling per new order. Fractional units are allowed. Default: 5 units.",
    "max_order_spend_usd": "Maximum estimated account capital per order, including a reserve for costs. At 2× leverage, $200 margin can provide up to $400 exposure. The units cap also applies.",
    "analysis_retention_days": "Archive never-submitted analysis after this many days. Broker orders, fills, uncertain submissions and open positions are preserved. Default: 3 days.",
    "conditional_mean_reversion_confirmation_atr": "Required completed-bar recovery from the post-news extreme before entering the reversal.",
    "conditional_mean_reversion_min_confidence": "Research confidence required for a temporary news reaction; technical distance never overrides it.",
    "conditional_mean_reversion_stop_cushion_atr": "Place the protective stop this many ATR units beyond the measured news-reaction extreme.",
    "max_research_age_hours": "A manual candidate older than this needs fresh research before submission.",
    "news_max_hold_hours": "Automatically request a broker close when a new directional news trade exceeds this holding time.",
    "mean_reversion_max_hold_hours": "Maximum holding time for a new conditional mean reversion trade.",
    "estimated_roundtrip_cost_bps": "Fallback round-trip trading cost estimate in basis points when broker cost evidence is unavailable.",
    "estimated_financing_annual_pct": "Fallback annual financing-rate estimate used over the planned holding period.",
    "enabled": "Whether this agent participates in analysis cycles.",
    "min_confidence": "Minimum confidence, from 0.0 to 1.0, needed to pass this decision gate.",
    "allowed_sectors": "Research candidates outside these sectors are rejected.",
    "max_fetch_per_cycle": "Maximum number of unique feed candidates analyzed per cycle. The pool rotates across cycles; with OpenAI enabled, each candidate may use a research call and a separate risk trend-review call.",
    "include_market_fundamentals": "Fetch Yahoo Finance company statements for strategist checks and include them in OpenAI research when enabled.",
    "fundamental_confirmation_min_confidence": "Minimum confidence when at least three company metrics and a directly matched headline support the proposed direction.",
    "countertrend_short_min_confidence": "Minimum confidence for a short that conflicts with strong fundamentals or company-specific bullish news.",
    "countertrend_long_min_confidence": "Minimum confidence for a long that conflicts with weak fundamentals or company-specific bearish news.",
    "conditional_mean_reversion_min_atr_multiple": "Minimum measured post-news excursion from the pre-news price, in ATR14 multiples. A temporary news assessment and completed-bar reversal confirmation are also required.",
    "strong_fundamentals_min_revenue_growth_pct": "Revenue growth threshold for one of the metrics used to identify strong company fundamentals.",
    "strong_fundamentals_min_net_profit_margin_pct": "Net margin threshold for one of the metrics used to identify strong company fundamentals.",
    "strong_fundamentals_max_debt_to_equity": "Maximum debt/equity ratio for one of the metrics used to identify strong company fundamentals.",
    "weak_fundamentals_max_revenue_growth_pct": "Revenue growth at or below this value counts as one of five weak-company indicators; at least three are required.",
    "weak_fundamentals_max_net_profit_margin_pct": "Net margin at or below this value counts as one of five weak-company indicators; EPS and cash flow are also considered.",
    "weak_fundamentals_min_debt_to_equity": "Debt/equity at or above this value counts as one of five weak-company indicators.",
    "require_company_specific_news_for_fundamental_confirmation": "Require a company-specific headline in the same direction before lowering the confidence threshold for a fundamentals-aligned trade.",
    "require_company_specific_news_for_countertrend_short": "Require a directly company-linked bearish headline before shorting against strong fundamentals or bullish news.",
    "require_company_specific_news_for_countertrend_long": "Require a directly company-linked bullish headline before going long against weak fundamentals or bearish news.",
    "require_current_market_quote": "Execution requires a fresh eToro quote. Missing quotes do not block news or company analysis.",
    "default_leverage": "Starting leverage for a candidate; the Strategist's final leverage is persisted with its exits.",
    "max_leverage": "Upper leverage limit for the strategist's proposed position.",
    "position_limit": "Maximum fraction of starting capital allocated to one trade.",
    "max_drawdown": "Risk rejection threshold for modeled account drawdown.",
    "max_volatility": "Risk rejection threshold for modeled volatility.",
    "stop_loss_pct": "Default stop-loss distance from entry price as a fraction.",
    "exit_on_risk_breach": "Reject/exit if a configured risk threshold is breached. This safety gate is required.",
    "openai_trend_review_enabled": "After strategy approval, request an independent OpenAI review of Yahoo price trends over 1 hour, 24 hours, this week, this year, and 5 years. This uses additional API tokens.",
    "trend_opposition_min_confidence": "Reject a proposed position when the OpenAI trend review strongly opposes it at or above this confidence.",
    "paper_trading_only": "Controls the local simulator. When eToro is the selected broker, approved trades are sent to the configured eToro demo or live endpoint.",
    "max_trade_value": "Maximum paper capital allocation for one trade.",
    "max_trades_per_cycle": "Maximum number of eligible eToro demo orders submitted during one research cycle.",
    "allowed_side": "LONG and/or SHORT directions permitted at the execution boundary.",
}

ENV_CONFIG_PATH = Path(__file__).resolve().parent / ".env.config"
PORTFOLIO_ACCOUNT_OPTIONS = {
    "paper": "Paper portfolio",
    "etoro_demo": "eToro demo account",
    "etoro_main": "eToro main account",
    "ibkr_demo": "IBKR demo account",
    "ibkr_main": "IBKR main account",
}


def integer_leverage(value: Any) -> Optional[int]:
    try:
        numeric = float(value)
        if math.isfinite(numeric) and numeric.is_integer():
            return int(numeric)
    except (TypeError, ValueError):
        pass
    return None


def format_leverage(value: Any) -> str:
    integer_value = integer_leverage(value)
    return f"{integer_value}x" if integer_value is not None else "Unknown"


def load_env_config() -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not ENV_CONFIG_PATH.exists():
        return values
    with ENV_CONFIG_PATH.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value
    return values


def save_env_config(updates: Dict[str, str]) -> None:
    existing_lines: list[str] = []
    if ENV_CONFIG_PATH.exists():
        existing_lines = ENV_CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    seen = set()
    rewritten: list[str] = []
    for raw_line in existing_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            rewritten.append(raw_line)
            continue
        key, _ = line.split("=", 1)
        if key in updates:
            rewritten.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            rewritten.append(raw_line)
    for key, value in updates.items():
        if key not in seen:
            rewritten.append(f"{key}={value}")
    ENV_CONFIG_PATH.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    os.environ.update(updates)
    clear_dashboard_caches()

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
            content: "Trade Monitor";
            position: absolute;
            left: 1rem;
            top: 0.55rem;
            color: #e2e8f0;
            font-size: 0.875rem;
            font-weight: 600;
            line-height: 2rem;
            pointer-events: none;
        }
        .trade-monitor-clock {
            position: fixed;
            left: 8rem;
            top: 1.05rem;
            z-index: 1000000;
            color: #94a3b8;
            font-size: 0.72rem;
            font-variant-numeric: tabular-nums;
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
            .trade-monitor-clock {
                left: 6.2rem;
                top: 1.12rem;
                font-size: 0.64rem;
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
        .flow-item.deferred {
            border-color: rgba(245, 158, 11, 0.7);
            box-shadow: inset 0 0 0 1px rgba(245, 158, 11, 0.25);
        }
        .flow-item.unavailable {
            border-color: rgba(168, 85, 247, 0.65);
            box-shadow: inset 0 0 0 1px rgba(168, 85, 247, 0.25);
        }
        .flow-item.skipped {
            border-style: dashed;
            color: #94a3b8;
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
    with st.sidebar.expander('Research Actions', expanded=False, key='lazy_render_research_actions_1', on_change="rerun") as lazy_panel:
        if lazy_panel.open:
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
                        if os.getenv("FINBOT_BROKER_PROVIDER", "dummy").strip().lower() == "etoro":
                            cycle_result = run_agent_cycle_for_ui()
                            news = (cycle_result or {}).get("news") or {}
                            news_result = {
                                "fetched": news.get("fetched", 0),
                                "inserted": news.get("inserted", 0),
                                "updated": news.get("updated", 0),
                            }
                        else:
                            news_result = asyncio.run(run_hourly_news_fetch())
                            cycle_result = run_agent_cycle_for_ui()
                    st.session_state["manual_research_result"] = {"news": news_result, "cycle": cycle_result}
                    refresh_dashboard_end_time()
                    st.rerun()

            cycle_status = get_latest_research_cycle_status()
            if cycle_status:
                started_at = datetime.fromisoformat(cycle_status["started_at"])
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                st.caption(f"Latest analysis started: {started_at.astimezone():%Y-%m-%d %H:%M:%S %Z}")
                portfolio = get_portfolio_summary()
                st.caption(f"Available paper cash: ${portfolio['cash_available']:,.2f}")
                if cycle_status["outcome_message"]:
                    st.write(cycle_status["outcome_message"])
                else:
                    st.warning("The latest analysis has not recorded a completed outcome.")
                if cycle_status["rejections"]:
                    with st.expander("Latest cycle rejection reasons"):
                        for rejection in cycle_status["rejections"]:
                            agent_label = rejection["agent_name"].replace("_", " ").title()
                            st.write(f"**{agent_label}**: {rejection['message']}")

            st.divider()
            st.markdown("**Test purchase workflow**")
            test_provider_ready = (
                os.getenv("FINBOT_BROKER_PROVIDER", "dummy").strip().lower() == "etoro"
                and os.getenv("ETORO_DEMO_TRADING", "true").strip().lower() in {"1", "true", "yes", "on"}
            )
            sample_inputs = get_sample_research_inputs()
            test_symbols = list(dict.fromkeys(item["symbol"] for item in sample_inputs))
            st.caption("Replays a saved bullish research fixture through the isolated demo-order test. It uses a 2×ATR stop and an explicit 1.5R target, then checks both levels against the broker-reported fill. Test orders are excluded from strategy performance stats.")
            if not test_symbols:
                st.warning("No valid saved bullish research fixtures are available.")
                test_symbols = ["(no eligible sample)"]
            test_symbol = st.selectbox("Test symbol", test_symbols, key="demo_test_symbol")
            test_amount = st.number_input("Test stake (USD)", min_value=1.0, max_value=25.0, value=10.0, step=1.0, key="demo_test_amount")
            st.warning("The next button sends a real order to the configured eToro DEMO account. It does not send a live order. Existing positions in this symbol prevent duplicate purchases.")
            if st.button("Replay sample through demo workflow", key="run_demo_test_workflow", disabled=not test_provider_ready or test_symbol.startswith("("), type="primary"):
                with st.spinner("Replaying saved research → strategy checks → eToro demo order → position reconciliation..."):
                    try:
                        st.session_state["demo_test_workflow_feedback"] = asyncio.run(run_demo_test_workflow(test_symbol, float(test_amount)))
                    except Exception as error:
                        st.session_state["demo_test_workflow_feedback"] = {"status": "BLOCKED", "symbol": test_symbol, "amount": float(test_amount), "reason": f"{type(error).__name__}: {error}"}
            if not test_provider_ready:
                st.caption("Select the eToro provider and keep ETORO_DEMO_TRADING=true to enable this test action.")
            feedback = st.session_state.get("demo_test_workflow_feedback")
            if feedback:
                st.write(f"Latest test workflow: **{feedback.get('status')}** · run {feedback.get('workflow_id')}")
                if feedback.get("reason"):
                    st.error(feedback["reason"])
                st.json(feedback.get("details", {}))
            for test_run in get_test_workflow_runs(limit=5):
                with st.expander(f"Test #{test_run['id']} · {test_run['symbol']} · {test_run['status']}"):
                    st.caption(f"Stake ${test_run['amount']:,.2f} · order {test_run.get('broker_order_id') or 'pending'} · position {test_run.get('broker_position_id') or 'pending'} · updated {test_run['updated_at']}")
                    st.json(test_run["details"])
                    if test_run["status"] in {"OPEN", "SUBMITTED_PENDING_RECONCILIATION"}:
                        refresh_col, close_col = st.columns(2)
                        if refresh_col.button("Refresh broker state", key=f"reconcile_test_{test_run['id']}"):
                            try:
                                st.session_state["demo_test_workflow_feedback"] = asyncio.run(reconcile_demo_test_workflow(int(test_run["id"])))
                            except Exception as error:
                                st.error(f"Test reconciliation failed: {error}")
                            st.rerun()
                        if test_run["status"] == "OPEN" and close_col.button("Close test position", key=f"close_test_{test_run['id']}"):
                            try:
                                st.session_state["demo_test_workflow_feedback"] = asyncio.run(close_demo_test_workflow(int(test_run["id"])))
                            except Exception as error:
                                st.error(f"Test close failed: {error}")
                            st.rerun()


def render_oversight_dashboard() -> None:
    st.subheader("Trade oversight")
    st.write(
        "Review Overwatcher audits and the configuration checks performed for each trade. "
        "Audit reports are stored locally as JSONL files."
    )
    if st.button("🔍 Run audit now", key="oversight_dashboard_run_audit"):
        with st.spinner("Auditing recent trades..."):
            audit_result = OverwatcherAgent().audit_all_trades()
        st.success(f"Audit complete: {audit_result['total_trades_audited']} trades checked.")
        st.rerun()

    audit_dir = backend_module.DB_PATH.parent / "audit_logs"
    audit_files = sorted(
        audit_dir.glob("oversight_*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ) if audit_dir.exists() else []
    if not audit_files:
        st.info("No audit logs yet. Run an audit to create the first report.")
        return

    selected_file = st.selectbox(
        "Audit log",
        audit_files,
        format_func=lambda path: path.name,
        key="oversight_log_selector",
    )
    records = []
    try:
        with selected_file.open(encoding="utf-8") as log_file:
            for line_number, line in enumerate(log_file, start=1):
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as error:
                    st.error(f"Invalid JSON in {selected_file.name}, line {line_number}: {error}")
                    return
    except OSError as error:
        st.error(f"Could not read audit log {selected_file.name}: {error}")
        return

    summary = next((record["summary"] for record in reversed(records) if "summary" in record), {})
    trade_records = [record for record in records if "trade_id" in record]
    issue_count = sum(bool(record.get("issues")) for record in trade_records)
    audit_timestamp = summary.get("timestamp")
    if audit_timestamp:
        parsed_timestamp = datetime.fromisoformat(audit_timestamp)
        if parsed_timestamp.tzinfo is None:
            parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)
        audit_timestamp = parsed_timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    metric_columns = st.columns(3)
    metric_columns[0].metric("Trades audited", f"{summary.get('total_trades_audited', len(trade_records)):,}")
    metric_columns[1].metric("Trades with issues", f"{issue_count:,}")
    metric_columns[2].metric("Audit created", audit_timestamp or "Unavailable")

    if trade_records:
        def check_label(check: Dict[str, Any]) -> str:
            valid = check.get("valid")
            if valid is True:
                return "Pass"
            if valid is False:
                return "Fail"
            return "Not checked"

        table_rows = []
        for record in trade_records:
            checks = record.get("configuration_checks", {})
            leverage_check = checks.get("leverage", {})
            stop_loss_check = checks.get("stop_loss", {})
            take_profit_check = checks.get("take_profit", {})
            actual_leverage = leverage_check.get("actual")
            leverage_label = (
                format_leverage(actual_leverage)
                if actual_leverage is not None
                else "Not checked"
            )
            table_rows.append({
                "Trade": record["trade_id"],
                "Ticker": record.get("ticker", ""),
                "Status": record.get("status", ""),
                "Leverage": leverage_label,
                "Leverage valid": check_label(leverage_check),
                "Stop loss valid": check_label(stop_loss_check),
                "Take profit valid": check_label(take_profit_check),
                "Issues": " · ".join(record.get("issues", [])) or "None",
            })
        st.dataframe(pd.DataFrame(table_rows), hide_index=True, width="stretch")
        for record in trade_records:
            trade_issue_count = len(record.get("issues", []))
            issue_label = f"{trade_issue_count} issue(s)" if trade_issue_count else "No issues"
            with st.expander(
                f"Trade {record['trade_id']} · {record.get('ticker', 'Unknown')} · {issue_label}"
            ):
                st.code(format_json_readable(record), language="json")
    else:
        st.info("This audit log contains no individual trade records.")


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
    render_expandable_plotly_chart(figure, "Decisions over time · by agent", "agent_decision_trend")


def render_expandable_plotly_chart(figure: Any, title: str, key: str) -> None:
    """Render a chart with a large dialog view and Plotly's fullscreen toolbar."""
    if st.button("Expand chart", key=f"{key}_expand", help=f"Open {title} in a larger view"):
        dialog = getattr(st, "dialog", None)
        if dialog is None:
            st.info("This Streamlit version has no chart dialog. Use the chart toolbar's fullscreen control.")
        else:
            @dialog(title, width="large")
            def show_chart_dialog() -> None:
                st.plotly_chart(
                    figure,
                    width="stretch",
                    height=760,
                    key=f"{key}_expanded",
                    config={"displayModeBar": True, "displaylogo": False, "responsive": True},
                )
            show_chart_dialog()
    st.plotly_chart(
        figure,
        width="stretch",
        key=key,
        config={"displayModeBar": True, "displaylogo": False, "responsive": True},
    )


def render_portfolio(summary: Dict[str, Any]) -> None:
    portfolio = summary["portfolio"]
    open_trades = summary.get("open_positions", [trade for trade in summary["trades"] if trade["status"] == "OPEN"])
    st.markdown("#### Portfolio")
    metrics = st.columns(2, gap="small")
    values = (
        ("First observed equity" if portfolio.get("source") else "Local starting capital", f"${portfolio['starting_capital']:,.2f}"),
        ("Available cash", f"${portfolio['cash_available']:,.2f}"),
        ("Invested", f"${portfolio['invested_capital']:,.2f}"),
        ("Gross exposure", f"${portfolio['gross_exposure']:,.2f}"),
        ("Net equity", f"${portfolio['account_equity']:,.2f}"),
        ("Tracked open positions", f"{len(open_trades):,}"),
    )
    for index, (label, value) in enumerate(values):
        with metrics[index % 2]:
            st.metric(label, value)
    if portfolio.get("source"):
        st.caption(f"{portfolio['source']} · {format_local_timestamp(portfolio.get('as_of'))}")
    elif os.getenv("FINBOT_BROKER_PROVIDER", "dummy").lower() == "etoro":
        st.caption("No broker equity snapshot yet; these figures describe local records, not verified eToro account equity.")
    portfolio_source = os.getenv("FINBOT_PORTFOLIO_ACCOUNT_SOURCE", "paper").lower()
    if portfolio_source != "paper":
        snapshot = st.session_state.get(f"portfolio_account_snapshot::{portfolio_source}")
        source_label = PORTFOLIO_ACCOUNT_OPTIONS.get(portfolio_source, portfolio_source)
        if snapshot and snapshot.get("available"):
            currency = snapshot.get("currency") or "USD"
            balance = snapshot.get("balance")
            balance_text = f"{currency} {float(balance):,.2f}" if balance is not None else "unavailable"
            st.caption(f"Portfolio source: {source_label} · Selected balance: {balance_text}")
        else:
            st.caption(f"Portfolio source: {source_label}")


@st.cache_data(ttl=60, show_spinner=False)
def get_dashboard_etoro_positions() -> Dict[str, Any]:
    settings = integrations_module.IntegrationSettings.from_environment()
    if settings.broker_provider != "etoro" or not settings.etoro_demo_trading:
        return {"available": False, "reason": "eToro demo mode is not selected."}
    try:
        return integrations_module.EtoroBrokerAdapter(settings).get_positions()
    except Exception as error:
        return {"available": False, "reason": f"{type(error).__name__}: broker position snapshot unavailable."}


def render_portfolio_charts(summary: Dict[str, Any]) -> None:
    open_trades = summary.get("open_positions", [trade for trade in summary["trades"] if trade["status"] == "OPEN"])
    chart_col, trend_col = st.columns([1, 2])
    with chart_col:
        st.markdown("#### Capital by open position")
        broker_snapshot = None
        chart_positions: list[Dict[str, Any]] = open_trades
        position_source = "Finbot tracked positions"
        if os.getenv("FINBOT_BROKER_PROVIDER", "dummy").strip().lower() == "etoro":
            broker_snapshot=(summary.get("broker_performance") or {}).get("positions_snapshot") or {"available":False}
            if st.button("Refresh eToro positions", key="refresh_capital_positions"):
                get_dashboard_etoro_positions.clear()
                st.session_state["explicit_broker_snapshot"]=get_dashboard_etoro_positions()
            override=st.session_state.get("explicit_broker_snapshot")
            if override and override.get("fetched_at"):
                fetched=datetime.fromisoformat(override["fetched_at"].replace("Z","+00:00"))
                if (datetime.now(timezone.utc)-fetched).total_seconds() < 60:
                    broker_snapshot=override
            if broker_snapshot.get("fetched_at"):
                st.caption(f"Broker snapshot: {format_local_timestamp(broker_snapshot['fetched_at'])}")
            elif not broker_snapshot.get("available"):
                st.caption("No saved broker snapshot yet. The worker updates it, or use Refresh eToro positions.")
            if broker_snapshot.get("available"):
                linked_trades = {
                    str(trade.get("broker_position_id")): trade
                    for trade in open_trades if trade.get("broker_position_id")
                }
                chart_positions = []
                for position in broker_snapshot.get("positions", []):
                    amount = position.get("amount")
                    units = position.get("units")
                    open_rate = position.get("open_rate")
                    leverage = position.get("leverage") or 1
                    if amount is None and units is not None and open_rate is not None:
                        amount = abs(float(units) * float(open_rate)) / max(1.0, float(leverage))
                    if amount is None:
                        continue
                    amount = abs(float(amount))
                    linked = linked_trades.get(str(position.get("position_id")))
                    chart_positions.append({
                        "id": position.get("position_id") or f"{position.get('symbol')}:{len(chart_positions)}",
                        "symbol": position.get("symbol") or (linked or {}).get("symbol") or "Unknown",
                        "allocated_capital": float(amount),
                        "status": linked.get("status") if linked else "eToro open · unlinked",
                    })
                position_source = "eToro demo positions"
            else:
                st.caption("eToro positions could not be refreshed; showing locally tracked positions instead.")

        if chart_positions:
            positions_frame = pd.DataFrame(chart_positions)
            exposure = positions_frame.groupby("symbol", as_index=False).agg(
                allocated_capital=("allocated_capital", "sum"),
                position_count=("id", "count"),
                states=("status", lambda values: ", ".join(sorted(set(map(str, values))))),
            )
            figure = go.Figure(go.Bar(
                x=exposure["symbol"],
                y=exposure["allocated_capital"],
                marker_color="#38bdf8",
                customdata=exposure[["position_count", "states"]],
                hovertemplate=(
                    "%{x}<br>Allocated capital: $%{y:,.2f}"
                    "<br>Tracked positions: %{customdata[0]}"
                    "<br>State: %{customdata[1]}<extra></extra>"
                ),
            ))
            figure.update_layout(
                title="Capital allocated to current open positions",
                height=340,
                margin={"l": 20, "r": 15, "t": 45, "b": 35},
                xaxis_title="Symbol",
                yaxis_title="Allocated capital (USD)",
            )
            render_expandable_plotly_chart(figure, "Capital by open position", "capital_by_open_position")
            if position_source == "eToro demo positions":
                st.caption(f"Live eToro demo positions ({len(broker_snapshot.get('positions', []))} positions), grouped by ticker. Capital uses the broker amount, or units × open price ÷ leverage when amount is unavailable; unlinked positions are included.")
            else:
                st.caption("Includes local open positions and broker-linked positions awaiting reconciliation. A reconciliation state is shown in the hover details; these count as potentially still open until closure is confirmed.")
        else:
            if position_source == "eToro demo positions" and broker_snapshot and broker_snapshot.get("positions"):
                st.caption("The broker returned open positions, but no capital amounts could be calculated from their reported fields.")
            else:
                st.caption("No open positions yet.")
    with trend_col:
        st.markdown("#### Decisions over time · by agent")
        decision_ranges = {
            "24 hours": timedelta(hours=24),
            "3 days": timedelta(days=3),
            "7 days": timedelta(days=7),
            "1 month": timedelta(days=30),
            "3 months": timedelta(days=90),
        }
        selected_range = st.selectbox(
            "Show decisions from",
            list(decision_ranges),
            index=2,
            key="decision_trend_range",
        )
        range_end = datetime.now(timezone.utc).replace(microsecond=0)
        range_start = range_end - decision_ranges[selected_range]
        events = backend_module.get_decision_timeline(
            range_start.strftime("%Y-%m-%d %H:%M:%S"),
            range_end.strftime("%Y-%m-%d %H:%M:%S"),
        )
        events = [
            event for event in events
            if event["decision"] in {"accepted", "rejected"}
        ]
        if events:
            render_agent_decision_trend(events)
        else:
            st.caption(f"No accepted/rejected agent decisions in the last {selected_range}.")


def render_event_evidence(event: Dict[str, Any]) -> None:
    details = event.get("details") or {}
    st.markdown(f"**Outcome: {event['decision'].title()}**")
    st.write(event["message"])

    broker_order = details.get("broker_order") or {}
    api_logs = details.get("broker_api_logs") or broker_order.get("api_logs") or []
    if api_logs:
        with st.expander(f'eToro API log · {len(api_logs)} request(s) · credentials masked', expanded=False, key=f"event_details_{event['id']}_1", on_change="rerun") as lazy_panel:
            if lazy_panel.open:
                for api_log in api_logs:
                    st.caption(
                        f"{api_log.get('request', {}).get('method', 'API')} "
                        f"{api_log.get('request', {}).get('url', '')} · "
                        f"request ID {api_log.get('request_id', 'unavailable')}"
                    )
                    st.code(format_json_readable(api_log), language="json")

    decision_data = {
        key: value for key, value in details.items()
        if key not in {
            "related_news", "news_context", "ai_analysis", "trend_review", "validation_type",
            "broker_order", "broker_api_logs",
        }
    }
    if decision_data:
        with st.expander('Decision inputs and rule checks', expanded=False, key=f"event_details_{event['id']}_2", on_change="rerun") as lazy_panel:
            if lazy_panel.open:
                st.code(format_json_readable(decision_data), language="json")

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
        with st.expander(f'Headline evidence · {len(headlines)} article(s)', expanded=False, key=f"event_details_{event['id']}_3", on_change="rerun") as lazy_panel:
            if lazy_panel.open:
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
        with st.expander('OpenAI research details', expanded=False, key=f"event_details_{event['id']}_4", on_change="rerun") as lazy_panel:
            if lazy_panel.open:
                st.write(ai_analysis.get("summary", "No summary returned."))
                fundamentals = {
                    key: "Unavailable" if value is None else value
                    for key, value in (ai_analysis.get("fundamentals") or {}).items()
                }
                st.code(format_json_readable({
                    "provider": ai_analysis.get("provider"),
                    "model": ai_analysis.get("model"),
                    "fundamentals": fundamentals,
                    "fundamental_analysis": ai_analysis.get("fundamental_analysis"),
                    "fundamental_grade": ai_analysis.get("fundamental_grade", "unknown"),
                    "fundamental_grades": ai_analysis.get("fundamental_grades", {}),
                }), language="json")
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


def _lifecycle_event_decision(event: Dict[str, Any]) -> str:
    decision = str(event.get("decision", "pending"))
    details = event.get("details") or {}
    reasons = details.get("reasons") or details.get("execution_reasons") or []
    capacity_phrases = (
        "open position limit reached",
        "position in this instrument is already open",
        "no risk budget remains",
        "portfolio exposure capacity",
        "per-cycle order limit reached",
    )
    if event.get("stage") == "execution" and decision == "pending" and any(
        phrase in str(reason).casefold()
        for reason in reasons
        for phrase in capacity_phrases
    ):
        return "deferred"
    return decision


def _trade_chart_events(trade: Dict[str, Any]) -> tuple[Dict[str, Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return decision events for each agent, excluding later reconciliation outcomes."""
    stage_for_agent = {
        "researcher": "research",
        "strategist": "strategy",
        "risk_manager": "risk",
        "execution": "execution",
    }
    latest_by_agent: Dict[str, Dict[str, Any]] = {}
    latest_reconciliation = None
    for event in trade.get("events", []):
        if event.get("stage") == "reconciliation":
            latest_reconciliation = event
            continue
        agent = event.get("agent_name")
        if agent not in stage_for_agent or event.get("stage") != stage_for_agent[agent]:
            continue
        if event.get("status") == "skipped":
            continue
        valid_decisions = {"accepted", "rejected"}
        if agent == "execution":
            valid_decisions.update({"submitted", "deferred"})
            if _lifecycle_event_decision(event) == "deferred":
                event = {**event, "chart_decision": "deferred"}
                valid_decisions.add("pending")
        if event.get("decision") in valid_decisions:
            latest_by_agent[agent] = event

    # A submitted event is the execution result. If eToro returned a fill,
    # display that more precise outcome while retaining reconciliation context.
    execution = latest_by_agent.get("execution")
    if execution and execution.get("decision") == "submitted":
        if trade.get("broker_order_id") and trade.get("broker_fill_price") is not None:
            execution = {**execution, "chart_decision": "executed"}
        else:
            execution = {**execution, "chart_decision": "submitted"}
        if latest_reconciliation:
            details = execution.get("details") or {}
            execution = {
                **execution,
                "chart_reconciliation": latest_reconciliation.get("message", ""),
                "details": details,
            }
        latest_by_agent["execution"] = execution
    return latest_by_agent, latest_reconciliation


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
        latest_by_agent, _ = _trade_chart_events(trade)

        stages = []
        for agent_name, event in latest_by_agent.items():
            stage = agent_rows[agent_name]
            stages.append((y_order.index(stage), stage, event))
        stages.sort(key=lambda item: item[0], reverse=True)
        for _, stage, event in stages:
            details = event.get("details") or {}
            decision = event.get("chart_decision", event["decision"])
            hover_parts = [
                f"<b>Trade {trade['id']} · {html.escape(trade.get('company_name') or trade['symbol'])} ({html.escape(trade['symbol'])})</b>",
                f"<b>{html.escape(stage)} · {html.escape(decision.title())}</b>",
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
            if event.get("chart_reconciliation"):
                hover_parts.append("Reconciliation warning: " + html.escape(event["chart_reconciliation"]))
            plotted_events.append({
                "trade_id": trade["id"],
                "x": trade_label,
                "y": stage,
                "decision": decision,
                "letter": {"accepted": "A", "rejected": "R", "submitted": "S", "executed": "E", "deferred": "D"}.get(decision, "?"),
                "hover": "<br><br>".join(hover_parts),
            })

    if not plotted_events:
        st.info("No agent decisions or submitted orders in this time range.")
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

    for decision, color in (("accepted", "#22c55e"), ("rejected", "#ef4444"), ("submitted", "#3b82f6"), ("executed", "#22c55e"), ("deferred", "#f59e0b")):
        points = [point for point in plotted_events if point["decision"] == decision]
        if points:
            figure.add_trace(go.Scatter(
                x=[point["x"] for point in points],
                y=[point["y"] for point in points],
                mode="markers+text",
                name={"accepted": "Accepted (A)", "rejected": "Rejected (R)", "submitted": "Order submitted (S)", "executed": "Executed (E)", "deferred": "Deferred by risk capacity (D)"}[decision],
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
    render_expandable_plotly_chart(figure, "Trade processing by agent", "trade_processing_chart")


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
        latest_by_agent, latest_reconciliation = _trade_chart_events(trade)
        execution_event = latest_by_agent.get("execution")
        time_value = execution_event["created_at"] if execution_event else trade["created_at"]
        label = f"#{trade['id']} · {pd.to_datetime(time_value).strftime('%b %d %H:%M')} · {trade['symbol']}"
        trade_labels.append(label)

        ordered_events = [
            (agent, latest_by_agent[agent])
            for agent in ("researcher", "strategist", "risk_manager", "execution")
            if agent in latest_by_agent
        ]
        for agent, event in ordered_events:
            details = event.get("details") or {}
            hover_parts = [
                f"<b>Trade #{trade['id']} · {html.escape(trade.get('company_name') or trade['symbol'])} ({html.escape(trade['symbol'])})</b>",
                f"{html.escape(event['created_at'])} · {html.escape(agent_rows[agent])} · {html.escape(event.get('chart_decision', event['decision']).title())}",
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
            if event.get("chart_reconciliation"):
                hover_parts.append("Reconciliation warning · " + html.escape(event["chart_reconciliation"]))
            plotted.append({
                "trade_id": trade["id"],
                "trade_label": label,
                "agent": agent_rows[agent],
                "decision": event.get("chart_decision", event["decision"]),
                "mark": {"accepted": "A", "rejected": "R", "submitted": "S", "executed": "E", "deferred": "D"}.get(event.get("chart_decision", event["decision"]), "?"),
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
        ("submitted", "#3b82f6", "S · Order submitted"),
        ("executed", "#22c55e", "E · Executed"),
        ("deferred", "#f59e0b", "D · Deferred by risk capacity"),
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
    render_expandable_plotly_chart(figure, "Trade agent flow", "trade_agent_flow_chart")


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
    render_expandable_plotly_chart(figure, "Historical validation: planned vs result", "trade_validation_chart")
    st.caption(
        "**Blue bars** = Planned target return you set. **Green bars** = Actual result from historical price data "
        "(what would have happened). Rejected/deferred trades are counterfactual outcomes. Results apply stop-loss and "
        "take-profit rules to Yahoo 5-minute bars; they are not actual fills or net returns."
    )


def account_controls_allowed() -> bool:
    profile = get_user_profile(st.session_state.get("username", ""))
    return bool(profile and profile.get("is_admin"))


@st.cache_data(ttl=5, max_entries=32, show_spinner=False)
def get_ui_dashboard_summary(db_path: str, start_at=None, end_at=None):
    return backend_module.get_dashboard_summary(start_at,end_at,include_details=False)


@st.cache_data(ttl=5, max_entries=64, show_spinner=False)
def get_ui_trade_page(db_path: str, trade_ids: tuple, limit: int, offset: int, end_at=None):
    return backend_module.get_trade_history(end_at=end_at,trade_ids=list(trade_ids),limit=limit,offset=offset,include_details=False)


@st.cache_data(ttl=5, max_entries=64, show_spinner=False)
def get_ui_trade_evidence(db_path: str, trade_id: int, end_at=None):
    rows=backend_module.get_trade_history(end_at=end_at,trade_ids=[trade_id],limit=1)
    return rows[0] if rows else None


def clear_dashboard_caches():
    get_ui_dashboard_summary.clear()
    get_ui_trade_page.clear()
    get_ui_trade_evidence.clear()


def lifecycle_page(items, limit, offset):
    return get_ui_trade_page(str(backend_module.DB_PATH),tuple(t["id"] for t in items),limit,offset,st.session_state.get("lifecycle_end_at"))


_LIFECYCLE_STAGE_RANK = {"execution": 0, "risk": 1, "strategy": 2, "research": 3}


def lifecycle_terminal_event(trade: Dict[str, Any]) -> Dict[str, Any]:
    """Return the stage that actually decided how far a candidate progressed.

    Every lifecycle contains downstream placeholder events.  A skipped execution
    placeholder must not make a research or strategy rejection look as though it
    reached execution.
    """
    events = trade.get("events") or []
    latest = {}
    for event in events:
        stage = str(event.get("stage") or "").lower()
        if stage in _LIFECYCLE_STAGE_RANK and int(event.get("id") or 0) >= int(
            latest.get(stage, {}).get("id") or 0
        ):
            latest[stage] = event

    last_accepted = {}
    for stage in ("research", "strategy", "risk"):
        event = latest.get(stage)
        if not event:
            return last_accepted
        decision = str(event.get("decision") or "").lower()
        status = str(event.get("status") or "").lower()
        if decision != "accepted" or status == "skipped":
            return event
        last_accepted = event

    # Passing risk means the candidate reached the execution gate even when
    # execution was subsequently deferred, unavailable, or rejected.
    return latest.get("execution") or last_accepted


def lifecycle_event_labels(events: list[Dict[str, Any]]) -> list[str]:
    """Name repeated gates by purpose instead of showing duplicate agent names."""
    totals: Dict[str, int] = {}
    seen: Dict[str, int] = {}
    for event in events:
        stage = str(event.get("stage") or "").lower()
        totals[stage] = totals.get(stage, 0) + 1
    labels = []
    for event in events:
        stage = str(event.get("stage") or "").lower()
        seen[stage] = seen.get(stage, 0) + 1
        occurrence = seen[stage]
        if stage == "strategy" and totals[stage] > 1:
            label = "Initial strategy" if occurrence == 1 else "Pre-order strategy recheck"
        elif stage == "risk" and totals[stage] > 1:
            label = "Initial risk" if occurrence == 1 else "Pre-order risk recheck"
        elif stage == "execution":
            label = "Broker submission" if str(event.get("decision") or "").lower() == "submitted" else "Execution readiness"
        elif stage == "notification":
            label = "Email notification"
        elif stage == "reconciliation":
            label = "Broker reconciliation"
        elif stage == "protection":
            label = "Protection update"
        else:
            label = stage.replace("_", " ").title()
        labels.append(label)
    return labels


def lifecycle_outcome_key(trade: Dict[str, Any]) -> tuple:
    """Group repeated analysis only when its material outcome is unchanged."""
    terminal = lifecycle_terminal_event(trade)
    details = terminal.get("details") or {}
    reasons = details.get("execution_reasons") or details.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    outcome_detail = tuple(sorted(str(reason).strip() for reason in reasons if str(reason).strip()))
    if not outcome_detail:
        outcome_detail = (" ".join(str(terminal.get("message") or "").split()),)
    return (
        str(trade.get("symbol") or "").upper(),
        str(trade.get("side") or "").upper(),
        str(trade.get("status") or "").upper(),
        str(terminal.get("stage") or "").lower(),
        str(terminal.get("decision") or "").lower(),
        outcome_detail,
    )


def collapse_repeated_analysis(trades: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    """Keep the newest row for each equivalent outcome and annotate its history."""
    groups: Dict[tuple, list[Dict[str, Any]]] = {}
    for trade in sorted(trades, key=lambda item: int(item.get("id") or 0), reverse=True):
        groups.setdefault(lifecycle_outcome_key(trade), []).append(trade)
    collapsed = []
    for rows in groups.values():
        representative = dict(rows[0])
        timestamps = [str(row.get("created_at") or "") for row in rows if row.get("created_at")]
        representative["repeat_count"] = len(rows)
        representative["repeat_first_at"] = min(timestamps) if timestamps else None
        representative["repeat_last_at"] = max(timestamps) if timestamps else None
        representative["repeat_trade_ids"] = [int(row["id"]) for row in rows]
        collapsed.append(representative)
    return sorted(
        collapsed,
        key=lambda trade: (
            _LIFECYCLE_STAGE_RANK.get(
                str(lifecycle_terminal_event(trade).get("stage") or "").lower(), 9
            ),
            -int(trade.get("id") or 0),
        ),
    )


def filter_analysis_stage(trades: list[Dict[str, Any]], focus: str) -> list[Dict[str, Any]]:
    stage_for = lambda trade: str(lifecycle_terminal_event(trade).get("stage") or "").lower()
    if focus == "Reached execution":
        return [trade for trade in trades if stage_for(trade) == "execution"]
    if focus == "Stopped at risk":
        return [trade for trade in trades if stage_for(trade) == "risk"]
    if focus == "Stopped at strategy":
        return [trade for trade in trades if stage_for(trade) == "strategy"]
    if focus == "Stopped at research":
        return [trade for trade in trades if stage_for(trade) == "research"]
    return trades


def render_trade_lifecycles(trades: list[Dict[str, Any]]) -> None:
    if not trades:
        st.info("No research cycles recorded yet. Run a cycle to create the first trade lifecycles.")
        return
    for trade in trades:
        company_name = trade.get("company_name") or trade["symbol"]
        timestamp = format_local_timestamp(trade.get("created_at"))
        repeat_count = int(trade.get("repeat_count") or 1)
        if repeat_count > 1:
            first_seen = format_local_timestamp(trade.get("repeat_first_at"))
            duplicate = f" · REPEATED {repeat_count} runs · first {first_seen} · latest"
        else:
            duplicate = (
                f" · DUPLICATE of #{trade['duplicate_of_trade_id']}"
                if trade.get("is_duplicate") else ""
            )
        label = f"#{trade['id']} · {timestamp}{duplicate} · {company_name} ({trade['symbol']}) · {trade['side']} · {trade['status']}"
        panel=st.expander(label,expanded=False,key=f"lifecycle_panel_{trade['id']}",on_change="rerun")
        if not panel.open:
            continue
        with panel:
            if not trade.get("evidence_loaded", True):
                full_trade=get_ui_trade_evidence(str(backend_module.DB_PATH),int(trade["id"]),st.session_state.get("lifecycle_end_at"))
                if not full_trade:
                    st.info("This lifecycle is no longer in active history. Refresh the page.")
                    continue
                trade=full_trade
            try:
                downloadable_evidence = load_trade_evidence(
                    Path(backend_module.DB_PATH), int(trade["id"])
                )
                report_name = f"finbot-trade-{trade['id']}-{str(trade['symbol']).lower()}"
                download_json, download_html = st.columns(2)
                download_json.download_button(
                    "Download JSON report",
                    data=json.dumps(
                        downloadable_evidence, indent=2, sort_keys=True, default=str
                    ).encode("utf-8"),
                    file_name=f"{report_name}.json",
                    mime="application/json",
                    key=f"download_trade_json_{trade['id']}",
                    on_click="ignore",
                    use_container_width=True,
                )
                download_html.download_button(
                    "Download HTML report",
                    data=build_trade_report_html(downloadable_evidence).encode("utf-8"),
                    file_name=f"{report_name}.html",
                    mime="text/html",
                    key=f"download_trade_html_{trade['id']}",
                    on_click="ignore",
                    use_container_width=True,
                )
                st.caption("Downloaded reports include sanitized lifecycle stages and supporting data; credentials are redacted.")
            except Exception as error:
                st.warning(f"Trade report is unavailable: {error}")
            if trade.get("status") == "OPEN" and trade.get("broker_position_id"):
                st.success(
                    f"eToro demo position confirmed · order {trade.get('broker_order_id') or 'not returned'} · "
                    f"position {trade['broker_position_id']} · reconciliation {trade.get('reconciliation_status') or 'unknown'}"
                )
            elif trade.get("broker_order_id") or trade.get("broker_position_id"):
                st.info(
                    f"Broker-linked lifecycle · order {trade.get('broker_order_id') or 'not returned'} · "
                    f"position {trade.get('broker_position_id') or 'pending'} · status {trade.get('status')}"
                )
            if trade.get("status_inferred_from_events"):
                st.caption(
                    f"Status derived from this lifecycle’s saved agent decisions; the older record remains stored as {trade.get('stored_status')} in the database."
                )
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

            metrics = st.columns(7)
            metrics[0].metric("Allocated", f"${trade['allocated_capital']:,.2f}")
            metrics[1].metric("Entry", f"${(trade['entry_price'] or 0):,.4f}")
            metrics[2].metric("Quantity", f"{trade['quantity']:,.4f}")
            metrics[3].metric("Leverage", format_leverage(trade.get("leverage")))
            metrics[4].metric("Stop loss", f"${trade['stop_loss_price']:,.4f}" if trade["stop_loss_price"] else "N/A")
            metrics[5].metric("Take profit", f"${trade['take_profit_price']:,.4f}" if trade["take_profit_price"] else "N/A")
            target_pnl = projected_target_pnl(trade)
            metrics[6].metric(
                "Target P&L (pre-fee)",
                f"${target_pnl:,.2f}" if target_pnl is not None else "N/A",
                help="Projected dollar P&L if the stored take-profit price is reached, using the recorded quantity. Before fees and slippage; not realized.",
            )
            risk_event = next((e for e in reversed(trade.get("events", [])) if e.get("stage") == "risk"), None)
            risk_details = (risk_event or {}).get("details", {})
            sizing = risk_details.get("sizing")
            if sizing:
                with st.expander("How the number of units was determined"):
                    st.write(sizing["formula"])
                    st.dataframe(pd.DataFrame([
                        {"Constraint": key, "Maximum units": value}
                        for key, value in sizing["limits_units"].items()
                    ]), hide_index=True)
                    st.caption(f"Selected {sizing['units']:.6f} units; binding limit: {sizing['binding_limit']}. Spend means account margin/capital, with estimated costs reserved.")
                    st.json({key: risk_event["details"].get(key) for key in ("required_margin", "notional", "planned_stop_risk", "estimated_costs", "estimated_total_spend", "estimated_net_target_profit")})
            policy = json.loads(trade.get("policy_json") or "{}")
            research_session = next(
                ((event.get("details") or {}).get("market_session_context") for event in reversed(trade.get("events", []))
                 if event.get("stage") == "research" and (event.get("details") or {}).get("market_session_context")),
                {},
            )
            session_context = policy.get("market_session_context") or risk_details.get("market_session_context") or research_session
            if session_context:
                session_local = session_context.get("local_timestamp") or session_context.get("observed_at")
                st.caption(
                    f"Market context: {session_context.get('primary_exchange') or 'exchange unresolved'} · "
                    f"{session_context.get('market_session') or 'session unknown'} · "
                    f"{session_local or 'time unavailable'} · "
                    f"eToro {session_context.get('etoro_execution_state') or 'state unknown'}"
                )
            protection = risk_details.get("protection") or {}
            atr14 = protection.get("atr14") or risk_details.get("atr14") or policy.get("atr14")
            protection_entry = protection.get("entry_price") or trade.get("entry_price")
            protection_stop = protection.get("stop_loss_price") or trade.get("stop_loss_price")
            protection_target = protection.get("take_profit_price") or trade.get("take_profit_price")
            if atr14 and protection_entry and protection_stop and protection_target:
                stop_distance = abs(float(protection_entry) - float(protection_stop))
                target_distance = abs(float(protection_target) - float(protection_entry))
                with st.expander("How stop loss and take profit were calculated"):
                    protection_metrics = st.columns(4)
                    protection_metrics[0].metric("ATR14", f"${float(atr14):,.4f}")
                    protection_metrics[1].metric("Stop distance", f"{stop_distance / float(atr14):,.2f}× ATR")
                    protection_metrics[2].metric("Target distance", f"{target_distance / float(atr14):,.2f}× ATR")
                    protection_metrics[3].metric("Reward / risk", f"{target_distance / stop_distance:,.2f}:1" if stop_distance else "N/A")
                    st.caption(
                        f"Entry ${float(protection_entry):,.4f} · stop ${float(protection_stop):,.4f} · "
                        f"take profit ${float(protection_target):,.4f}. "
                        + str(protection.get("formula") or "Distances are calculated from the recorded entry using the ATR14 saved by the Risk Manager.")
                    )
            if trade.get("strategy_version"):
                st.caption(f"Strategy {trade['strategy_version']} · {policy.get('framework', 'unknown')} · maximum hold {policy.get('holding_hours', 'unknown')} hours")

            events = trade["events"]
            # Older automated lifecycles saved the readiness gate as pending
            # before appending the actual broker submission. Present that gate
            # as accepted once a later execution event proves submission.
            has_submitted_execution = any(
                event.get("stage") == "execution" and event.get("decision") == "submitted"
                for event in events
            )
            if has_submitted_execution:
                events = [
                    {
                        **event,
                        "decision": "accepted",
                        "message": "Execution readiness passed; final broker checks completed and the later execution event submitted the order.",
                    }
                    if (
                        event.get("stage") == "execution"
                        and event.get("decision") == "pending"
                        and (event.get("details") or {}).get("status") == "ready"
                    )
                    else event
                    for event in events
                ]
            if events:
                event_labels = lifecycle_event_labels(events)
                flow = ""
                for index, (event, event_label) in enumerate(zip(events, event_labels)):
                    decision = _lifecycle_event_decision(event)
                    status_class = (
                        decision if decision in {"accepted", "rejected", "unavailable", "skipped", "deferred"}
                        else "pending"
                    )
                    flow += (
                        f'<div class="flow-item {status_class}"><strong>{html.escape(event_label)}</strong>'
                        f'<br>{html.escape(event["agent_name"].replace("_", " ").title())}'
                        f'<br><small>{html.escape(decision.title())}</small></div>'
                    )
                    if index < len(events) - 1:
                        flow += '<div class="flow-arrow">→</div>'
                st.markdown(f'<div class="flow-stream">{flow}</div>', unsafe_allow_html=True)
                evidence_tabs = st.tabs([
                    f"{event_label} · {_lifecycle_event_decision(event).title()}"
                    for event, event_label in zip(events, event_labels)
                ], key=f"lifecycle_evidence_{trade['id']}", on_change="rerun")
                for tab, event in zip(evidence_tabs, events):
                    if not tab.open:
                        continue
                    with tab:
                        st.caption(format_local_timestamp(event["created_at"]))
                        render_event_evidence(event)
            else:
                st.caption("Trade has no recorded lifecycle events.")

            if trade.get("status") in {"ANALYSIS_ONLY", "DEFERRED_CAPACITY"} and account_controls_allowed():
                research_ok = any(
                    event.get("stage") == "research" and event.get("decision") == "accepted"
                    for event in trade.get("events", [])
                )
                strategy_ok = any(
                    event.get("stage") == "strategy" and event.get("decision") == "accepted"
                    for event in trade.get("events", [])
                )
                if research_ok and strategy_ok:
                    st.warning(
                        "This submits an eToro demo order after fresh account, quote, strategy, risk, and stop checks. "
                        "Direction-aware stops, targets and the configured units/spend caps are enforced."
                    )
                    confirmed = st.checkbox(
                        "I confirm: submit this candidate to my eToro demo account",
                        key=f"confirm_manual_demo_{trade['id']}",
                    )
                    if st.button(
                        "Submit manual eToro demo order",
                        key=f"submit_manual_demo_{trade['id']}",
                        disabled=not confirmed,
                    ):
                        try:
                            with st.spinner("Rechecking account, quote, research, and risk before demo submission..."):
                                submission = asyncio.run(place_analysis_candidate_demo_order(int(trade["id"])))
                                clear_dashboard_caches()
                            st.success(
                                f"Demo order submitted for {trade['symbol']}; order ID "
                                f"{submission.get('order', {}).get('order_id') or 'pending'}; reconciliation is pending."
                            )
                        except Exception as error:
                            st.error(f"Manual eToro demo order was not confirmed: {error}")
                        clear_dashboard_caches()
                        st.rerun()

            if trade["notes"]:
                st.write(trade["notes"])
            broker_trade = bool(trade.get("broker_position_id"))
            if trade.get("signal_provider") or trade.get("quote_provider"):
                st.caption(
                    f"Signal: {trade.get('signal_provider') or 'unknown'} at {trade.get('signal_timestamp') or 'unknown'} · "
                    f"quote: {trade.get('quote_provider') or 'unknown'} at {trade.get('quote_timestamp') or 'unknown'}"
                )
            if broker_trade:
                st.caption(
                    f"eToro demo · order {trade.get('broker_order_id') or 'unknown'} · "
                    f"position {trade.get('broker_position_id')} · fill "
                    f"{trade.get('broker_fill_price') or 'pending'} × {trade.get('broker_fill_units') or 'pending'} · "
                    f"fees {trade.get('broker_fees') if trade.get('broker_fees') is not None else 'not reported'} · "
                    f"stop {trade.get('stop_state') or 'unknown'} · reconciliation {trade.get('reconciliation_status') or 'unknown'}"
                )
                if trade.get("status") in {"OPEN", "RECONCILIATION_REQUIRED"} and account_controls_allowed():
                    with st.expander("Adjust eToro demo stop loss and take profit"):
                        st.caption(
                            "Submitting changes both protection prices once. Finbot then verifies the broker snapshot; "
                            "it does not repeatedly resend the same requested pair."
                        )
                        with st.form(f"protection_form_{trade['id']}"):
                            protection_stop, protection_target = st.columns(2)
                            stop_value = protection_stop.number_input(
                                "Stop loss price",
                                min_value=0.000001,
                                value=float(trade["stop_loss_price"]) if trade.get("stop_loss_price") else None,
                                format="%.6f",
                                key=f"protection_stop_{trade['id']}",
                            )
                            target_value = protection_target.number_input(
                                "Take profit price",
                                min_value=0.000001,
                                value=float(trade["take_profit_price"]) if trade.get("take_profit_price") else None,
                                format="%.6f",
                                key=f"protection_target_{trade['id']}",
                                help="Legacy positions without a stored target require you to enter one explicitly.",
                            )
                            update_protection = st.form_submit_button(
                                "Submit protection update to eToro demo", type="primary"
                            )
                        if update_protection:
                            try:
                                if stop_value is None or target_value is None:
                                    raise ValueError("Enter both a stop loss and take profit price.")
                                with st.spinner("Submitting protection prices and awaiting later broker verification..."):
                                    asyncio.run(update_etoro_demo_protection(
                                        int(trade["id"]), float(stop_value), float(target_value)
                                    ))
                                clear_dashboard_caches()
                                st.success("Protection update submitted. Reconciliation will verify the broker values.")
                                st.rerun()
                            except Exception as error:
                                st.error(f"eToro protection update failed: {error}")
                if trade.get("status") in {"OPEN", "RECONCILIATION_REQUIRED"} and account_controls_allowed() and st.button("Request eToro demo close", key=f"close_trade_{trade['id']}"):
                    try:
                        asyncio.run(request_etoro_demo_close(int(trade["id"])))
                        clear_dashboard_caches()
                        st.info("Close requested; position remains pending until broker reconciliation.")
                        st.rerun()
                    except Exception as error:
                        st.error(f"eToro close request failed: {error}")
            elif trade["status"] == "OPEN":
                exit_price = st.number_input(
                    "Paper exit price", min_value=0.000001,
                    value=float(trade["current_price"] or trade["entry_price"] or 1),
                    key=f"exit_price_{trade['id']}",
                )
                if account_controls_allowed() and st.button("Close local simulation position", key=f"close_trade_{trade['id']}"):
                    close_paper_trade(int(trade["id"]), float(exit_price))
                    clear_dashboard_caches()
                    st.rerun()


def render_dashboard_lifecycle_panel(trades: list[Dict[str, Any]]) -> None:
    st.subheader("Trade lifecycles")
    st.caption("Pending/deferred analysis is not a queued broker order. Fresh scheduled analysis can produce a new executable opportunity when its constraints clear.")
    search_col, status_col, count_col = st.columns([3, 2, 1])
    search_text = search_col.text_input("Search ticker or company", key="dashboard_lifecycle_search")
    statuses = ["All"] + sorted({trade["status"] for trade in trades})
    selected_status = status_col.selectbox("Lifecycle status", statuses, key="dashboard_lifecycle_status")
    page_size = count_col.selectbox("Rows", [10, 25, 50], index=0, key="dashboard_lifecycle_rows")
    query = search_text.strip().casefold()
    filtered = [
        trade for trade in trades
        if (selected_status == "All" or trade["status"] == selected_status)
        and (not query or query in str(trade.get("company_name", "")).casefold() or query in trade["symbol"].casefold())
    ]
    executed_statuses = {
        "OPEN", "CLOSED", "CLOSED_PNL_UNAVAILABLE", "EXIT_PENDING",
        "SUBMITTED_PENDING_RECONCILIATION", "RECONCILIATION_REQUIRED",
    }
    executed = [
        trade for trade in filtered
        if trade.get("broker_order_id") or trade.get("broker_position_id")
        or trade.get("status") in executed_statuses
    ]
    broker_linked_count = sum(
        1 for trade in executed
        if trade.get("broker_order_id") or trade.get("broker_position_id")
    )
    local_position_count = len(executed) - broker_linked_count
    analysis = [trade for trade in filtered if trade not in executed]
    show_repeated = st.toggle(
        "Show every repeated analysis run",
        value=False,
        key="dashboard_show_repeated_analysis",
        help="Off groups unchanged outcomes while retaining their run count and first/latest timestamps.",
    )
    analysis_focus = st.selectbox(
        "Analysis focus",
        ["Reached execution", "All stages", "Stopped at risk", "Stopped at strategy", "Stopped at research"],
        key="dashboard_analysis_focus",
    )
    analysis = filter_analysis_stage(analysis, analysis_focus)
    analysis_runs = len(analysis)
    if not show_repeated:
        analysis = collapse_repeated_analysis(analysis)
    executed_tab, analysis_tab = st.tabs([
        f"Positions / submissions · {len(executed)}", f"Analysis / not executed · {len(analysis)}",
    ], key="dashboard_lifecycle_tabs", on_change="rerun")
    st.caption(
        f"eToro broker-linked: {broker_linked_count} · local simulation positions: {local_position_count}. "
        "DUPLICATE marks a repeated ticker lifecycle. Broker-linked orders awaiting reconciliation remain here."
    )
    for tab, items, key, caption in (
        (executed_tab, executed, "dashboard_executed", "Executed or submitted lifecycles"),
        (analysis_tab, analysis, "dashboard_analysis", "Analyzed candidates without a broker order"),
    ):
        if not tab.open:
            continue
        with tab:
            st.caption(caption)
            if key == "dashboard_analysis" and not show_repeated:
                st.caption(f"{analysis_runs} stored runs grouped into {len(analysis)} materially distinct outcomes. Execution-stage failures appear first.")
            if not items:
                st.info("No matching lifecycles.")
                continue
            page_count = max(1, math.ceil(len(items) / page_size))
            page_key = f"{key}_page"
            page = min(max(1, int(st.session_state.get(page_key, 1))), page_count)
            st.session_state[page_key] = page
            controls = st.columns([1, 2, 1])
            if controls[0].button("Previous", key=f"{key}_previous", disabled=page <= 1):
                st.session_state[page_key] = page - 1
                st.rerun()
            controls[1].caption(f"Page {page} of {page_count} · {len(items)} lifecycles")
            if controls[2].button("Next", key=f"{key}_next", disabled=page >= page_count):
                st.session_state[page_key] = page + 1
                st.rerun()
            render_trade_lifecycles(lifecycle_page(items,page_size,(page-1)*page_size))


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
    performance = summary.get("broker_performance") or {}
    with st.expander("Broker performance evidence"):
        st.caption("Equity change includes any account cash flows, manual trades and test trades. It is not a verified strategy return until those effects are reconciled.")
        st.json({key: performance.get(key) for key in ("status", "reason", "strategy_outcomes", "snapshot_count", "closed_positions", "reported_closed_net_profit", "reported_closed_fees", "fees_complete", "equity_change_pct", "observed_equity_drawdown_pct", "benchmark_return_pct", "archived_analysis_count")})
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
    render_dashboard_lifecycle_panel(summary["trades"])


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


def format_trade_price(value: Any) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    price = float(value)
    return f"${price:,.2f}" if math.isfinite(price) and price > 0 else "N/A"


def projected_target_pnl(trade: Dict[str, Any]) -> Optional[float]:
    if trade.get("status") not in {"OPEN", "CLOSED"}:
        return None

    entry_price = trade.get("entry_price")
    take_profit_price = trade.get("take_profit_price")
    quantity = trade.get("quantity")
    side = trade.get("side")
    if (
        entry_price is None
        or take_profit_price is None
        or quantity is None
        or side not in {"LONG", "SHORT"}
    ):
        return None

    entry_price = float(entry_price)
    take_profit_price = float(take_profit_price)
    quantity = float(quantity)
    if (
        not all(map(math.isfinite, (entry_price, take_profit_price, quantity)))
        or entry_price <= 0
        or take_profit_price <= 0
        or quantity <= 0
    ):
        return None

    direction = 1 if side == "LONG" else -1
    return (take_profit_price - entry_price) * quantity * direction


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
            trades_df["Entry"] = trades_df["entry_price"].apply(format_trade_price)
            trades_df["Current"] = trades_df["current_price"].apply(format_trade_price)
            if "leverage" in trades_df.columns:
                trades_df["Leverage"] = pd.array(
                    trades_df["leverage"].apply(integer_leverage),
                    dtype="Int64",
                )
            else:
                trades_df["Leverage"] = "Unknown"
            trades_df["Target P&L (pre-fee)"] = trades_df.apply(
                lambda trade: (
                    f"${profit:,.2f}"
                    if (profit := projected_target_pnl(trade.to_dict())) is not None
                    else "—"
                ),
                axis=1,
            )
            trades_df["Realized P&L"] = trades_df.apply(
                lambda trade: (
                    f"${float(trade['realized_pnl']):,.2f}"
                    if trade["status"] == "CLOSED" and pd.notna(trade["realized_pnl"])
                    else "—"
                ),
                axis=1,
            )
            st.dataframe(
                trades_df[
                    [
                        "Created", "side", "Leverage", "Target P&L (pre-fee)",
                        "status", "Entry", "Current", "Realized P&L",
                    ]
                ],
                hide_index=True,
                width="stretch",
            )
            st.caption(
                "Target P&L is projected for executed positions using the recorded quantity and take-profit price, "
                "so leverage is included. It is before fees and slippage; Realized P&L is recorded only after closing."
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
    st.caption("News-reaction trading with local simulation and eToro demo execution. Live broker routing is blocked.")
    st.info(
        "The objective is positive net return within explicit loss and exposure limits. "
        "Historical evaluation is informational; every demo order still requires fresh broker, quote, sizing and protection checks."
    )

    workflow_tab, strategy_tab, operations_tab, config_tab, limits_tab = st.tabs(
        ["Workflow", "Strategies", "Operations", "Configuration map", "Known limits"]
    )
    with workflow_tab:
        agent_content = (
            ("1 · Researcher", "Discovers companies from current Yahoo headlines, attaches company financials, completed price context, post-news movement and exchange-local session evidence, then asks OpenAI for a dated directional assessment."),
            ("2 · Strategist", "Combines company-specific news, OpenAI findings, fundamentals, SMA trend, ATR and measured post-news movement. It selects directional news, conditional mean reversion, or rejection. Missing optional data is recorded rather than disguised as rejection."),
            ("3 · Risk Manager", "Verifies account equity, all broker positions, aggregate stop and stressed-gap risk, exposure, fresh eToro bid/ask, ATR protection, costs, quantity and the configured unit/spend limits."),
            ("4 · Execution", "Rechecks the candidate immediately before submission, writes a durable request reference, submits only to eToro demo, and leaves the lifecycle pending until broker reconciliation confirms the position."),
        )
        columns = st.columns(4)
        for column, (agent, description) in zip(columns, agent_content):
            with column:
                st.markdown(f"**{agent}**")
                st.write(description)
        st.markdown("**Lifecycle after submission**")
        st.write(
            "Maintenance reconciles order/fill/position IDs, quantity, actual fill, stop loss, take profit, fees and closure history. "
            "It can request configured time/thesis/protection exits, but never marks a broker position closed from a local database update alone."
        )
        st.caption(
            "Research → Strategy → Risk → Execution is the decision path. Later Strategy/Risk/Execution events are fresh pre-submit rechecks, "
            "and reconciliation/notification events report broker and delivery outcomes rather than a second trade decision."
        )

    with strategy_tab:
        st.markdown("**Directional news reaction**")
        st.write(
            "Bullish company research proposes LONG and bearish research proposes SHORT. Fundamentals, completed SMA context, exchange-local session, "
            "post-news price behavior and costs provide supporting or conflicting evidence. By default, protection uses a 2×ATR14 stop and a target "
            "1.5 times the stop distance (3×ATR14)."
        )
        st.markdown("**Conditional mean reversion**")
        st.write(
            "A temporary bearish reaction inside a bullish trend may propose LONG; a temporary bullish reaction inside a bearish trend may propose SHORT. "
            "The setup requires the configured ATR excursion, completed-bar reversal confirmation, confidence and a valid entry/stop/target relationship."
        )
        st.markdown("**Exchange-aware timing**")
        st.write(
            "Each candidate records its primary exchange, IANA timezone, local weekday, opening/morning/midday/afternoon/closing state, lunch break where applicable, "
            "and eToro quote state. These variables are evidence only until measured results justify a session-specific rule."
        )
        st.warning("OpenAI confidence is an analysis score, not a calibrated probability of profit. No current evaluation establishes profitability.")

    with operations_tab:
        st.markdown("**Automation and scheduling**")
        st.write(
            "Research Actions controls the automatic cycle interval and suspension. Automated eToro demo orders stay enabled after successful trades; "
            "each candidate still passes current account and portfolio gates. The per-cycle order limit and duplicate-position protection remain active."
        )
        st.markdown("**Storage and retention**")
        st.write(
            "SQLite is the active database on persistent local/block storage. Never-submitted analysis is archived after the configured retention period. "
            "Broker-linked lifecycles are preserved. Optional S3 support creates consistent snapshots and restores them at startup; S3 is not the live database filesystem."
        )
        st.markdown("**Reports and notifications**")
        st.write(
            "Trade lifecycles can be downloaded as sanitized JSON or a dark HTML report. SMTP can notify multiple recipients after an acknowledged demo submission. "
            "API debug evidence masks credentials and is controlled separately."
        )

    with config_tab:
        st.write(
            "Environment values configure providers, credentials and hosting. Persisted application settings configure scheduling and automation. "
            "Agent rules configure decisions and sizing. Current agent values are shown below."
        )
        current_rules = backend_module.get_agent_rules()
        rows = []
        for agent_name, defaults in DEFAULT_AGENT_RULES.items():
            values = current_rules.get(agent_name, defaults)
            for name, default in defaults.items():
                if name == "status":
                    continue
                rows.append({
                    "Agent": agent_name.replace("_", " ").title(),
                    "Setting": name,
                    "Current": json.dumps(values.get(name), default=str) if isinstance(values.get(name), (dict, list)) else str(values.get(name)),
                    "Default": json.dumps(default, default=str) if isinstance(default, (dict, list)) else str(default),
                    "Purpose": AGENT_RULE_HELP.get(name, "Persisted agent behavior setting."),
                })
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=520)
        manual_path = Path(__file__).resolve().parent / "CONFIGURATION.md"
        if manual_path.is_file():
            st.download_button(
                "Download configuration manual",
                data=manual_path.read_bytes(),
                file_name="FINBOT_CONFIGURATION.md",
                mime="text/markdown",
                key="download_configuration_manual",
            )
        st.caption("Credentials are intentionally excluded from this table. Configure them in Broker settings or the hosting provider's secret store.")

    with limits_tab:
        st.markdown(
            "- The active historical evaluation is inconclusive until point-in-time news, fundamentals, model decisions and execution costs can be replayed without look-ahead.\n"
            "- Yahoo and eToro data can be delayed, corrected, unavailable or disagree. Execution always uses a fresh eToro quote.\n"
            "- eToro acknowledgments do not prove fills or protection updates; reconciliation records the broker's later state and exposes mismatches.\n"
            "- Broker closed-history fields and fees may be incomplete for some positions, so realized attribution can remain unavailable.\n"
            "- SQLite supports the current single-replica design. Multiple application replicas require a transactional server database and distributed locking.\n"
            "- Market gaps, liquidity, financing, model error and regime changes can produce losses beyond modeled stops and costs."
        )

    st.divider()
    st.subheader("Path to trading APIs")
    st.markdown(
        "1. Connect a broker’s **paper-trading API** and verify quotes, order acknowledgements, fills, and position reconciliation.\n"
        "2. Replace simulated risk inputs with account equity, current positions, real-time prices, and measured volatility.\n"
        "3. Add idempotent order handling, partial-fill/cancel recovery, native bracket orders, stale-data checks, and a kill switch.\n"
        "4. Run extended paper and shadow tests with fees and slippage, then review operational and risk results before considering any live account.\n\n"
        "Live execution is blocked in code. Do not change credentials or mode to enable live orders; this strategy version has no live-order route."
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
    now = datetime.now(DISPLAY_TIMEZONE).replace(microsecond=0, tzinfo=None)
    now = now.replace(second=now.second-now.second%5)
    default_start = now - timedelta(days=30)
    selected_start = st.session_state.get("dashboard_range_start", default_start)
    selected_end = st.session_state.get("dashboard_range_end", now)
    if selected_start.tzinfo is not None:
        selected_start = selected_start.astimezone(DISPLAY_TIMEZONE).replace(tzinfo=None)
    if selected_end.tzinfo is not None:
        selected_end = selected_end.astimezone(DISPLAY_TIMEZONE).replace(tzinfo=None)
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

    start_utc = selected_start.replace(tzinfo=DISPLAY_TIMEZONE).astimezone(timezone.utc)
    end_utc = selected_end.replace(tzinfo=DISPLAY_TIMEZONE).astimezone(timezone.utc)
    return start_utc.strftime("%Y-%m-%d %H:%M:%S"), end_utc.strftime("%Y-%m-%d %H:%M:%S")


def refresh_dashboard_end_time() -> None:
    st.session_state["dashboard_range_end"] = datetime.now(DISPLAY_TIMEZONE).replace(tzinfo=None)


def format_local_timestamp(value: Optional[str]) -> str:
    if not value:
        return "time unavailable"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    # SQLite CURRENT_TIMESTAMP values are UTC even though they have no suffix.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")


def render_trade_monitor_clock() -> None:
    def render_clock_value() -> None:
        value = datetime.now(DISPLAY_TIMEZONE).strftime("%H:%M:%S %Z")
        st.markdown(f'<div class="trade-monitor-clock">{html.escape(value)}</div>', unsafe_allow_html=True)

    fragment = getattr(st, "fragment", None)
    if fragment:
        @fragment(run_every="1s")
        def live_clock() -> None:
            render_clock_value()
        live_clock()
    else:
        render_clock_value()


def render_trade_search(trades: list[Dict[str, Any]]) -> None:
    st.subheader("Trade lifecycles")
    age_range_col, batch_col = st.columns([3, 2], vertical_alignment="center")
    with age_range_col:
        min_age_days, max_age_days = st.slider(
            "Batch validation opportunity age (days)",
            min_value=1,
            max_value=18,
            value=(1, 18),
            step=1,
            key="batch_validation_age_range",
            help="Only opportunities whose decision age falls within this inclusive range are selected.",
        )
    with batch_col:
        if st.button(
            f"Batch validate {min_age_days}–{max_age_days} day opportunities",
            icon=":material/fact_check:",
            key="batch_validate_trade_paths",
            use_container_width=True,
        ):
            with st.status("Selecting eligible opportunities...", expanded=True) as validation_status:
                validation_progress = st.progress(0, text="Selecting eligible opportunities...")
                available_so_far = 0

                def update_validation_progress(
                    index: int,
                    total: int,
                    trade_id: int,
                    available: Optional[bool],
                ) -> None:
                    nonlocal available_so_far
                    if available is None:
                        fraction = (index - 1) / total
                        label = f"Fetching historical bars for trade #{trade_id} ({index}/{total})..."
                    else:
                        available_so_far += int(available)
                        fraction = index / total
                        outcome = "available" if available else "unavailable"
                        label = (
                            f"Completed {index}/{total} · {available_so_far} with data · "
                            f"trade #{trade_id} {outcome}"
                        )
                    validation_progress.progress(fraction, text=label)
                    validation_status.update(label=label, state="running", expanded=True)

                batch_result = asyncio.run(
                    validate_pending_trade_outcomes(
                        min_age_days=min_age_days,
                        max_age_days=max_age_days,
                        max_validation_days=14,
                        progress_callback=update_validation_progress,
                    )
                )
                st.session_state["trade_validation_batch_result"] = batch_result
                completion = (
                    f"Batch complete: {batch_result['selected_count']} selected · "
                    f"{batch_result['available_count']} with historical data · "
                    f"{batch_result['unavailable_count']} unavailable."
                )
                validation_progress.progress(1.0, text=completion)
                validation_status.update(label=completion, state="complete", expanded=True)
            st.rerun()
    st.caption("Runs across all eligible records, regardless of the current dashboard date or status filters.")

    batch_result = st.session_state.get("trade_validation_batch_result")
    if batch_result:
        age_range = batch_result["age_range_days"]
        st.info(
            f"Batch complete for {age_range['older_than']:g}–{age_range['younger_than']:g} days: "
            f"{batch_result['selected_count']} selected · "
            f"{batch_result['available_count']} with historical data · "
            f"{batch_result['unavailable_count']} unavailable."
        )

    st.divider()
    if st.toggle("Show historical validation chart",value=False,key="show_validation_chart"):
        full_trades=backend_module.get_trade_history(end_at=st.session_state.get("lifecycle_end_at"),trade_ids=[t["id"] for t in trades]) if trades else []
        render_trade_validation_chart(full_trades)
    st.divider()

    search_col, status_col = st.columns([3, 1])
    with search_col:
        search_text = st.text_input("Search company, ticker, or decision", key="trade_search")
    with status_col:
        statuses = ["All"] + sorted({trade["status"] for trade in trades})
        selected_status = st.selectbox("Status", statuses, key="trade_lifecycle_status")

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
    executed_statuses = {
        "OPEN", "CLOSED", "CLOSED_PNL_UNAVAILABLE", "EXIT_PENDING",
        "SUBMITTED_PENDING_RECONCILIATION", "RECONCILIATION_REQUIRED",
    }
    executed = [
        trade for trade in filtered
        if trade.get("broker_order_id") or trade.get("broker_position_id")
        or trade.get("status") in executed_statuses
    ]
    broker_linked_count = sum(
        1 for trade in executed
        if trade.get("broker_order_id") or trade.get("broker_position_id")
    )
    local_position_count = len(executed) - broker_linked_count
    analysis = [trade for trade in filtered if trade not in executed]

    def render_cycle_tab(cycles: list[Dict[str, Any]], key: str, kind: str) -> None:
        page_size_option = st.selectbox(
            "Trades per page", [10, 25, 50, 100, "All"], index=1,
            key=f"{key}_page_size",
        )
        if page_size_option == "All":
            page_cycles = cycles
            st.caption(f"Showing all {len(cycles)} {kind} cycle(s) in the selected time range")
        elif not cycles:
            page_cycles = []
            st.session_state[f"{key}_page"] = 1
            st.caption(f"0 {kind} cycles in the selected time range")
        else:
            page_size = int(page_size_option)
            page_count = math.ceil(len(cycles) / page_size)
            current_page = min(max(1, int(st.session_state.get(f"{key}_page", 1))), page_count)
            st.session_state[f"{key}_page"] = current_page
            start_index = (current_page - 1) * page_size
            end_index = min(start_index + page_size, len(cycles))
            page_cycles = cycles[start_index:end_index]
            previous_col, page_col, next_col = st.columns([1, 2, 1])
            with previous_col:
                if st.button("Previous", key=f"{key}_previous", disabled=current_page == 1):
                    st.session_state[f"{key}_page"] = current_page - 1
                    st.rerun()
            with page_col:
                st.caption(
                    f"Showing {kind} cycles {start_index + 1}–{end_index} of {len(cycles)} "
                    f"· page {current_page} of {page_count}"
                )
            with next_col:
                if st.button("Next", key=f"{key}_next", disabled=current_page == page_count):
                    st.session_state[f"{key}_page"] = current_page + 1
                    st.rerun()
        if page_size_option == "All":
            page_cycles=lifecycle_page(cycles,max(1,len(cycles)),0) if cycles else []
        elif cycles:
            page_cycles=lifecycle_page(cycles,page_size,start_index)
        render_trade_lifecycles(page_cycles)

    executed_tab, analysis_tab = st.tabs([
        f"Positions / submissions · {len(executed)}", f"Analysis / not executed · {len(analysis)}",
    ], key="trade_search_tabs", on_change="rerun")
    st.caption(
        f"eToro broker-linked: {broker_linked_count} · local simulation positions: {local_position_count}. "
        "A DUPLICATE lifecycle marker means an earlier cycle exists for the same ticker."
    )
    with executed_tab:
        if executed_tab.open:
            st.caption("Includes opened or closed local simulation positions and broker-linked orders, including submissions awaiting reconciliation. Check each lifecycle status for confirmed broker state.")
            render_cycle_tab(executed, "executed_cycles", "executed")
    with analysis_tab:
        if analysis_tab.open:
            focus = st.selectbox(
                "Analysis focus",
                ["Reached execution", "All stages", "Stopped at risk", "Stopped at strategy", "Stopped at research"],
                key="trade_analysis_focus",
            )
            show_repeated = st.toggle(
                "Show every repeated analysis run",
                value=False,
                key="trade_show_repeated_analysis",
                help="Off groups unchanged outcomes while retaining their run count and first/latest timestamps.",
            )
            focused_analysis = filter_analysis_stage(analysis, focus)
            displayed_analysis = focused_analysis if show_repeated else collapse_repeated_analysis(focused_analysis)
            if not show_repeated:
                st.caption(
                    f"{len(focused_analysis)} stored runs grouped into {len(displayed_analysis)} materially distinct outcomes. "
                    "Execution-stage failures appear first."
                )
            render_cycle_tab(displayed_analysis, "analysis_cycles", "analysis outcome")


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
        and "conditional_mean_reversion_min_atr_multiple" in strategist_defaults
        and hasattr(backend_module, "manage_etoro_demo_positions")
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
        and hasattr(backend_module, "get_latest_research_cycle_status")
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
        "get_latest_research_cycle_status",
        "get_user_profile", "list_users", "update_user_avatar", "change_user_password",
        "run_trading_cycle", "set_ai_research_enabled", "set_starting_capital",
        "configure_research_schedule", "suspend_research_schedule",
        "sync_openai_token_allowance", "update_agent_rules", "validate_trade_outcome",
        "validate_pending_trade_outcomes", "validate_session_token", "set_user_admin",
        "delete_user",
        "run_strategy_evaluation", "get_strategy_evaluation",
        "get_demo_automated_trading_enabled", "set_demo_automated_trading_enabled",
        "place_analysis_candidate_demo_order",
        "get_test_workflow_runs", "get_sample_research_inputs", "run_demo_test_workflow", "reconcile_demo_test_workflow", "close_demo_test_workflow",
        "request_etoro_demo_close",
    ):
        globals()[name] = getattr(refreshed, name)


def run_agent_cycle_for_ui() -> Dict[str, Any] | None:
    try:
        result=asyncio.run(run_trading_cycle())
        clear_dashboard_caches()
        return result
    except Exception as error:
        st.session_state["manual_research_error"] = str(error)
        return None


def main() -> None:
    refresh_stale_backend_import()
    db_path=str(backend_module.DB_PATH)
    if st.session_state.get("initialized_db_path") != db_path:
        ensure_db()
        st.session_state["initialized_db_path"]=db_path
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
    render_trade_monitor_clock()
    if not account_controls_allowed():
        st.caption("Read-only account access. Administrators control trading, settings and cleanup.")
        st.session_state["lifecycle_end_at"]=None
        summary = get_ui_dashboard_summary(str(backend_module.DB_PATH))
        render_summary(summary)
        render_graphs(summary)
        return
    st.sidebar.title("Navigation")
    with st.sidebar.expander('Analysis cleanup and restore', key='lazy_main_1', on_change="rerun") as lazy_panel:
        if lazy_panel.open:
            retention = backend_module.get_agent_rules()["execution"].get("analysis_retention_days", 3)
            st.caption(f"The worker archives never-submitted analysis older than {retention} days, checking hourly. Change retention under Execution rules. Broker-linked records are preserved.")
            if st.button("Archive stale analysis now", key="cleanup_analysis_now"):
                result = backend_module.cleanup_stale_analysis()
                clear_dashboard_caches()
                st.success(f"Archived {result['archived']} stale lifecycles.")
                st.rerun()
            restore_id = st.number_input("Archived trade ID to restore", min_value=1, value=1, step=1, key="restore_analysis_id")
            if st.button("Restore lifecycle", key="restore_analysis_button"):
                try:
                    backend_module.restore_archived_analysis(int(restore_id))
                    clear_dashboard_caches()
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))

    current_capital = get_portfolio_summary()["starting_capital"]
    with st.sidebar.expander('Portfolio settings', key='lazy_main_2', on_change="rerun") as lazy_panel:
        if lazy_panel.open:
            capital = st.number_input("Starting capital", min_value=1.0, value=float(current_capital), step=500.0)
            if st.button("Save capital", key="save_starting_capital"):
                try:
                    set_starting_capital(capital)
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))

            st.divider()
            st.caption("Choose the account whose balance should populate the dashboard portfolio setting.")
            portfolio_source = st.selectbox(
                "Portfolio account",
                list(PORTFOLIO_ACCOUNT_OPTIONS),
                index=list(PORTFOLIO_ACCOUNT_OPTIONS).index(
                    os.getenv("FINBOT_PORTFOLIO_ACCOUNT_SOURCE", "paper").lower()
                )
                if os.getenv("FINBOT_PORTFOLIO_ACCOUNT_SOURCE", "paper").lower() in PORTFOLIO_ACCOUNT_OPTIONS
                else 0,
                format_func=lambda key: PORTFOLIO_ACCOUNT_OPTIONS[key],
                help="This selection is stored in .env.config and used when syncing the portfolio capital.",
            )
            portfolio_source_key = f"portfolio_account_snapshot::{portfolio_source}"
            if st.button("Save portfolio account", key="save_portfolio_account"):
                try:
                    save_env_config({"FINBOT_PORTFOLIO_ACCOUNT_SOURCE": portfolio_source})
                    st.success("Portfolio account saved.")
                    st.rerun()
                except Exception as error:
                    st.error(f"Could not save portfolio account: {error}")

            selected_snapshot = st.session_state.get(portfolio_source_key)
            if portfolio_source != "paper":
                if st.button("Fetch selected account balance", key="fetch_portfolio_account_balance"):
                    try:
                        selected_snapshot = integrations_module.get_portfolio_account_snapshot(portfolio_source)
                        st.session_state[portfolio_source_key] = selected_snapshot
                        st.rerun()
                    except Exception as error:
                        selected_snapshot = {
                            "available": False,
                            "source": portfolio_source,
                            "message": str(error),
                        }
                        st.session_state[portfolio_source_key] = selected_snapshot

                if selected_snapshot:
                    if selected_snapshot.get("available"):
                        balance = selected_snapshot.get("balance")
                        cash_available = selected_snapshot.get("cash_available")
                        currency = selected_snapshot.get("currency") or "USD"
                        if balance is not None:
                            st.caption(f"Selected balance: {currency} {float(balance):,.2f}")
                        if cash_available is not None and cash_available != balance:
                            st.caption(f"Available cash: {currency} {float(cash_available):,.2f}")
                        if st.button(
                            "Sync capital from selected account",
                            key="sync_portfolio_capital",
                            disabled=balance is None,
                        ):
                            try:
                                set_starting_capital(float(balance))
                                st.success("Starting capital updated from the selected account balance.")
                                st.rerun()
                            except ValueError as error:
                                st.error(str(error))
                    else:
                        st.warning(selected_snapshot.get("message", "Selected account balance is unavailable."))

            st.divider()
            st.subheader("Trade Oversight")
            if st.button("🔍 Audit all trades", key="run_overwatcher_audit"):
                with st.spinner("Running trade configuration audit..."):
                    overwatcher = OverwatcherAgent()
                    audit_result = overwatcher.audit_all_trades()
                st.success(f"✓ Audit complete: {audit_result['total_trades_audited']} trades checked")
                st.caption(f"Log file: audit_logs/{audit_result['audit_log_file'].split('/')[-1]}")
                st.rerun()

    with st.sidebar.expander('Broker settings', expanded=False, key='lazy_main_3', on_change="rerun") as lazy_panel:
        if lazy_panel.open:
            with st.form("broker_settings"):
                broker_provider = st.selectbox(
                    "Broker provider",
                    ["dummy", "etoro"],
                    index=["dummy", "etoro"].index(os.getenv("FINBOT_BROKER_PROVIDER", "dummy").lower())
                    if os.getenv("FINBOT_BROKER_PROVIDER", "dummy").lower() in {"dummy", "etoro"}
                    else 0,
                    help="dummy is local simulation; eToro is demo-only. Live order execution is blocked.",
                )
                st.caption("Changing the provider updates the live environment and persists to .env.config.")
                st.markdown("**eToro**")
                st.caption("eToro API access requires both the public API key and the user key; demo balance checks can also use an optional demo account ID.")
                etoro_base_url = st.text_input(
                    "eToro API base URL",
                    value=os.getenv("ETORO_API_BASE_URL", "https://public-api.etoro.com"),
                    help="Leave unchanged unless your account uses a different eToro API host.",
                )
                etoro_api_key = st.text_input(
                    "eToro public API key",
                    value=os.getenv("ETORO_API_KEY", ""),
                    type="password",
                )
                etoro_user_key = st.text_input(
                    "eToro user key",
                    value=os.getenv("ETORO_USER_KEY", ""),
                    type="password",
                )
                etoro_main_account_id = st.text_input(
                    "eToro main account ID",
                    value=os.getenv("ETORO_MAIN_ACCOUNT_ID", ""),
                    help="Optional read-only account balance lookup. This does not enable live order execution.",
                )
                etoro_demo_account_id = st.text_input(
                    "eToro demo account ID",
                    value=os.getenv("ETORO_DEMO_ACCOUNT_ID", ""),
                    help="Optional. Needed only if you want to test the demo balance endpoint as well.",
                )
                st.checkbox("eToro demo trading (live route disabled)", value=True, disabled=True)
                etoro_api_debug_logging = st.checkbox(
                    "Enable eToro API debug logs",
                    value=get_etoro_api_debug_logging(),
                    help="Store masked eToro request and response details on each trade event. Turn off to avoid extra log storage.",
                )
                st.markdown("**Interactive Brokers**")
                st.caption("Legacy account fields only; this strategy does not submit IBKR orders.")
                ibkr_host = st.text_input("IBKR host", value=os.getenv("IBKR_HOST", "127.0.0.1"))
                ibkr_port = st.number_input("IBKR port", min_value=1, max_value=65535, value=int(os.getenv("IBKR_PORT", "7497")))
                ibkr_client_id = st.number_input("IBKR client id", min_value=0, value=int(os.getenv("IBKR_CLIENT_ID", "1")))
                ibkr_main_account_id = st.text_input(
                    "IBKR main account ID",
                    value=os.getenv("IBKR_MAIN_ACCOUNT_ID", os.getenv("IBKR_ACCOUNT_ID", "")),
                )
                ibkr_demo_account_id = st.text_input(
                    "IBKR demo account ID",
                    value=os.getenv("IBKR_DEMO_ACCOUNT_ID", ""),
                )
                ibkr_paper_trading = st.checkbox(
                    "Use IBKR paper trading",
                    value=os.getenv("IBKR_PAPER_TRADING", "true").lower() in {"1", "true", "yes", "on"},
                )
                if st.form_submit_button("Save broker settings"):
                    try:
                        save_env_config({
                            "FINBOT_BROKER_PROVIDER": broker_provider,
                            "ETORO_API_BASE_URL": etoro_base_url.strip(),
                            "ETORO_API_KEY": etoro_api_key.strip(),
                            "ETORO_USER_KEY": etoro_user_key.strip(),
                            "ETORO_MAIN_ACCOUNT_ID": etoro_main_account_id.strip(),
                            "ETORO_DEMO_ACCOUNT_ID": etoro_demo_account_id.strip(),
                            "ETORO_DEMO_TRADING": "true",
                            "IBKR_HOST": ibkr_host.strip(),
                            "IBKR_PORT": str(int(ibkr_port)),
                            "IBKR_CLIENT_ID": str(int(ibkr_client_id)),
                            "IBKR_ACCOUNT_ID": ibkr_main_account_id.strip(),
                            "IBKR_MAIN_ACCOUNT_ID": ibkr_main_account_id.strip(),
                            "IBKR_DEMO_ACCOUNT_ID": ibkr_demo_account_id.strip(),
                            "IBKR_PAPER_TRADING": "true" if ibkr_paper_trading else "false",
                        })
                        set_etoro_api_debug_logging(etoro_api_debug_logging)
                        st.success("Broker settings saved.")
                        st.rerun()
                    except Exception as error:
                        st.error(f"Could not save broker settings: {error}")

            validate_clicked = st.button(
                "Validate eToro credentials",
                key="validate_etoro_credentials",
                help="Runs a read-only eToro market-data lookup using the configured public API key and user key.",
            )
            if validate_clicked:
                with st.spinner("Checking eToro authentication and market-data access..."):
                    try:
                        validation = asyncio.run(integrations_module.validate_etoro_credentials())
                        st.session_state["etoro_validation_result"] = validation
                    except Exception as error:
                        st.session_state["etoro_validation_result"] = {
                            "available": False,
                            "status": "error",
                            "message": str(error),
                            "source": "eToro Public API",
                        }

            validation_result = st.session_state.get("etoro_validation_result")
            if validation_result:
                if validation_result.get("available"):
                    st.success(validation_result.get("message", "eToro credentials validated."))
                else:
                    st.warning(validation_result.get("message", "Some eToro read-only checks failed."))

                checks = validation_result.get("checks", {})
                market_check = checks.get("market_data", {})
                portfolio_check = checks.get("demo_portfolio", {})
                balance_check = checks.get("demo_balance", {})

                st.markdown("**Validation results**")
                st.write(
                    f"Market data lookup: {'passed' if market_check.get('available') else 'failed'}"
                )
                if market_check.get("available"):
                    market_bits = [f"Symbol {market_check.get('quote_symbol') or market_check.get('symbol')}"]
                    if market_check.get("instrument_id") is not None:
                        market_bits.append(f"Instrument ID {market_check['instrument_id']}")
                    if market_check.get("quote_type"):
                        market_bits.append(f"Quote type {market_check['quote_type']}")
                    st.caption(" · ".join(market_bits))
                else:
                    st.caption(market_check.get("error", "No market-data details returned."))

                st.write(f"Demo portfolio snapshot: {'passed' if portfolio_check.get('available') else 'failed'}")
                if not portfolio_check.get("available"):
                    st.caption(portfolio_check.get("error", "No portfolio details returned."))

                if balance_check.get("skipped"):
                    st.caption(balance_check.get("message", "Demo balance check skipped."))
                else:
                    st.write(f"Demo balance endpoint: {'passed' if balance_check.get('available') else 'failed'}")
                    if not balance_check.get("available"):
                        st.caption(balance_check.get("error", "No demo balance details returned."))

    with st.sidebar.expander(
        "Trade email notifications", expanded=False, key="lazy_trade_email_settings", on_change="rerun"
    ) as lazy_panel:
        if lazy_panel.open:
            st.caption(
                "Send an email after eToro acknowledges a demo order. The message includes every agent-stage decision "
                "and an optional sanitized JSON evidence attachment. Fill and protection remain pending until reconciliation."
            )
            current_email = load_env_config()
            security_options = ["starttls", "tls", "none"]
            current_security = current_email.get(
                "FINBOT_SMTP_SECURITY", os.getenv("FINBOT_SMTP_SECURITY", "starttls")
            ).lower()
            if current_security not in security_options:
                current_security = "starttls"
            with st.form("trade_email_settings"):
                email_enabled = st.checkbox(
                    "Email successful demo submissions",
                    value=current_email.get(
                        "FINBOT_TRADE_EMAIL_ENABLED", os.getenv("FINBOT_TRADE_EMAIL_ENABLED", "false")
                    ).lower() in {"1", "true", "yes", "on"},
                )
                smtp_host = st.text_input(
                    "SMTP host", value=current_email.get("FINBOT_SMTP_HOST", os.getenv("FINBOT_SMTP_HOST", ""))
                )
                smtp_port = st.number_input(
                    "SMTP port", min_value=1, max_value=65535,
                    value=int(current_email.get("FINBOT_SMTP_PORT", os.getenv("FINBOT_SMTP_PORT", "587"))),
                    step=1,
                )
                smtp_security = st.selectbox(
                    "Connection security",
                    security_options,
                    index=security_options.index(current_security),
                    format_func=lambda value: {
                        "starttls": "STARTTLS (usually port 587)",
                        "tls": "Implicit TLS (usually port 465)",
                        "none": "None",
                    }[value],
                )
                smtp_username = st.text_input(
                    "SMTP username",
                    value=current_email.get("FINBOT_SMTP_USERNAME", os.getenv("FINBOT_SMTP_USERNAME", "")),
                )
                smtp_password = st.text_input(
                    "SMTP password", type="password",
                    help="Leave blank to preserve the currently configured password.",
                )
                smtp_from = st.text_input(
                    "From address",
                    value=current_email.get("FINBOT_SMTP_FROM", os.getenv("FINBOT_SMTP_FROM", "")),
                )
                email_to = st.text_area(
                    "Recipient email addresses",
                    value=current_email.get("FINBOT_TRADE_EMAIL_TO", os.getenv("FINBOT_TRADE_EMAIL_TO", "")).replace(",", "\n"),
                    help="Enter one address per line, or separate addresses with commas or semicolons.",
                )
                attach_json = st.checkbox(
                    "Attach sanitized lifecycle evidence as JSON",
                    value=current_email.get(
                        "FINBOT_TRADE_EMAIL_ATTACH_JSON", os.getenv("FINBOT_TRADE_EMAIL_ATTACH_JSON", "true")
                    ).lower() in {"1", "true", "yes", "on"},
                )
                if st.form_submit_button("Save trade email settings"):
                    try:
                        recipients = list(parse_recipient_addresses(email_to))
                        if email_enabled and (not smtp_host.strip() or not smtp_from.strip() or not recipients):
                            raise ValueError("Enabled email requires an SMTP host, from address, and recipient address.")
                        updates = {
                            "FINBOT_TRADE_EMAIL_ENABLED": "true" if email_enabled else "false",
                            "FINBOT_SMTP_HOST": smtp_host.strip(),
                            "FINBOT_SMTP_PORT": str(int(smtp_port)),
                            "FINBOT_SMTP_SECURITY": smtp_security,
                            "FINBOT_SMTP_USERNAME": smtp_username.strip(),
                            "FINBOT_SMTP_FROM": smtp_from.strip(),
                            "FINBOT_TRADE_EMAIL_TO": ",".join(recipients),
                            "FINBOT_TRADE_EMAIL_ATTACH_JSON": "true" if attach_json else "false",
                        }
                        if smtp_password:
                            updates["FINBOT_SMTP_PASSWORD"] = smtp_password
                        save_env_config(updates)
                        st.success("Trade email settings saved. Automated cycles read these values without a restart.")
                        st.rerun()
                    except (ValueError, OverflowError) as error:
                        st.error(str(error))
            smtp_test_col, delivery_test_col = st.columns(2)
            if smtp_test_col.button("Test SMTP connection", key="test_smtp_connection"):
                try:
                    with st.spinner("Checking SMTP encryption and authentication..."):
                        result = test_smtp_connection()
                    st.success(
                        f"SMTP connected with {result['security'].upper()}; "
                        f"authentication {'passed' if result['authenticated'] else 'was not configured'}. No email was sent."
                    )
                except Exception as error:
                    st.error(f"SMTP connection test failed: {type(error).__name__}: {error}")
            if delivery_test_col.button(
                "Send sample trade email",
                key="send_sample_trade_email",
                help="Emails the latest broker-linked lifecycle as an SMTP TEST. It does not submit or modify a trade.",
            ):
                connection = backend_module.get_connection()
                try:
                    latest = connection.execute(
                        "SELECT id FROM trades WHERE broker_order_id IS NOT NULL OR broker_position_id IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                finally:
                    connection.close()
                if not latest:
                    st.warning("No broker-linked lifecycle is available for a sample email.")
                else:
                    try:
                        with st.spinner("Sending the explicitly labeled SMTP test report..."):
                            result = send_trade_execution_email(
                                Path(backend_module.DB_PATH), int(latest["id"]), test_mode=True
                            )
                        st.success(
                            f"SMTP test report for trade #{latest['id']} sent to "
                            f"{result['recipient_count']} configured recipient(s)."
                        )
                    except Exception as error:
                        st.error(f"SMTP delivery test failed: {type(error).__name__}: {error}")

    with st.sidebar.expander('AI research settings', expanded=False, key='lazy_main_4', on_change="rerun") as lazy_panel:
        if lazy_panel.open:
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

    with st.sidebar.expander('News research strategy and evaluation', expanded=False, key='lazy_main_5', on_change="rerun") as lazy_panel:
        if lazy_panel.open:
            st.caption("eToro research discovers candidates from current Yahoo headlines, adds Yahoo company data and price trends, then asks OpenAI for evidence-based analysis. Candidate names are not hard-coded.")
            with st.form("daily_strategy_config"):
                research_candidate_limit = st.number_input("Maximum news-discovered companies analyzed per cycle", min_value=1, max_value=20, value=int(os.getenv("FINBOT_RESEARCH_MAX_CANDIDATES", "8")), step=1)
                risk_trade = st.text_input("Risk per trade (fraction of equity)", value=os.getenv("FINBOT_RISK_PER_TRADE_PCT", "0.005"))
                risk_total = st.text_input("Maximum aggregate stop risk (fraction)", value=os.getenv("FINBOT_MAX_TOTAL_RISK_PCT", "0.02"))
                max_positions = st.number_input(
                    "Maximum open positions", min_value=1, max_value=100,
                    value=max(1, min(100, int(os.getenv("FINBOT_MAX_OPEN_POSITIONS", "30")))),
                    step=1,
                )
                max_gross = st.text_input("Maximum gross exposure (fraction of equity)", value=os.getenv("FINBOT_MAX_GROSS_EXPOSURE_PCT", "1.0"))
                spread_cost = st.text_input("Backtest spread bps per side", value=os.getenv("FINBOT_BACKTEST_SPREAD_BPS_PER_SIDE", ""))
                commission_cost = st.text_input("Backtest commission bps per side", value=os.getenv("FINBOT_BACKTEST_COMMISSION_BPS_PER_SIDE", ""))
                financing_cost = st.text_input("Annual financing percent", value=os.getenv("FINBOT_BACKTEST_FINANCING_ANNUAL_PCT", ""))
                fx_cost = st.text_input("Backtest FX bps per side", value=os.getenv("FINBOT_BACKTEST_FX_BPS_PER_SIDE", ""))
                dd_limit = st.text_input("Maximum holdout drawdown percent", value=os.getenv("FINBOT_BACKTEST_MAX_DRAWDOWN_PCT", "20"))
                if st.form_submit_button("Save strategy settings"):
                    try:
                        for name, value in (("risk per trade", risk_trade), ("aggregate risk", risk_total), ("gross exposure", max_gross), ("drawdown", dd_limit)):
                            number = float(value)
                            if not math.isfinite(number) or number <= 0:
                                raise ValueError(f"{name} must be a positive number.")
                        for name, value in (("spread", spread_cost), ("commission", commission_cost), ("financing", financing_cost), ("FX", fx_cost)):
                            if value.strip() and (not math.isfinite(float(value)) or float(value) < 0):
                                raise ValueError(f"{name} cost must be zero or positive.")
                        if int(max_positions) < 1:
                            raise ValueError("Maximum open positions must be at least 1.")
                        save_env_config({
                            "FINBOT_RESEARCH_MAX_CANDIDATES": str(int(research_candidate_limit)),
                            "FINBOT_RISK_PER_TRADE_PCT": risk_trade,
                            "FINBOT_MAX_TOTAL_RISK_PCT": risk_total,
                            "FINBOT_MAX_OPEN_POSITIONS": str(int(max_positions)),
                            "FINBOT_MAX_GROSS_EXPOSURE_PCT": max_gross,
                            "FINBOT_BACKTEST_SPREAD_BPS_PER_SIDE": spread_cost,
                            "FINBOT_BACKTEST_COMMISSION_BPS_PER_SIDE": commission_cost,
                            "FINBOT_BACKTEST_FINANCING_ANNUAL_PCT": financing_cost,
                            "FINBOT_BACKTEST_FX_BPS_PER_SIDE": fx_cost,
                            "FINBOT_BACKTEST_MAX_DRAWDOWN_PCT": dd_limit,
                        })
                        st.success("Strategy settings saved to .env.config.")
                        st.rerun()
                    except (ValueError, OverflowError) as error:
                        st.error(str(error))
            if st.button("Check strategy evaluation", key="run_daily_strategy_eval"):
                with st.spinner("Checking whether historical news inputs are available for a chronological evaluation..."):
                    try:
                        st.session_state["daily_strategy_evaluation"] = asyncio.run(run_strategy_evaluation())
                    except Exception as error:
                        st.session_state["daily_strategy_evaluation"] = {"status": "inconclusive", "reason": f"Evaluation failed: {type(error).__name__}: {error}"}
            # Read the version-checked persisted result so an old cached SMA result
            # cannot appear to authorize the current news-research strategy.
            evaluation = get_strategy_evaluation()
            st.write(f"Evaluation status: **{evaluation.get('status', 'not_run')}**")
            if evaluation.get("reason"):
                st.caption(evaluation["reason"])
            metrics = {key: evaluation.get(key) for key in (
                "holdout_start", "holdout_end", "net_return_pct", "benchmark_return_pct",
                "max_drawdown_pct", "trade_count", "turnover", "costs", "cost_assumptions",
            ) if evaluation.get(key) is not None}
            if metrics:
                st.json(metrics)
            evaluation_current = get_strategy_evaluation()
            broker_is_etoro_demo = (
                os.getenv("FINBOT_BROKER_PROVIDER", "dummy").strip().lower() == "etoro"
                and os.getenv("ETORO_DEMO_TRADING", "true").strip().lower() in {"1", "true", "yes", "on"}
            )
            can_enable_demo = broker_is_etoro_demo
            requested_demo_orders = st.checkbox(
                "Enable automated eToro demo orders",
                value=get_demo_automated_trading_enabled(),
                disabled=not can_enable_demo,
                help="Enables automatic eToro demo orders for approved candidates. The preference stays enabled after a trade; known position warnings do not disable automation; uncertain submissions are resolved before new account risk is added.",
            )
            if requested_demo_orders != get_demo_automated_trading_enabled():
                try:
                    set_demo_automated_trading_enabled(requested_demo_orders)
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
            if not can_enable_demo:
                st.caption("Select eToro demo mode to enable automated demo orders. This strategy never sends live orders.")
            else:
                st.caption("Automation remains enabled after successful trades and reconciliation warnings. Each new order still requires verified account equity, a complete broker position snapshot, and enough position-level stop, fill, and quantity data to calculate total risk.")
            st.caption("Evaluation is shown for performance context only. Broker-side stop confirmation is still required after an order fills.")

    broker_provider = os.getenv("FINBOT_BROKER_PROVIDER", "dummy").strip().lower()
    if broker_provider == "etoro":
        automated = "enabled" if get_demo_automated_trading_enabled() else "disabled"
        st.caption(f"Order mode: eToro demo only · automated demo orders {automated}")
    else:
        st.caption("Order mode: local simulation · no broker orders are submitted")

    render_research_actions()

    navigation_col, range_col, refresh_col = st.columns([8, 1, 1], vertical_alignment="center", gap="small")
    with navigation_col:
        selected_view = st.segmented_control(
            "Main navigation",
            ["Dashboard", "Trade lifecycles", "Research", "Companies", "Oversight", "Profile", "How it works"],
            default="Dashboard",
            label_visibility="collapsed",
            key="main_navigation",
        )
    with range_col:
        start_at, end_at = render_time_range()
    with refresh_col:
        if st.button("🔄", help="Refresh page", key="page_refresh_button", use_container_width=True):
            clear_dashboard_caches()
            st.rerun()

    st.session_state["lifecycle_end_at"]=end_at
    if st.session_state.get("manual_research_result"):
        clear_dashboard_caches()
    summary = get_ui_dashboard_summary(str(backend_module.DB_PATH),start_at,end_at)

    manual_result = st.session_state.pop("manual_research_result", None)
    manual_error = st.session_state.pop("manual_research_error", None)
    if manual_error:
        st.error(f"Agent analysis failed: {manual_error}")
    if manual_result:
        with st.expander("Latest manual research run", expanded=True):
            news_result = manual_result.get("news")
            cycle_result = manual_result.get("cycle")
            if news_result is not None:
                st.write(
                    f"News fetch: {news_result['fetched']} headlines received, "
                    f"{news_result['inserted']} new and {news_result['updated']} existing articles refreshed."
                )
            if cycle_result:
                market_data = cycle_result.get("market_data")
                if cycle_result.get("mode") in {"eToro demo", "demo"} or not isinstance(market_data, dict):
                    cycle_label = "eToro demo" if cycle_result.get("mode") in {"eToro demo", "demo"} else "Trading"
                    st.write(f"{cycle_label} cycle: {cycle_result.get('status', 'unknown')} · {cycle_result.get('reason', '')}")
                    account = cycle_result.get("account")
                    account = account if isinstance(account, dict) else {}
                    st.write(f"Broker positions: {cycle_result.get('broker_position_count', 'unavailable')} · equity: {account.get('equity', 'unavailable')}")
                    if cycle_result.get("signals"):
                        st.dataframe(pd.DataFrame(cycle_result["signals"]).T, width="stretch")
                    if cycle_result.get("research_candidates"):
                        st.dataframe(pd.DataFrame([
                            {
                                "lifecycle_id": candidate.get("trade_id"),
                                "ticker": candidate.get("ticker"),
                                "company": candidate.get("company_name"),
                                "signal": candidate.get("signal"),
                                "confidence": candidate.get("confidence"),
                                "analysis": candidate.get("analysis_status"),
                                "analysis decision": candidate.get("analysis_decision"),
                                "execution gate": candidate.get("execution_status"),
                                "research_status": candidate.get("research_status"),
                                "headlines": (candidate.get("research_input") or {}).get("article_count"),
                                "quote_time": (candidate.get("research_input") or {}).get("quote_timestamp"),
                                "SMA trend": ((candidate.get("research_input") or {}).get("daily_trend_context") or {}).get("trend_state"),
                                "exchange": (candidate.get("research_input") or {}).get("primary_exchange"),
                                "exchange session": (candidate.get("research_input") or {}).get("market_session"),
                                "exchange local time": ((candidate.get("research_input") or {}).get("market_session_context") or {}).get("local_timestamp"),
                                "eToro quote state": (candidate.get("research_input") or {}).get("etoro_execution_state"),
                                "trend alignment": (((candidate.get("agent_workflow") or {}).get("strategist") or {}).get("decision_gate") or {}).get("daily_trend_alignment"),
                                "researcher": ((candidate.get("agent_workflow") or {}).get("researcher") or {}).get("decision"),
                                "strategist": ((candidate.get("agent_workflow") or {}).get("strategist") or {}).get("decision"),
                                "risk_manager": ((candidate.get("agent_workflow") or {}).get("risk_manager") or {}).get("decision"),
                                "execution": ((candidate.get("agent_workflow") or {}).get("execution") or {}).get("decision"),
                                "analysis findings": "; ".join(candidate.get("analysis_reasons") or []),
                                "execution gate reasons": "; ".join(candidate.get("execution_reasons") or []),
                            }
                            for candidate in cycle_result["research_candidates"]
                        ]), hide_index=True, width="stretch")
                        with st.expander("Agent decisions and evidence"):
                            for candidate in cycle_result["research_candidates"]:
                                st.markdown(f"**{candidate.get('ticker')} · {candidate.get('company_name')}**")
                                st.json(candidate.get("agent_workflow") or {})
                                st.caption("Research inputs and OpenAI findings")
                                st.json({
                                    "research_input": candidate.get("research_input"),
                                    "research_summary": candidate.get("research_summary"),
                                    "fundamental_analysis": candidate.get("fundamental_analysis"),
                                    "analysis_status": candidate.get("analysis_status"),
                                    "analysis_decision": candidate.get("analysis_decision"),
                                    "analysis_reasons": candidate.get("analysis_reasons"),
                                    "execution_status": candidate.get("execution_status"),
                                    "execution_reasons": candidate.get("execution_reasons"),
                                })
                    if cycle_result.get("trades"):
                        st.dataframe(pd.DataFrame(cycle_result["trades"]), hide_index=True, width="stretch")
                else:
                    headlines = market_data.get("recent_news", [])
                    relevance = market_data.get("headline_relevance", {})
                    st.write(
                        f"Analyzed {len(headlines)} headlines: {relevance.get('relevant', 0)} relevant, "
                        f"{relevance.get('not_relevant', 0)} not relevant."
                    )
                    research_mode = "OpenAI research" if cycle_result.get("ai_research_enabled") else "Dummy research"
                    st.write(f"Research mode: {research_mode}")
                    opened_count = sum(trade.get("status") == "OPEN" for trade in cycle_result["trades"])
                    deferred_count = sum(trade.get("status") == "DEFERRED_CAPACITY" for trade in cycle_result["trades"])
                    st.write(
                        f"Candidate pool: {market_data.get('candidate_pool_size', 0)} · "
                        f"analyzed: {cycle_result.get('research_candidate_count', 0)} · "
                        f"risk-approved: {cycle_result.get('risk_approved_candidate_count', 0)} · "
                        f"opened: {opened_count} of {cycle_result.get('max_trades_per_cycle', 0)} allowed · "
                        f"deferred by quota: {deferred_count}."
                    )
                    st.caption("This local simulation ranks research heuristics; those scores are not used as probabilities of profit.")
                    st.dataframe(pd.DataFrame(cycle_result["trades"]), hide_index=True, width="stretch")

    st.sidebar.subheader("Agent rule editor")
    for agent_name, default_rules in DEFAULT_AGENT_RULES.items():
        agent_rules = next((agent["rules"] for agent in summary.get("agents", []) if agent["agent_name"] == agent_name), default_rules)
        with st.sidebar.expander(agent_name.title(), key=f"agent_settings_{agent_name}", on_change="rerun") as lazy_panel:
            if lazy_panel.open:
                with st.form(f"edit_rules_{agent_name}"):
                    updated = {}
                    for key, value in agent_rules.items():
                        inactive = {
                            "researcher": {"allowed_sectors", "max_fetch_per_cycle", "default_leverage", "include_market_fundamentals"},
                            "strategist": {"position_limit", "require_current_market_quote"},
                            "risk_manager": {"max_drawdown", "max_volatility", "exit_on_risk_breach", "stop_loss_pct", "openai_trend_review_enabled", "trend_opposition_min_confidence"},
                            "execution": {"max_trade_value", "paper_trading_only"},
                        }
                        if key == "status" or os.getenv("FINBOT_BROKER_PROVIDER", "dummy").lower() == "etoro" and key in inactive.get(agent_name, set()):
                            continue
                        label = key.replace("_", " ").title()
                        help_text = AGENT_RULE_HELP.get(key, "Rule applied by this agent.")
                        if key in {"default_leverage", "max_leverage"} and agent_name in {"researcher", "strategist"}:
                            try:
                                leverage_value = int(float(value))
                            except (TypeError, ValueError):
                                leverage_value = 2
                            updated[key] = st.number_input(
                                label, min_value=1, value=leverage_value, step=1, help=help_text
                            )
                        elif key == "conditional_mean_reversion_min_atr_multiple" and agent_name == "strategist":
                            updated[key] = st.number_input(
                                label, min_value=0.1, max_value=10.0, value=float(value), step=0.05,
                                format="%.2f", help=help_text,
                            )
                        elif isinstance(value, bool):
                            if agent_name == "risk_manager" and key == "enabled":
                                st.checkbox("Enabled (required safety gate)", value=True, disabled=True, help=help_text)
                                updated[key] = True
                            elif agent_name == "execution" and key == "paper_trading_only":
                                st.checkbox("Local simulator is paper-only (locked)", value=True, disabled=True, help=help_text)
                                if os.getenv("FINBOT_BROKER_PROVIDER", "dummy").strip().lower() == "etoro":
                                    st.caption("The selected eToro provider handles approved orders; this local simulator rule does not override it.")
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
                            updated[key] = st.number_input(label, min_value=1, value=int(value), step=1, help=help_text)
                        elif isinstance(value, float):
                            updated[key] = st.number_input(label, value=float(value), step=0.01, help=help_text)
                        else:
                            updated[key] = st.text_input(label, value=str(value), help=help_text)
                    if st.form_submit_button(f"Save {agent_name} rules"):
                        try:
                            update_agent_rules(agent_name, updated)
                            clear_dashboard_caches()
                            st.sidebar.success(f"{agent_name.title()} rules updated.")
                            st.rerun()
                        except ValueError as error:
                            st.sidebar.error(str(error))

    if selected_view == "Dashboard":
        render_summary(summary)
        render_graphs(summary)
    elif selected_view == "Trade lifecycles":
        render_trade_search(summary["trades"])
    elif selected_view == "Research":
        render_research_headlines(summary)
    elif selected_view == "Companies":
        render_companies_dashboard()
    elif selected_view == "Oversight":
        render_oversight_dashboard()
    elif selected_view == "Profile":
        render_profile_page(st.session_state.get("username", ""))
    else:
        render_how_it_works()


if __name__ == "__main__":
    main()
