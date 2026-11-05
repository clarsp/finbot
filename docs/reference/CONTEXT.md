# Current handoff — 24 October 2026

## 5 November: short-horizon market structure

- Added symmetric completed-bar SMA5/SMA14 entry direction.
- Added a 0.75 ATR minimum-room gate to confirmed opposing daily structure.
- Added beta plus ATR% as a capped 5% opportunity-speed ranking input.
- High beta can reduce allocation; volatility never grants direction.

- Finbot is a Python/Streamlit app. Sole local Compose container `finbot-finbot-1`,
  dashboard https://localhost:8501, external volume `finbot_migrated_20261022`,
  active SQLite `/data/finbot.db` inside Docker. Fly compute/volume retired.
- eToro demo remains admin/reference broker. Broker adapters are isolated in
  `brokers/`; existing trades/actions retain immutable origin broker ownership.
- Tenant foundation is deployed: admin-reference role, immutable trade/action
  ownership, owner-only planned entry destinations. Additional runtimes remain
  disabled until account-scoped settings, schedules, data, caches, credentials,
  workers and risk controls are implemented. See MULTITENANCY.md.
- Broker status distinguishes not submitted, explicit rejection, pending and
  uncertain transport. Closed-market instrument resolution and indicative quotes
  are separate from execution eligibility. No automatic stale-news entry retry.
- News Researcher sees at most eight deduplicated headlines, 400-character titles
  and 1,000-character summaries, selected fundamentals and market context. No
  full articles or independent browsing. Company/index identity is explicit.
- Cleanup validation: all 357 remaining tests passed. Four test files containing
  21 tests for retired Fly/correction tools were archived with their scripts.
  Documentation links and git diff whitespace checks passed.
- Workspace cleanup is documented in WORKSPACE.md. Read current reference docs
  selectively; historical archives are excluded from routine AI analysis.
- Working tree already contained extensive uncommitted application changes.
  Preserve them. No deployment or trading changes are part of this cleanup.

- 26 October rule update: positive net entry payoff (`minimum_reward_risk=0`),
  stops/targets and other gates preserved. Hourly broker-dollar-profit trailing
  enabled in shared demo: peaks >$10 arm $3 drawdown; peaks >$20 arm $5 drawdown.
  Persistent `profit_trailing` state starts at activation, not historical peaks.
  Every close/retry rechecks current broker identity/P&L using existing durable
  recovery. This policy is independent of the existing active $5/2% news-exit floor.

- Profit trailing now exposes interval, prime/major peak thresholds and drawdowns
  in Position Manager. Active/default interval is 30 minutes; dollar thresholds
  remain $10/$20 and drawdowns $3/$5. Current settings apply to persisted peaks
  and close revalidation without resetting tracking state.

- 27 October: optional private Telegram assistant added to the existing worker,
  disabled until the user creates a BotFather bot and configures the local admin
  sidebar. Supports recorded-data Q&A, allowlisted validated rule changes and
  new-entry pause/resume. Sender/chat IDs map to a normal active Finbot user;
  service_request preserves suspension/membership enforcement. Telegram update
  IDs, offsets and replies persist, with no interrupted mutation replay. No
  direct orders or code execution. See TELEGRAM.md for user setup.

- Telegram Q&A fix: supply broker-confirmed linked closed history separately from
  open positions and candidate creation dates. `/closed [1..20]` returns recent
  closures directly. Natural-language answers receive current closure evidence,
  sync timestamps and neutral-research gate facts; prior incorrect replies are
  not treated as evidence. No trading rules changed.

- Closure emails/reports now include reason, source (broker_reported,
  finbot_request, unknown) and supporting trigger details. Uses explicit broker
  text or matching submitted close actions; no SL/TP guess from P/L or prices.
  Position 3623057603 / JNJ: recorded prime trailing request, peak $10.67,
  check profit $5, drawdown $5.67, trigger $7.67. No historical email resend.

- Closed-trades GUI now defaults to This week, explains the Last completed week
  filter, has Refresh/Show latest controls, displays broker sync time and closure
  reason/source. Report queries were current; the prior default hid this week's
  exits. #1362 AXON / #1476 CRWD retrospective uses saved broker P/L only, with
  no broker calls: a 30-minute-from-entry replay triggered earlier/lower on these
  two paths. Offline artifact: /tmp/finbot-profit-review-20261027/ (HTML/CSV/method).


### Entry ATR price tracking — 28 October 2026

Position Manager now uses `profit_trailing_prime_arm_atr=0.5`,
`profit_trailing_major_arm_atr=1.0`, `profit_trailing_prime_retreat_atr=0.15`
and `profit_trailing_major_retreat_atr=0.25`, with configurable
`profit_trailing_interval_minutes=30`. Arming is strictly above the threshold;
a retreat at or beyond the threshold requests closure after a fresh identity
and executable quote check. LONG measures bid minus entry and follows the
highest bid; SHORT measures entry minus ask and follows the lowest ask, then
closes on an upward retreat. All distances use the ATR14 saved in the entry
policy, frozen alongside actual broker entry in `price_trailing`. Size, margin,
leverage and cash P/L do not affect the price trigger. Missing/invalid entry ATR
or quotes withhold this discretionary exit; broker protection remains active.
Existing open positions start new observed price peaks at activation; legacy
`profit_trailing` dollar history and closure evidence remain unchanged. Current
settings replace the four legacy USD trailing fields. The independent news-exit
net USD/margin floors and broker SL/TP retain their existing behavior.

Streamlit shows ATR field tooltips and recorded tracking state. Telegram exposes
the new ATR settings and recorded price triggers. The reporting export includes
shared labels/help/units; finbotdashboard shows snapshot time, current imported
ATR rules and accessible tooltips, identifying older snapshots explicitly.

- Deployment verification 28 October: sole existing container replaced with the
  ATR build and healthy; persisted enabled/30m/.5/1/.15/.25 verified. All ten
  current positions have entry ATR and produced price observations; old dollar
  history retained (17 rows). Independent news-exit floors remain $5/2%.
  Rollback image: `finbot:before-atr-trailing-20261028`. Python: 400 tests pass;
  dashboard: 24 tests/build and desktop/mobile keyboard-tooltip checks pass.
  Dashboard source/build updated in sibling `../finbotdashboard`. On 28 October
  the user explicitly approved the saved exact FTPS pin (with CA validation)
  for this deployment and the normal curated trade/account reporting sync.
  Static deployment is verified against the local build. The approved reporting
  sync completed with consumer status `imported`, generation
  `20261028T171906Z-3eda1d99fa93` (182 partitions, one configuration record).
  Both the dashboard frontend and updated reporting configuration are published.

- Optional native ATR trailing built on 28 October, deliberately unselected:
  `profit_trailing_mode=scheduled` by default; broker mode uses existing minute
  maintenance for arming plus unchanged scheduled verification. Current runtime
  interval was changed by the user in GUI to 15 minutes at 18:34 CET. Preserve it.
  New native_trailing.py coordinates peak updates and broker-confirmed protection;
  broker_recovery uses durable typed protection intents and never blindly resets
  uncertain native trails. eToro normalizes isTslEnabled and supports an explicit
  trailing stop type. Major widening retains a tighter prime stop. Scheduled and
  observed-stop close paths avoid competing with verified native protection.
  Native demo activation/distance semantics have not been exercised against the
  broker, as requested. The existing exchange-open conflict is not fixed here;
  dedicated eligibility remains a proposed execution-only adapter extension.

- Native trailing validation: all 421 tests pass, including actual main-maintenance orchestration with a mocked broker, both directions, inactive default, fresh guards, peak retention, tighter major handling, continuing verification and no duplicate native resets/competing close. Built image `20d171e2b8dd`; rollback `finbot:before-native-trailing-20261028`. Deployment completed: sole container healthy, persisted mode `scheduled`, interval 15 minutes, tracking enabled and zero native ATR actions. The broker option remains deliberately unselected.


- Broker trailing activated by explicit user request on 28 October at about
  20:40 CET: `profit_trailing_mode=broker`, enabled, 15-minute scheduled
  reconfirmation retained, prime .5/.15 ATR and major 1/.25 ATR unchanged.
  First native protection action #76 belongs to MRVL #1053, accepted once.
  Fresh broker portfolio at 20:42:42 CET explicitly confirmed
  `isTslEnabled=true`, stop 249.37 (initial requested 249.23217568832547),
  take profit 288.35 retained. The stop had already ratcheted upward. Recovery
  was still ACKNOWLEDGED at that direct read; the normal worker reconciles it.
  Other unarmed positions keep their original SL/TP. Do not revert to scheduled
  based on earlier historical inactive-feature notes. Continue observing actual
  broker behavior, SHORT activation and any instrument-distance rejection.
  Subsequent worker verification marked action #76 APPLIED after one attempt,
  with no reported error.


- 29 October 07:20 CET latest-entry investigation (read-only): latest candidates
  #2542 TSM/#2541 SKHY were not submitted. TSM checked 07:09 CET / 02:09 New York,
  outside its recorded 09:30 regular open; SKHY additionally inactive/untradable.
  NVDA #2535 blocked an existing position. MSFT #2540 was outside its monthly
  resistance range with negative estimated net reward; MA #2539 costs exceeded
  target reward. No submitted entry since MSFT #2237 on 27 October.
  Concrete symbol-identity issue: CVX #2534 researched Chevron but exact broker
  symbol CVX resolves crypto instrument 100512 (inactive); a fresh broker search
  returned NYSE stock CVX.US instrument 1014 active/currently tradable, alongside
  CVX.PA and CVX.RTH inactive variants. Adapter exact-symbol matching therefore
  selects the wrong asset identity for this equity candidate; fix with explicit
  broker mapping plus asset/exchange checks, not by permitting crypto or simply
  taking the first search result. SSAB-A.ST #2531 also lacks broker symbol mapping.
  No trading rules, broker mapping or orders were changed during this review.


- 29 October broker-symbol fix implemented solely in eToro's adapter:
  brokers/etoro_symbols.py provides explicit verified aliases and ambiguity
  rejection. CVX research identity remains unchanged; quotes, cost requests and
  order preflight map to CVX.US / 1014 only with equity + NYSE + ID validation.
  Broker symbol/resolution provenance is captured separately. Unknown suffixes
  are not guessed; unsupported/closed/directional restrictions remain enforced.
  No agent prompt or classification logic changed, and historical candidates
  were not rewritten/replayed. All 430 tests pass (nine dedicated symbol tests).
  Rollback image: finbot:before-symbol-mapping-20261029. Deployment completed: sole container healthy. Read-only resolution through
  the deployed adapter verified research CVX → CVX.US / 1014, Stocks, NYSE,
  active/currently tradable; exchange_open remained false and was not overridden.
  Native trailing mode broker, enabled, 15-minute reconfirmation and all ATR
  thresholds were verified unchanged.


- 29 October ~17:00 CET entry audit: 134 candidate lifecycles/58 symbols,
  zero submitted entries. Statuses: Research 32, Strategy 43, Risk 16,
  ANALYSIS_ONLY 35, existing-position deferrals 8. Exchange-only blocks 24:
  19 pre-session and five during recorded regular sessions (MSFT #2600/#2608,
  MRVL #2604, INTC #2606, SHEL #2614). NVDA #2602 additionally duplicate-held.
  Fresh MSFT default-field and explicit-field searches both return stock 1004,
  active/currently tradable but isExchangeOpen=false; requested fields do not
  explain that flag. Authoritative broker execution-availability investigation
  remains outstanding; no gate was overridden. The day's one crypto-CVX block
  was pre-fix, not a regression of CVX.US mapping. Read-only report:
  /tmp/finbot-entry-review-20261029.md. No orders/settings changed.


- 29 October AAPL #2227 review: native prime arming at 15:09 CET, entry ATR
  6.3950042724609375, actual entry 347.09, arming boundary 350.2875, quote
  350.45. Native action #78 requested SL 349.490749 / TP 351.09051; APPLIED
  once at 15:10:12 CET. Portfolio explicitly trailing-enabled, SL ratcheted
  349.75 through 15:20:25; broker closed 15:20:45 at 349.72, net +3.01.
  No Finbot close action. Consistent with native stop, but history has no explicit
  closure reason and trailingStopLoss=false; retain Unknown attribution rather
  than guessing. Detailed report /tmp/finbot-aapl-review-20261029.md.
- Dashboard status alignment built/tested in sibling finbotdashboard: canonical
  lifecycleStatusLabel + TradeStatus reused in tables/drawer; CSV retains raw
  status and adds status_label. ANALYSIS_ONLY now reads Not submitted · execution
  blocked, including existing snapshots (#2614). No producer/database/trading
  change. 27 tests, build, 12-case Python label parity and desktop/mobile/CSV
  browser checks passed. User explicitly approved the saved FTPS pin; static publication completed
  with byte-for-byte verification of live index/assets. Published JS
  index-Uj6XbmgB.js. Existing imported rows are corrected without a full sync or
  trading-runtime restart; no broker/account changes.

- 30 October selectable arming: amount (default) vs ATR, not AND gates.
  New PM keys profit_trailing_arming_basis=amount, prime_arm_usd=10,
  major_arm_usd=20. Strictly above thresholds using broker unrealized P/L;
  no second leverage multiplier. ATR alternative retains .5 / 1; retreat
  remains .15 / .25 entry ATR for both LONG bid highs and SHORT ask lows.
  Cash peak saved in price_trailing.evidence and revalidated in fresh guards.
  Basis switches reset observed peaks, retain frozen entry/ATR and existing
  native broker stops. Broker mode and current 15-minute interval preserved.
  Local GUI enum/help, Telegram allowlist/validation and reporting methodology
  updated. 438 Python tests pass; dashboard 28 tests/build and desktop/mobile
  summary/tooltips pass. User approved this publication's saved FTPS pin;
  static frontend published/byte-verified (index-DCfycfBs.js). Older snapshots
  without arming selector remain ATR. Runtime deployment/sync verification below.
- Runtime deployment: Docker API hung/returned 500 during Compose replacement.
  Supported graceful Desktop restart timed out; supported force-stop then start
  recovered the daemon. Recreated only the existing Compose service, preserving
  finbot_migrated_20261022. Final image 62c56e502393; rollback tag
  finbot:before-amount-arming-20261030. Persisted PM verified amount/10/20,
  broker mode and 15 minutes; frontend/worker health passed after recovery.
  Existing open NVDA #996, BMY #1285, META #2052 retained. Arming observations
  migrate on first fresh executable quote; frozen entry ATR remains intact.
- Post-deployment SQLite PRAGMA quick_check returned ok; configuration change
  recorded in access_events. Fresh 04:51 UTC observations for all three open
  positions use arming_basis=amount, cash peak 0 (currently negative), retaining
  their frozen entry ATRs. Worker heartbeat current, sole container healthy.
  Final full Python suite again passed 438 tests after making the ATR-mode
  retreat<arming validation conditional on ATR arming (amount retreats need
  not be constrained by inactive ATR arm values).
- Existing user-approved curated reporting sync completed: imported generation
  20261030T044402Z-bc8e7ffc8929, 214 partitions, 70,124,977 compressed bytes;
  configuration=1, trades=2631. Dashboard data now carries amount mode/10/20.
  Export was much slower than prior runs; did not change transport or launch
  another trading worker. Temporary browser preview server stopped.

- 30 October 06:53 CET exchange-flag correction deployed at user request.
  Search isExchangeOpen=false remains raw evidence, but no longer permanently
  vetoes an otherwise active/currently tradable supported demo instrument when
  an explicitly mapped exchange calendar is open (holiday/early-close/break
  aware), matching demo eligibility explicitly allows opening and a dated
  realtime broker bid/ask is <=60s old (future tolerance 5s). Preflight repeats
  verification and requires non-potential direction/leverage permission.
  Eligibility is account configuration, not a real-time market clock; calendars
  do not establish unscheduled halts. Broker acceptance remains authoritative.
  Unknown/unavailable evidence retains the block. No candidate replay/order
  was manually triggered. Fixed v2 rate date normalization/freshness checks.
  Added pinned exchange_calendars 4.13.2; full suite 450 tests passed, plus 26
  eToro tests inside deployed image. Sole container healthy, image 22139d62e5a5,
  migrated volume preserved; rollback finbot:before-exchange-availability-20261030.
  Read-only deployed MSFT quote correctly remains indicative outside the session.
  Actual market-open operation has not yet been observed. NVDA/BMY/META remain
  reconciled/protected; amount arming 10/20, broker mode and 15m unchanged.
  No unresolved broker actions. Details in LOCAL_MVP.md, brokers/etoro_sessions.py
  and tests/test_etoro_exchange_availability.py.

- 30 October automatic exchange discovery implemented at user request.
  Unknown supported-asset exchange names now trigger eToro directory verification
  by exact name and ID (or unique exact name when ID is unavailable). Installed
  calendar names/MICs/aliases and explicit venue synonyms can register mappings
  dynamically; there is no fuzzy matching, generated code or invented schedule.
  Discovery runs even for inactive instruments without enabling them. Results and
  unresolved reasons persist in /data/exchange_calendar_registry.json; positive
  records refresh daily, unresolved hourly, and calendar-version changes force
  revalidation. Directory reads are shared/throttled. Derived records survive
  recreation and rebuild automatically after SQLite-only restore if missing.
  All 462 tests passed, plus 12 registry tests inside the deployed image.
  Sole service replaced successfully, image 28218a3b1650, healthy; rollback
  finbot:before-dynamic-exchanges-20261030. Actual broker NESN.ZU/SIX ID12,
  DNB.OL/Oslo ID14 and SAN.MC/Madrid ID10 automatically registered SIX/XOSL/XMAD.
  All remained untradable while inactive/outside session. No order was submitted.

- 30 October 19:24 CET approved fresh-move/quote-retry plan deployed.
  `news_reaction_v3` centralizes side selection. Measured temporary reversals
  now allow mixed trends at half risk; established opposing trends retain higher
  confidence. Two completed closes beyond a monthly/weekly boundary plus 0.1 ATR,
  current-price clearance and directional follow-through can qualify continuation
  with existing ATR stop/target and positive net gates. Valid opposing levels
  ahead still cap targets. Rejected proposals no longer expose losing-side TP.
  Enabled effective Strategist keys allow_mixed_trend_reversals and
  fresh_boundary_entries_enabled; GUI metadata and Telegram allowlist included.
  Catalyst anchors persist by symbol/topic in reaction_anchors using first dated
  report (actual release time explicitly unverified). Commentary cannot reset
  active anchors; expired anchors need a distinct explicit event report. Two
  completed post-event bars and <=15m last-bar freshness remain required.
  Broker rates get one initial read plus two stale-only retries after 2s/5s,
  shared with availability checks; diagnostic codes distinguish missing/invalid,
  undated/future/delayed, stale, transport and rate-limit failures. No order retry.
  Final execution checks broker and retrieval age; cost-review expiry permits one
  bounded full recalculation under the same lease before SUBMITTING. v2/v3
  positions both retain original management/fill policies; native trailing unchanged.
  Full suite: 482 tests pass. Deployed image 92bc4ad4843a; 20 focused tests pass
  inside image; sole container healthy, volume preserved. Rollback image:
  finbot:before-fresh-moves-20261030. META #2052 retains v2, SL590.5428700474331,
  TP716.9606949288504, reconciled protection; PM amount10/20, broker mode, 15m.
  Offline saved AMZN #2786 now evaluates LONG but rejects absent measured reversal;
  historical record was not replayed/changed. Live read-only AMZN quotes remained
  stale (71.2/73.5/78.7s) across all three reads, clearly recorded stale_quote
  rather than exchange-closed; execution withheld. No manual order submitted.

- 2 November Telegram /today now includes each listed trade's creation date/time
  in Europe/Stockholm (YYYY-MM-DD HH:MM), separate from its agent-event time.
  UTC-naive SQLite times are converted explicitly; context retains both timestamps.
  Existing reply cap/permissions preserved. Full suite 482 tests pass, including
  midnight UTC-to-Stockholm conversion and rendered /today output. Deployed sole
  container healthy, image ce77baea09cb; rollback
  finbot:before-telegram-created-time-20261102. No test Telegram message sent.

- 3 November volume implementation (source only): `news_reaction_v4` adds optional
  same-elapsed-session RVOL and approximate session VWAP, symmetric LONG/SHORT
  confirmation, bounded 0.05 directional / 0.025 countertrend confidence relief
  (0.60 floor), shared Strategy/Risk thresholds and fresh submission revalidation.
  Half-risk and all capital/quote/cost/protection limits remain unchanged; measured
  reversal rules retain baseline requirements. Half of new research slots preserve
  recency; volume prioritizes the remainder, with at most 40 symbols/four concurrent
  Yahoo reads. GUI metadata and Telegram allowlist include controls. Unsupported
  exchanges or incomplete/stale volume retain baseline eligibility. OBV deferred.
  No runtime deployment, persisted setting mutation or manual order performed.
  Validation: full unittest suite passed 491 tests; git diff whitespace check passed.
  Nine new offline regressions cover calculation/DST/incomplete bars, LONG/SHORT
  thresholds and fallbacks, preserved half allocation, recency reservation and
  actual final-submission refresh with mocked provider/broker boundaries.

- 3 November volume deployment completed at user request. Decision messages in
  Strategy, Risk Manager and fresh-submission lifecycle events explicitly state
  LONG/SHORT, measured RVOL/VWAP distance, baseline/effective confidence thresholds,
  whether relaxation enabled approval, and unchanged risk allocation/limits.
  Full suite 491 tests passed; nine focused mocked tests passed inside deployed
  image 46360d4867ae. Sole Compose service healthy; frontend and worker heartbeat
  passed. External volume finbot_migrated_20261022 preserved; pre-deployment
  SQLite quick_check ok. Effective v4 controls enabled/1.5 RVOL/0.75 ATR/0.05
  relaxation verified. Risk/execution limits and PM broker mode, amount10/20 and
  15-minute reconfirmation match the pre-deployment snapshot. No manual order or
  candidate replay triggered. Rollback: finbot:before-volume-confirmation-20261103.

- 3 November recent-trend implementation (source only; runtime remains deployed
  v4): `recent_trend.py` classifies existing completed daily history using
  7/14-session displacement (each >=0.5 daily ATR), five-session-average change,
  nonoverlapping five-session high/low ranges and completed weekly close/average
  confirmation. Original SMA state remains intact. Directional recent evidence
  leads new v5 entry bias; fundamentals are contextual on that path. Opposing
  news requires fresh aligned RVOL/VWAP follow-through; countertrend requires
  measured temporary reversal and original countertrend confidence. Shared
  Strategy/Risk guards, final-submission recomputation and human-readable
  messages/policy snapshots included. No increase to existing SMA allocation;
  recent-countertrend capped at half. Shadow accepts RECENT_TREND_CONTINUATION.
  Existing v2/v3/v4 positions retain policies. recent_trend_enabled boolean has
  GUI/reporting metadata and Telegram validation/allowlist. Mixed/unavailable or
  disabled recent direction uses explicit legacy fallback.
  Daily bars are loaded once before research; compact recent/SMA/ATR evidence is
  supplied to the model (full multiyear level arrays excluded). Prompt version
  news_market_horizon_v3 explains market direction vs company quality and busts
  old neutral reuse. No new feed, schema, broker call or trading worker.
  META #2052 offline fixture selects bearish bias despite mixed SMA state;
  unconfirmed LONG and opposing-news SHORT are rejected, not replayed to broker.
  No live settings, orders, existing positions or runtime were changed.
  Validation: full unittest suite passed 502 tests, including 11 new offline
  trend/gate/fresh-submission regressions; git diff whitespace check passed.
  The existing research-cycle test now mocks volume explicitly to avoid provider
  network access. User's pre-existing .DS_Store and trade-review HTML preserved.

- 3 November automatic-confirmation activation deployed at explicit user request.
  Added Strategist checkbox **Auto-confirm trend entries** /
  trend_auto_confirm_enabled=true. ON confirms only passing measured RVOL/VWAP
  follow-through or measured countertrend reversal; OFF holds confirmation-needed
  entries for review, including manual submission, without bypassing checks.
  Aligned entries needing no additional confirmation retain normal rules.
  Strategy/Risk messages record ON/OFF and confirmed/awaiting_measurement/
  automatic_confirmation_disabled/not_required. Setting validated as boolean and
  included in GUI/reporting metadata plus Telegram allowlist.
  Deployed v5 recent analysis and this control together; persisted both
  recent_trend_enabled and trend_auto_confirm_enabled true through the existing
  settings function, with user-requested activation in access_events. Verified
  eToro demo and automated demo orders enabled. Risk/execution limits, volume
  thresholds and broker-mode amount10/20 trailing at 15 minutes matched before/
  after snapshots. Sole container healthy, frontend/worker health passed, external
  volume finbot_migrated_20261022 preserved. Full suite 506 tests passed; 15 focused
  mocked tests passed inside deployed image 4a0d76a44fef. No manual order or
  historical candidate replay triggered. Rollback image:
  finbot:before-trend-autoconfirm-20261103. Earlier source-only v5 notes are now
  superseded by this deployment.

- 4 November reporting/retention repair deployed. Reporting now rebuilds only
  affected UTC daily partitions and reuses unchanged v2 manifest parts; it runs
  in a separate subprocess so reporting memory/failure cannot restart the backup
  daemon or trading worker. Operational `signals.payload` and selected
  `agent_events.details` retain compact decision summaries plus a hash/reference
  to canonical immutable `trade_events`, rather than repeating full evidence.
  Active non-executed analysis retention is 3 days, recoverable archive retention
  is 7 days from archival, and duplicate operational history retention is 3 days
  (latest sector signal is protected). Before mutation, a verified compressed
  backup completed. Batch migration compacted 4,720 rows from 3,917,165,570 to
  3,950,948 JSON bytes and removed 483 expired signals; 999 recent archive rows
  were retained. Controlled WAL checkpoint/VACUUM/quick_check reduced the live DB
  from 6,349,828,096 bytes to about 1.91 GB; quick_check returned ok. Sole demo
  container is healthy with restart count zero; final image
  9bbfbc39c220a57106b3c1ad6fdcb7906b90124fa7edd7d76092e7d2b8881edf.
  Full suite passed 509 tests; final focused reporting/retention suite passed 20.
  Rollback image: finbot:before-incremental-reporting-20261104.
