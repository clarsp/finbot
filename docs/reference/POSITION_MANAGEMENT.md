# Position management

## Profit-only discretionary exits

Both LONG and SHORT positions retain automatic configured SL/TP exits and explicit user-requested closes. Elapsed time alone never authorizes closure. Fresh opposing structural news or a confirmed fill-size breach may now authorize a discretionary close only when all of these checks pass:

- A fresh complete broker position read confirms identity, side, actual units, entry, margin, fees and positive reported P/L.
- A newly requested bid/ask produces positive conservative estimated net profit. LONG uses bid minus entry; SHORT uses entry minus ask. Actual units already include exposure from leverage; leverage is not multiplied again.
- Deduct non-negative broker fees plus conservative configured round-trip and financing estimates over actual holding age. The default minimum assumptions are 10 bps round trip and 8% annual financing; higher saved assumptions remain in force. These are estimates, not a complete broker settlement quote.
- Use the lower of price-derived and broker-reported profit after those deductions; subtract a slippage reserve of the greater of two spreads across all units or 10 bps of entry notional.
- Remaining estimated profit must exceed the greater of $1 or 0.5% of actual margin.
- Current provider metadata confirms today's regular exchange session with over ten minutes to close. Unknown calendar/session evidence blocks the discretionary exit.
- Immediately before submission, revalidate the current reason: still-opposing fresh research or an actual remaining size breach. Repeat the whole guard on any retry after a broker-confirmed failed close. Ambiguous outcomes are tracked without resubmission.

These defaults live in the existing Position Manager rules and UI, with a disable switch. Qualifying discretionary attempts are spaced at least 15 minutes per position. Profit evidence is retained in the tracked action payload. Profit by itself is not a reason to close. Losing positions remain advisory unless their configured stop is observed or the user explicitly requests closure.

A market-order profit check cannot guarantee positive realized settlement: price gaps, suspension, queuing or costs beyond estimates can change the result after submission. The implementation reduces this risk; it does not represent a guaranteed minimum execution price.

## Protection recovery

A never-submitted protection intent may adopt a verified tighter broker stop even when TP differs. The outstanding target is then reconciled independently, using that tighter stop, existing price-clearance checks and bounded write/backoff tracking. An outstanding submitted protection request is never replaced on this path. The stop is never widened by this automatic repair.

## Held-company research

Position Manager queues one persisted request per symbol when required research is stale. It records one pending reassessment for that dependency and rechecks hourly without reloading full bars or emitting the same assessment repeatedly. The ordinary scheduled Researcher cycle reserves up to half its candidate budget (at least one slot) for queued holdings and fetches their company news explicitly. It uses the existing AI, fundamentals and market-context analysis. Results are attached to the held lifecycle; this refresh cannot submit a new entry. Failed/unavailable work remains queued with at least hourly backoff. Closed holdings are removed from the queue. AI-disabled configurations remain respected.

## Pending entry validity

Default validity is 60 minutes from the persisted actual submission timestamp (older rows use their submitted event, then creation time only as a fallback). Existing recovery lookup must prove the exact broker order ID and a documented pending status before requesting cancellation through the demo-only endpoint. The cancellation intent and request ID are committed before transport; an acknowledgment or timeout never marks the trade cancelled. No automatic repeated cancellation write is sent. Existing backoff lookup confirms the terminal state or actual fill. A fill, including a partially filled cancellation, wins the race and is retained/protected rather than closed as a loss.

Official contract: https://api-portal.etoro.com/api-reference/trading--demo/cancels-an-order-before-it-is-executed

Shadow comparisons retain their original frozen plans and do not route extra orders.


## Hourly dollar-profit trailing exits — 26 October 2026

Historical policy, replaced by entry ATR tracking on 28 October.

The active shared demo enables `profit_trailing_enabled`. The existing Position
Manager scans each linked open position hourly, starting with its first observation
after activation. Broker-reported unrealized dollar P/L is used directly; share
price and leverage are not multiplied into this value. The highest observed
profit, last scan and tier evidence persist in `profit_trailing`, keyed by lifecycle
and verified broker position identity. No pre-activation or between-scan peak is inferred.

A peak strictly above $10 arms prime status with a $3 decline threshold. A peak
strictly above $20 upgrades permanently to major with a $5 decline threshold.
A decline equal to or greater than the threshold requests closure, including gaps
past the threshold between scans. Thus $11 to $9 stays open, $11 to $8 closes,
$19 to $15 closes, and $40 to $32 closes. Exactly $20 remains prime.

The existing account lease, broker ownership checks and durable close recovery
handle submission and ambiguous outcomes. Each attempted close rechecks a fresh
complete broker position snapshot and the dollar drawdown. A rebound that clears
the trigger withholds that attempt. Missing/invalid P/L never authorizes closure.
Existing SL/TP remain active. This explicit trailing policy is independent of the
$5/2% discretionary news-exit floor and may close below that floor, or at a loss
after a gap. Hourly observations and broker availability cannot guarantee the
threshold as a realized settlement amount.

### Configurable profit tracking — 26 October 2026

Position Manager now exposes the scan interval (default and active value **30
minutes**), prime/major minimum peak profits ($10/$20), and prime/major drawdowns
($3/$5). The enable switch remains available. Values must be finite and positive;
the interval is 1–1440 minutes and the major threshold must exceed prime.
Changes apply to subsequent observations and close revalidation using the saved
peak; neither peaks nor last-check timestamps are reset. Shortening the interval
makes positions due based on the shorter interval since their last scan. The
existing maintenance cadence may delay a due scan while another account operation
is in progress. Historical hourly references above describe the original rollout.

## Closure reason reporting

Closure and weekly outcome emails, report rows and CSV exports carry
`closure_reason`, `closure_reason_basis` and `closure_reason_detail`. Explicit
textual broker reasons identify SL/TP/manual closures when available. Otherwise,
a matching submitted Finbot close action can supply its recorded request reason:
profit trailing (including peak/check profit/drawdown/trigger), SL/TP level
observed, news invalidation, size breach or user request. The message distinguishes
that request evidence from broker confirmation of the execution trigger.
Unsubmitted/superseded actions, future-dated requests and undocumented numeric
broker codes are not treated as causes. P/L sign and exit-price proximity do not
establish a cause. Missing evidence is labeled Unknown. Already delivered emails
are not resent by this change; newly generated reports include stored evidence.


## Entry ATR price trailing — 28 October 2026

The original 28 October ATR arming policy (see the 30 October selector below)
observes every configured interval and freezes the ATR14 captured in the
entry policy. Actual broker entry is the reference. Prime arms strictly above
0.5 entry ATR and closes on a retreat of at least 0.15 ATR; major arms strictly
above 1 ATR and closes on a retreat of at least 0.25 ATR. All five quantities and
the enable switch are configurable under Position Manager, with tooltips.

For LONG, favorable movement is `(bid-entry)/ATR`, best is the highest bid and
the close trigger is `best-allowed_retreat*ATR`. For SHORT, favorable movement is
`(entry-ask)/ATR`, best is the lowest ask and the trigger is
`best+allowed_retreat*ATR`. The SHORT retreat is an upward price movement. Both
cases arm on positive favorable movement regardless of position size or cash
P/L. Missing entry ATR, invalid/delayed/stale quotes or changed identity/entry
withhold tracking exits. The existing broker SL/TP remain in place.

`price_trailing` persists actual entry, entry ATR, best price, first/latest check
and evidence. Existing open positions start price peaks at the first fresh quote
after activation; old dollar peaks are never translated into inferred prices.
Historical dollar tracking/closure records remain unchanged. Every close/retry
fetches a fresh portfolio and quote and revalidates the retreat. A rebound
withholds the request; uncertain close outcomes are not resent. Periodic checks
may overshoot the price trigger, so it is not a guaranteed settlement price.

Position management shows recorded entry ATR, best price, tier, retreat, trigger
and observation time. Closure messages describe ATR trigger evidence. Reporting
configuration carries the same field labels/help/units to finbotdashboard and
labels the snapshot time; older snapshots remain explicitly historical.

## Optional native broker trailing — 28 October 2026

`profit_trailing_mode` selects `scheduled` (the default/current policy) or
`broker`. The feature is implemented but native mode was deliberately not
activated. The active shared demo retains its GUI-selected 15-minute interval
and the existing 0.5/1 ATR arming and 0.15/0.25 ATR retreat values.

In broker mode, the existing maintenance loop performs faster arming checks,
using executable bid for LONG and ask for SHORT, the actual entry and frozen
entry ATR. It records best prices without postponing the separately scheduled
reconfirmation. Prime/major arm above their thresholds. The initial stop uses
the observed best price and configured retreat, clamped to retain any tighter
existing broker stop. After a small retreat, the effective native distance can
therefore be smaller than the configured maximum; it is recorded in the intent.
Take profit is retained. No additional worker is started.

The broker adapter owns native capability and API translation. eToro demo maps
the request to `stopLossType=trailing`; other adapters remain on the execution
boundary and do not become research dependencies. An acknowledgment is not
proof: the portfolio must explicitly report trailing type and a sufficiently
protective stop/unchanged target. Missing flags remain unknown. Pending or
ambiguous native updates are read and escalated without blindly resetting the
native distance. A broker stop that ratchets is adopted locally rather than
repaired back to an older fixed value. The native mode's scheduled checks record
its observed stop and confirmation; a type flag alone with an inadequate stop
does not suppress the scheduled exit fallback. Fresh close revalidation avoids
competing with verified native protection, including the ordinary observed-stop
exit path. Closed positions still reconcile against broker history.

An active tighter prime trail is retained when major's configured distance is
wider. Native revisions must tighten protection; an immediate jump to major
without an existing native prime stop can use the major distance. Unknown/manual
native distances are retained and checked rather than overwritten. Changing
back to scheduled mode does not silently remove an already-active broker stop.

Before native mode is enabled, demo validation is still required for each
instrument's permitted minimum distances, enabling-on-existing-position offset
semantics and portfolio confirmation (including SHORT). No broker-native update
was sent as part of building this inactive option. Native stops remove the
Finbot polling delay only after activation; arming/upgrades still depend on
maintenance, and gaps can produce worse fills than the stop level.

Official API contract:
https://api-portal.etoro.com/api-reference/trading--demo/modify-stop-loss-and-take-profit-settings-on-an-open-position

### Execution eligibility boundary

Broker-specific eligibility belongs to Execution and the selected adapter.
Research/Strategist analysis must continue when a broker's eligibility service
is unavailable. Analysis conclusions remain separate from execution readiness.
A dedicated account eligibility query can be reused for stable instrument/side/
leverage/stop-boundary rules; fresh quote, account, pending-position and current
availability checks still precede a write. This change does not add an early
broker filter or silently override the unresolved exchange-availability flags.
The current entry boundary retains its existing broker checks; introducing the
dedicated eligibility endpoint is a separate execution-only extension.

News candidates are grouped by ticker, and the cycle's candidate list contains
one entry per ticker; related headlines do not launch parallel same-ticker
entries in that loop. Repeated analyses across cycles and separate related
instruments remain possible. Final execution rechecks a broker position
snapshot and unresolved entry states before submission. This is separate from
the contradictory exchange-open evidence found in the day's audit.


### Native activation observation — 28 October, 20:42 CET

The user subsequently enabled broker mode. Shared demo remains enabled with
15-minute scheduled reconfirmation and the same ATR values. MRVL #1053 was the
first eligible prime position: one accepted request, followed by a fresh broker
portfolio explicitly reporting trailing enabled. Its stop had advanced from the
requested 249.23217568832547 to 249.37; take profit remained 288.35. This confirms
the first LONG activation/ratchet, not every instrument or SHORT behavior. The
normal worker continues reconciliation; unarmed positions retain normal SL/TP.

## Selectable arming — 30 October 2026

`profit_trailing_arming_basis` selects `amount` (new default) or `atr`.
Amount arms prime strictly above `profit_trailing_prime_arm_usd=10` and major
strictly above `profit_trailing_major_arm_usd=20`, using broker-reported unrealized
USD P/L without multiplying leverage again. ATR mode retains the alternative
0.5 / 1 entry ATR arming thresholds. These are alternatives, not combined gates.
Both modes retain 0.15 / 0.25 entry ATR retreat distances and direction-aware
LONG bid highs / SHORT ask lows. Entry ATR is still required in amount mode.
Observed cash peaks persist with tracking evidence so tiers stay armed after
P/L retreats. Switching basis starts fresh price/cash observations while keeping
the frozen entry and entry ATR. Already-active broker stops are retained and
never loosened by the switch. Broker mode and the active 15-minute scheduled
reconfirmation are retained. The interval default for new installations is 30.
Original SL/TP remain active before arming. The local editor and Telegram expose
the selector and both sets of thresholds; the reporting dashboard labels the
selected basis, with old snapshots lacking a selector interpreted as ATR.
