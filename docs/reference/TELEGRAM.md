# Private Telegram assistant

The existing Finbot worker hosts an optional Telegram long-polling task. No VPN,
webhook, inbound port, hosted relay, second trading worker or extra Python
package is required. The Mac and Docker must remain running. This is a Finbot
assistant using the existing OpenAI API configuration, not a connection to the
Codex chat session or its filesystem tools.

## Setup

1. In Telegram, open the official [@BotFather](https://t.me/BotFather), send
   `/newbot`, and choose a name and an available username ending in `bot`.
2. Keep the returned API token private. Sign in as an administrator at
   **https://localhost:8501**, then open sidebar **Telegram bot**.
3. Enter the token, enable the bot, leave both sender IDs blank, and save.
   Underscores and hyphens are accepted; pasted quotes, line breaks and invisible
   whitespace are cleaned automatically. The password field never displays the saved token. The token is written to
   the existing private runtime configuration file, not the database or reports.
4. Open a **private chat** with your new bot and send `/whoami`.
5. Copy the returned numeric user ID and private-chat ID into the local setup,
   and save again. This links the sender to the currently signed-in Finbot user.
6. Send `/status` to verify the link, then `/help` or a natural-language question.

The worker reloads this configuration without restart, usually within about 30
seconds when idle. Check the saved bot token and polling status in the same panel
if there is no reply. Use a dedicated bot: an existing webhook or another poller
using that token prevents `getUpdates` from working. The panel detects webhooks;
it does not silently remove another integration. No actual Telegram connection
can be tested until the user supplies the BotFather token.

## Commands and plain-language requests

- `/whoami`: return only the sender's Telegram IDs; available before linking.
- `/help`: list commands and scope.
- `/status`: recorded worker heartbeat, new-entry automation and today's counts.
- `/positions`: recorded linked positions and profit-trailing observations.
- `/closed` (or `/closed 5`): latest broker-confirmed linked closures across all
  dates, with realized net P/L and Stockholm timestamps.
- `/today`: today's lifecycle counts and recent recorded decisions, Stockholm time.
- `/trade 123`: recorded stages for a specific lifecycle.
- `/settings position_manager`: show one agent's settings. `/settings` lists agents.
- `/set position_manager profit_trailing_interval_minutes 30`: update an allowed rule.
- `/pause`, `/resume`: pause/resume new eToro demo entries through the existing
  validated function; existing position management remains active.

Examples: "Why were today's candidates rejected?", "Which positions are major
candidates?", "Change profit tracking to 15 minutes", "Pause new entries".
Plain language uses the configured OpenAI model to select a bounded operation
and, for questions, summarize selected stored evidence. It has no arbitrary SQL,
shell, source-code tools, live broker fetch or direct broker-order tools. Only
one settings change is supported per request. The previous six exchanges from
the same private chat within an hour are supplied as conversation context.
No independent web research is performed. Reports include timestamps and bounds;
recorded P/L and account snapshots can lag the broker.

## Authorization and delivery

Both the configured numeric sender ID and private-chat ID must match. Group,
channel, bot-origin and unlinked requests cannot access Finbot data. The mapped
Finbot account must still be active and have membership on active profile 1.
Ordinary viewer/operator permissions are enforced on every operation; user
suspension, membership revocation and configuration changes stop further writes.
News/provider text is data, not instructions for mutation. Natural-language
interpretation sees the user's request and setting names before any research
context; the subsequent answer step has no write capability.

Telegram request IDs and polling offsets are persisted. Each request is claimed
before an action; interrupted or duplicate requests are never automatically
reapplied. Stored replies are retried independently. A crash after a setting
change but before acknowledgment can leave an uncertain response: inspect
`/settings` or `/status` before sending a new request. A reply may be delivered
more than once if Telegram accepted it before a transport failure. Commands over
five minutes old, forwarded messages and invalid values cannot mutate settings.
Changes record before/after values and the mapped actor in access events.
Telegram questions and replies are stored in `telegram_requests`; the bot token
is never included. Obvious credential-like text is rejected/redacted rather than
sent to the model or saved as a question.

The enable switch disconnects the bot without changing trading automation.
Revoking its token in BotFather additionally invalidates the Telegram credential.

## Environment keys

The admin form saves these in the existing private runtime file. They can also
be supplied as environment configuration. Runtime-file Telegram values override
environment values so GUI changes reach the worker without restart.

| Key | Default |
|---|---|
| `FINBOT_TELEGRAM_ENABLED` | `false` |
| `FINBOT_TELEGRAM_BOT_TOKEN` | empty, secret |
| `FINBOT_TELEGRAM_USER_ID` | `0`, unlinked |
| `FINBOT_TELEGRAM_CHAT_ID` | `0`, unlinked |
| `FINBOT_TELEGRAM_USERNAME` | empty, mapped Finbot account |

Protocol reference: [Telegram Bot API](https://core.telegram.org/bots/api).

Closed-trade answers receive separate latest-history and requested-closure-date
lists, broker history sync metadata and counts. These do not use entry creation
date or the open-position list to infer closures. Earlier bot replies are not
considered evidence when current database records contradict them. Rejection
answers distinguish recorded neutral research gates from narrative financial
commentary, especially for indices, futures and crypto.

Price tracking uses entry ATR: `/set position_manager profit_trailing_prime_arm_atr 0.5`, `profit_trailing_major_arm_atr 1.0`, `profit_trailing_prime_retreat_atr 0.15` and `profit_trailing_major_retreat_atr 0.25`. `/positions` shows ATR movement, best price and trigger price from saved observations. LONG follows bid highs; SHORT follows ask lows. Old dollar trailing setting names are no longer editable.

The optional ATR mechanism is selected with `profit_trailing_mode` (`"scheduled"` or `"broker"`). Scheduled mode remains current. Native broker trailing is implemented but unselected pending demo validation; selecting it is an explicit operational change. Scheduled verification remains active in both modes.

Profit protection supports `position_manager.profit_trailing_arming_basis`
(`amount` or `atr`) and configurable `profit_trailing_prime_arm_usd` /
`profit_trailing_major_arm_usd` (defaults 10 / 20). `/positions` identifies the
recorded arming basis and cash peak alongside ATR movement and price triggers.
