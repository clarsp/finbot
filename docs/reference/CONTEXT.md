# Current handoff — 24 October 2026

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
