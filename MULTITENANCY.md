# Trading profiles and future multitenancy

Updated 18 October 2026. This is the handoff for the next implementation phase.

## Implemented now

- Separate platform logins and trading profiles. Membership roles are viewer, operator and owner; platform administrator is a separate account flag.
- Existing users share profile 1, **Finbot shared demo**. Initial administrators receive owner membership; other existing users receive viewer membership. New users default to viewer membership, with an explicit onboarding choice for operator, owner or no shared access.
- `clarsp@live.se` and `admin` are platform administrators. The migration preserves their existing credentials and assigns both ownership of the shared profile. No passwords are reset.
- Active-profile selector, personal password/avatar settings, profile membership sharing, and creation of draft profiles with planned broker/account metadata.
- Relationship: `users` ↔ `profile_memberships` → `trading_profiles` → `broker_accounts` → `broker_connections`.
- Default connection metadata refers to the existing runtime configuration. It does not copy API secrets into the profile tables. Draft connections contain no credentials.
- Existing trades are tagged `trading_profile_id=1`. Policies, orders, broker identifiers, quantities, stops, targets, recovery actions and historical reports are preserved.
- Normal viewers can read shared dashboards, trades and reports; mutation controls are unavailable. Operators can use profile research, trading and rule controls. Owners can also manage profile memberships. User administration and process-level broker/SMTP/AI credentials remain platform-admin-only.
- Business operations enforce backend permissions in a validated web-session context, including cached UI reads. Permissions are rechecked rather than trusted from a client-supplied username or role. The current in-process worker remains a trusted system caller outside web contexts; any future API/worker boundary must establish an explicit principal.
- Suspension prevents login, revokes existing sessions and blocks their further actions. Reactivation does not revive revoked sessions. Password changes revoke existing sessions. Suspension affects the login, not the shared profile's automation or existing positions.
- Last-active-admin and last-active-profile-owner safeguards. Self-suspension/deletion is blocked. Transfer ownership before suspending or deleting a sole owner.
- Access-management events are recorded without passwords or tokens.

## Deliberately parked

This is **shared-profile access control**, not a completed multi-account trading service.

Only profile 1 has an active runtime. Additional profiles are drafts: their metadata can be shared, but they cannot trade or display profile 1's data. A database constraint also prevents creating a second active runtime. Do not remove this gate until account/profile isolation is complete.

Most settings, agent rules, operational tables, snapshots, news analytics, reports, notifications and worker schedules still belong to the sole shared runtime. Broker IDs, recovery uniqueness rules and the account lease still assume that runtime. A profile ID on trades alone does not make these components tenant-safe.

The broker interface is incomplete; IBKR is a scaffold and Binance is not implemented. The existing environment/runtime credential path remains the only operational connection. No additional broker credentials should be entered into draft metadata or exposed to viewers/operators.

A second user can use the current shared profile now. Independent profiles, private live data and distinct broker execution must wait for the uplift below.

## Current administration

Use **Profile → User management** to register users, choose shared-profile access, suspend/reactivate accounts and assign platform-admin status. Normal accounts default to viewers.

Use **Profile → Trading profiles and access** to create drafts and, where you are an owner, grant another existing username viewer/operator/owner access. Owners may share a draft now; it remains inactive.

Sign in as `clarsp@live.se` before suspending `admin`. Suspension is available but the implementation does not automatically suspend `admin` or assume your personal login has been tested.

## Copy/paste prompt for the uplift

Copy the following block into a new Codex task when independent trading profiles are required:

```text
Read README.md, LOCAL_MVP.md, CONFIGURATION.md, HOSTING.md, MULTITENANCY.md,
APPLICATION_AUDIT.md, IMPLEMENTATION_REPORT.md, applicable AGENTS.md files and
the current git diff. Continue from the implemented repository, not previous chat.

Implement independent, securely isolated trading profiles and broker/account
routing using the existing six-role architecture. Preserve the current shared
Finbot demo profile, its users/memberships, broker credentials, open positions,
policies, pending recovery operations and immutable performance reports.

Existing foundation:
- Users can share profiles through viewer/operator/owner memberships.
- Platform admins manage logins, suspension and platform configuration.
- Profile 1 is the only active runtime; all other profiles are drafts.
- Profile/account/connection tables exist, but operational settings/storage,
  workers, account locks, broker recovery identifiers and caches are not yet
  scoped for independent runtimes. Do not confuse metadata with isolation.
- Web operations use validated-session permission checks; in-process workers
  currently use a trusted context. Establish explicit worker/service principals
  before exposing any new service/API entry point.

Target architecture:
1. Shared Researcher and Strategist may generate broker-independent proposals.
   A configured reference broker can supply preliminary/reference risk inputs.
   Each destination account MUST receive fresh account-specific sizing, costs,
   executable quote, margin, exposure and protection-capability checks before
   execution. Risk Manager owns the calculations; Execution owns submission.
2. Introduce a complete broker interface covering account/currency/cash/equity,
   instruments, executable quotes, sizing increments/minimums, costs, orders,
   partial fills, order/reference lookups, protection, cancellation, closing,
   realized history and connection/authentication health. Keep vendor-specific
   semantics in adapters and normalize evidence without inventing confirmations.
   Advertise capabilities and reject unsupported combinations explicitly.
3. Complete and validate IBKR paper trading first. For Binance, establish whether
   the requested product is spot, margin or derivatives before implementing its
   risk/execution model. Do not assume SELL means short, that leverage multiplies
   actual units again, or that stop/target modification is a position PATCH on
   every broker. Country, timezone, broker and product are separate properties;
   validate availability for the actual connection rather than guessing by country.
4. Store secrets per connection using encrypted storage or a secret manager.
   Keep secrets out of profile metadata, browser state, logs, exports and reports.
   Separate demo/paper and live accounts. Preserve the current live-order block;
   do not activate live trading as part of this task.
5. Scope ALL relevant records and queries to their trading profile and/or broker
   account: trades/events, orders/fills, recovery actions, agent rules, schedules,
   shadows, snapshots/history, archives/restores, news/research private evidence,
   accounting, notifications, configuration audits and performance reports.
   Explicitly identify what public research may be shared and what remains private.
   Plan a transactional PostgreSQL migration if sharing one hosted installation;
   preserve an isolated SQLite deployment path where appropriate. Enforce backend
   authorization regardless of UI visibility and validate direct record-ID access.
6. Pin every existing/new order and held position to its original connection and
   account. Changing a profile's destination affects future entries only. Continue
   managing older positions through their original account until resolved.
7. Account leases, broker-ID uniqueness, idempotency and reconciliation must be
   scoped to the actual broker connection/account. Multiple profiles on ONE broker
   account must share the account lock and aggregate account-level risk limits.
   Different accounts must not block or close one another's positions.
8. Schedule work by active profile/account. Maintain existing per-action backoff,
   Retry-After handling and bounded writes; add no shared/global API budget by
   default. Use product/session-aware pacing: a crypto profile must not silently
   inherit the stock weekend policy. Suspend a login independently from a profile
   or broker connection; add explicit profile automation controls.
9. Scope dashboard queries, cache keys, exports, email recipients, operational
   alerts and Performance Analyst reviews. Membership changes and suspension must
   take effect on existing sessions and cache hits. Granting view access must not
   grant execution, user administration or credential access.
10. Preserve viewer/operator/owner sharing and active-owner/admin safeguards.
    Make connection use/sharing permissions explicit so membership alone cannot
    route orders to an unrelated connection or overwrite another user's secrets.
11. Add activation readiness checks for draft profiles. Enable a second runtime
    only after isolation, adapter capabilities, account binding, safe credentials,
    risk configuration and scheduler readiness are verified. Preserve the profile-1
    gate until these checks pass; do not simply remove the uniqueness constraint.

Validation requirements:
- Test two users with private profiles plus a deliberately shared profile.
- Test viewer/operator/owner/admin boundaries, direct-ID attacks, profile switching,
  session revocation, suspension, changed memberships, caches, exports and emails.
- Test two brokers issuing identical external IDs without cross-account collision.
- Test two profiles sharing an account: aggregate risk and lock/idempotency safety.
- Exercise adapter contract tests plus paper/demo timeouts, partial fills, duplicate
  requests, reconnects, rejected protection and unknown close outcomes. Unknown
  outcomes must not become permission for blind resubmission.
- Verify the migration preserves current profile 1's protected levels, policy
  snapshots, positions and pending-action IDs/attempt counts/next-check times.
- Ensure research reuse never reuses another account's risk approval or sizing.
- Separate realized broker outcomes, estimates, shadow paths and historical
  counterfactuals. Do not fabricate past shadow plans or rewrite frozen reports.

Deployment:
Take a consistent backup INSIDE Docker before migrations; never open the live
Docker-volume SQLite files from macOS. Preserve /data/runtime.env and the named
volume. Do not delete volumes. Run meaningful checks, update the implementation
and configuration documentation, and deploy only validated demo/paper changes.
Report implemented capabilities, migrated data and remaining limitations clearly.
```
