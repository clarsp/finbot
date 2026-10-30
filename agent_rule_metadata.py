"""Shared descriptions for local controls and the reporting configuration mirror."""
AGENT_RULE_HELP = {'profit_review_exits_enabled': 'Allow a news or sizing review to request closure only above the '
                                'conservative net-profit floor. Never authorizes a discretionary losing '
                                'exit. SL/TP and explicit user closes remain active.',
 'minimum_reward_risk': 'Minimum net target reward divided by stop distance. Zero allows any strictly '
                        'positive estimated net target profit; other risk and execution checks still apply.',
 'profit_exit_min_net_usd': 'Minimum estimated net profit after fees, conservative financing and slippage '
                            'reserve. Default $1; combined with the margin percentage floor by taking the '
                            'larger value.',
 'profit_exit_min_margin_pct': 'Minimum estimated net profit as a percentage of actual position margin, '
                               'after reserves. Default 0.5%.',
 'profit_exit_slippage_bps': 'Additional reserve on entry notional before a discretionary profit exit, at '
                             'least two current spreads. Default 10 basis points. Actual market fills can '
                             'still differ.',
 'pending_entry_max_minutes': 'Cancel a broker-confirmed unfilled pending demo entry after this validity '
                              'period. Default 60 minutes. An acknowledgment does not confirm cancellation; '
                              'fills always remain protected.',
 'recovery_max_attempts': 'Maximum writes for one broker action, including the initial request. Uncertain '
                          'closes are never blindly resent. Default 3; range 1–5.',
 'recovery_initial_check_seconds': 'Initial per-action read interval, with exponential backoff and '
                                   'deterministic jitter. Minimum 60 seconds.',
 'recovery_max_check_seconds': 'Maximum normal backoff before jitter; stock weekend queues use at least 15 '
                               'minutes. Broker Retry-After may require longer.',
 'recovery_escalate_hours': 'Unresolved business-day age before one persisted attention event. Weekend time '
                            'is excluded from age-only alerts; broker errors can alert immediately.',
 'structure_target_mode': 'range_boundary: aim within the outer recent monthly range while recording nearer '
                          'obstacles. nearest: cap at the closest opposing level. Both preserve the ATR '
                          'ceiling and existing risk gates.',
 'structure_range_months': 'Number of preceding calendar months, plus the current evolving month, used for '
                           'the monthly high/low boundary. Only completed daily bars are used. Default 1.',
 'structure_range_weeks': 'Weekly fallback lookback in calendar weeks when monthly boundary data is '
                          'unavailable. Default 4.',
 'max_target_atr_multiple': 'Maximum initial directional target distance in ATR14. Initial target = min(stop '
                            'ATR × target reward/risk, this ceiling); daily/weekly/monthly structure may '
                            'shorten it. Mean-reversion targets retain their measured news anchor. Default 3 '
                            'preserves the existing default.',
 'target_reward_risk': 'Initial directional target distance relative to the stop. With a 2 ATR stop and 1.5R '
                       'this is 3 ATR, subject to the maximum target ATR and support/resistance caps.',
 'shadow_tracking_enabled': 'Track prospective alternatives after an approved directional order fills. Sends '
                            'no extra orders and changes no actual exits. Existing positions are not '
                            'retroactively enrolled.',
 'shadow_stop_atr_multiples': 'Counterfactual stop distances; wider stops reduce simulated units to preserve '
                              'original planned stop risk.',
 'shadow_target_atr_multiples': 'Counterfactual target distances, preserving the earlier nearest '
                                'daily/weekly/monthly support/resistance rule even when production uses '
                                'range_boundary.',
 'shadow_horizons_hours': 'Additional comparison holding limits; production holding limit is always '
                          'included. 168h = 7 calendar days; 336h = 14 calendar days.',
 'risk_per_trade_pct': 'Maximum planned entry-to-stop risk for one new position as a fraction of verified '
                       'account equity.',
 'max_total_risk_pct': 'Maximum aggregate planned stop risk across verified open positions and the proposed '
                       'order.',
 'max_gross_exposure_pct': 'Maximum total gross notional exposure as a fraction of verified account equity.',
 'max_open_positions': 'Maximum number of verified broker positions allowed before another order is '
                       'deferred.',
 'max_single_position_exposure_pct': 'Maximum gross notional for one position as a fraction of account '
                                     'equity.',
 'stress_gap_pct': 'Additional adverse gap assumption, as a fraction of price, added to nominal stop risk. '
                   'This is a scenario, not a loss guarantee.',
 'max_stressed_loss_pct': 'Maximum portfolio loss under the configured stop-plus-gap stress scenario, as a '
                          'fraction of equity.',
 'stop_atr_multiple': 'Distance from entry to stop loss in ATR14 units. The current default is 2× ATR14.',
 'sector_exposure_enabled': 'Optional sector concentration gate. Requires verified sector labels for every '
                            'broker holding; unknown sectors block new orders when enabled.',
 'max_sector_exposure_pct': 'Maximum gross notional per sector as a fraction of equity when the sector gate '
                            'is enabled.',
 'max_order_units': 'Hard quantity ceiling per new order. Fractional units are allowed. Default: 5 units.',
 'max_order_spend_usd': 'Maximum estimated account capital per order, including a reserve for costs. At 2× '
                        'leverage, $200 margin can provide up to $400 exposure. The units cap also applies.',
 'analysis_retention_days': 'Archive never-submitted analysis after this many days. Broker orders, fills, '
                            'uncertain submissions and open positions are preserved. Default: 3 days.',
 'conditional_mean_reversion_confirmation_atr': 'Required completed-bar recovery from the post-news extreme '
                                                'before entering the reversal.',
 'conditional_mean_reversion_min_confidence': 'Research confidence required for a temporary news reaction; '
                                              'technical distance never overrides it.',
 'conditional_mean_reversion_stop_cushion_atr': 'Place the protective stop this many ATR units beyond the '
                                                'measured news-reaction extreme.',
 'max_research_age_hours': 'A manual candidate older than this needs fresh research before submission.',
 'news_max_hold_hours': 'Directional trade reassessment interval and cost-estimation horizon. Elapsed time '
                        'never triggers a close.',
 'mean_reversion_max_hold_hours': 'Mean-reversion reassessment interval and cost-estimation horizon. Elapsed '
                                  'time never triggers a close.',
 'estimated_roundtrip_cost_bps': 'Fallback round-trip trading cost estimate in basis points when broker cost '
                                 'evidence is unavailable.',
 'estimated_financing_annual_pct': 'Fallback annual financing-rate estimate used over the planned holding '
                                   'period.',
 'enabled': 'Whether this agent participates in analysis cycles.',
 'min_confidence': 'Minimum confidence, from 0.0 to 1.0, needed to pass this decision gate.',
 'allowed_sectors': 'Research candidates outside these sectors are rejected.',
 'max_fetch_per_cycle': 'Maximum number of unique feed candidates analyzed per cycle. The pool rotates '
                        'across cycles; with OpenAI enabled, each candidate may use a research call and a '
                        'separate risk trend-review call.',
 'include_market_fundamentals': 'Fetch Yahoo Finance company statements for strategist checks and include '
                                'them in OpenAI research when enabled.',
 'fundamental_confirmation_min_confidence': 'Minimum confidence when at least three company metrics and a '
                                            'directly matched headline support the proposed direction.',
 'countertrend_short_min_confidence': 'Minimum confidence for a short that conflicts with strong '
                                      'fundamentals or company-specific bullish news.',
 'countertrend_long_min_confidence': 'Minimum confidence for a long that conflicts with weak fundamentals or '
                                     'company-specific bearish news.',
 'conditional_mean_reversion_min_atr_multiple': 'Minimum measured post-news excursion from the pre-news '
                                                'price, in ATR14 multiples. A temporary news assessment and '
                                                'completed-bar reversal confirmation are also required.',
 'strong_fundamentals_min_revenue_growth_pct': 'Revenue growth threshold for one of the metrics used to '
                                               'identify strong company fundamentals.',
 'strong_fundamentals_min_net_profit_margin_pct': 'Net margin threshold for one of the metrics used to '
                                                  'identify strong company fundamentals.',
 'strong_fundamentals_max_debt_to_equity': 'Maximum debt/equity ratio for one of the metrics used to '
                                           'identify strong company fundamentals.',
 'weak_fundamentals_max_revenue_growth_pct': 'Revenue growth at or below this value counts as one of five '
                                             'weak-company indicators; at least three are required.',
 'weak_fundamentals_max_net_profit_margin_pct': 'Net margin at or below this value counts as one of five '
                                                'weak-company indicators; EPS and cash flow are also '
                                                'considered.',
 'weak_fundamentals_min_debt_to_equity': 'Debt/equity at or above this value counts as one of five '
                                         'weak-company indicators.',
 'require_company_specific_news_for_fundamental_confirmation': 'Require a company-specific headline in the '
                                                               'same direction before lowering the '
                                                               'confidence threshold for a '
                                                               'fundamentals-aligned trade.',
 'require_company_specific_news_for_countertrend_short': 'Require a directly company-linked bearish headline '
                                                         'before shorting against strong fundamentals or '
                                                         'bullish news.',
 'require_company_specific_news_for_countertrend_long': 'Require a directly company-linked bullish headline '
                                                        'before going long against weak fundamentals or '
                                                        'bearish news.',
 'require_current_market_quote': 'Execution requires a fresh eToro quote. Missing quotes do not block news '
                                 'or company analysis.',
 'default_leverage': "Starting leverage for a candidate; the Strategist's final leverage is persisted with "
                     'its exits.',
 'max_leverage': "Upper leverage limit for the strategist's proposed position.",
 'position_limit': 'Maximum fraction of starting capital allocated to one trade.',
 'max_drawdown': 'Risk rejection threshold for modeled account drawdown.',
 'max_volatility': 'Risk rejection threshold for modeled volatility.',
 'stop_loss_pct': 'Default stop-loss distance from entry price as a fraction.',
 'exit_on_risk_breach': 'Reject/exit if a configured risk threshold is breached. This safety gate is '
                        'required.',
 'openai_trend_review_enabled': 'After strategy approval, request an independent OpenAI review of Yahoo '
                                'price trends over 1 hour, 24 hours, this week, this year, and 5 years. This '
                                'uses additional API tokens.',
 'trend_opposition_min_confidence': 'Reject a proposed position when the OpenAI trend review strongly '
                                    'opposes it at or above this confidence.',
 'paper_trading_only': 'Controls the local simulator. When eToro is the selected broker, approved trades are '
                       'sent to the configured eToro demo endpoint; live orders remain blocked.',
 'max_trade_value': 'Maximum paper capital allocation for one trade.',
 'max_trades_per_cycle': 'Maximum number of eligible eToro demo orders submitted during one research cycle.',
 'allowed_side': 'LONG and/or SHORT directions permitted at the execution boundary.',
 'profit_trailing_enabled': 'Enable price trailing using the ATR captured at entry. LONG uses fresh '
                            'executable bid; SHORT uses ask. Quantity, leverage and dollar P/L do not affect '
                            'arming or exit decisions. Existing SL/TP remain active.',
 'profit_trailing_interval_minutes': 'Minutes between observations per position (1–1440). Default 30. '
                                     'Recorded best prices persist across restarts. Moves between checks can '
                                     'overshoot the trigger.',
 'profit_trailing_prime_arm_atr': 'Arm prime tracking when the best favorable price movement from the actual '
                                  'entry is strictly greater than this multiple of frozen entry ATR. Default '
                                  '0.5 ATR. Used only when arming basis is atr.',
 'profit_trailing_major_arm_atr': 'Upgrade to major when the best favorable movement exceeds this multiple '
                                  'of entry ATR. Default 1 ATR. Must exceed the prime arming level. Used only in atr arming mode.',
 'profit_trailing_prime_retreat_atr': 'Close a prime position after price retreats by at least this multiple '
                                      'of entry ATR from its best observed executable price. Default 0.15 '
                                      'ATR. In ATR arming mode must be smaller than prime arming.',
 'profit_trailing_major_retreat_atr': 'Close a major position after a retreat of at least this multiple of '
                                      'entry ATR from its best observed executable price. Default 0.25 ATR. '
                                      'In ATR arming mode must be smaller than major arming. Margin and leverage change cash '
                                      'P/L, not this price trigger.'}

AGENT_RULE_HELP['profit_trailing_mode'] = (
    'scheduled: Finbot requests exits on scheduled ATR retreats. '
    'broker: maintenance arms a broker-native trailing stop; scheduled observations continue to '
    'verify it. Until native protection is confirmed, scheduled exits remain the fallback. '
    'A wider major distance never loosens a tighter active prime stop. '
    'Switching modes does not remove an existing broker trailing stop. Native mode needs demo validation '
    'for broker minimum distances and activation behavior before use.')

AGENT_RULE_HELP.update({
 'profit_trailing_arming_basis':'Choose amount (default) or atr to arm profit protection. Amount uses the highest freshly observed broker unrealized USD profit; prime above $10, major above $20 by default. ATR uses favorable entry-ATR movement instead. These alternatives are not combined. Existing broker stops are never removed or loosened when changing basis. New observations start on a basis change; ATR retreat distances remain in effect.',
 'profit_trailing_prime_arm_usd':'Arm prime after observed broker unrealized profit strictly exceeds this USD amount. Default $10. Used only with amount arming. Broker P/L already incorporates position sizing/leverage; do not multiply leverage again.',
 'profit_trailing_major_arm_usd':'Upgrade to major after observed broker unrealized profit strictly exceeds this USD amount. Default $20; must exceed prime. Used only with amount arming. A wider major retreat does not loosen an existing tighter broker stop.',
 'profit_trailing_enabled':'Enable profit protection with selectable amount or ATR arming. Retreat distances always use frozen entry ATR; LONG uses bid and SHORT uses ask. Broker-native trailing and scheduled reconfirmation retain existing SL/TP protection.',
})

AGENT_RULE_LABELS = {'profit_trailing_arming_basis':'Arming basis','profit_trailing_prime_arm_usd':'Prime arming profit (USD)','profit_trailing_major_arm_usd':'Major arming profit (USD)','profit_trailing_mode':'Profit protection mechanism','profit_trailing_enabled': 'Enable profit protection', 'profit_trailing_interval_minutes': 'Price tracking interval (minutes)', 'profit_trailing_prime_arm_atr': 'Prime arming movement (entry ATR)', 'profit_trailing_major_arm_atr': 'Major arming movement (entry ATR)', 'profit_trailing_prime_retreat_atr': 'Prime retreat from best price (entry ATR)', 'profit_trailing_major_retreat_atr': 'Major retreat from best price (entry ATR)', 'news_max_hold_hours': 'News review interval (hours)', 'mean_reversion_max_hold_hours': 'Mean reversion review interval (hours)'}

def rule_metadata(rules):
    return {agent: {key: {"label": AGENT_RULE_LABELS.get(key, key.replace("_", " ").title()), "help": AGENT_RULE_HELP.get(key, ""), "unit": "USD" if key in {"profit_trailing_prime_arm_usd","profit_trailing_major_arm_usd"} else "ATR" if key.startswith("profit_trailing_") and key.endswith("_atr") else "minutes" if key == "profit_trailing_interval_minutes" else ""} for key in values if key != "status"} for agent, values in rules.items() if isinstance(values, dict)}
