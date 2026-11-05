# Reporting contracts

Finbot is the authority for trading; sibling `../finbotdashboard` is a separately
versioned reporting consumer. Neither project imports the other's runtime code.
A reporting-only JSON schema and sample payload live in the dashboard's docs
and fixtures; this document and reporting_export.py define the producer boundary.

`reporting_export.py --db PATH --output reporting.json.gz` takes a consistent
read-only SQLite transaction, includes archived analysis, and creates a gzip
snapshot with schema_version=1, generation_id, generated_at (UTC), source_id,
profile (id/name/provider/environment), counts, and eight list collections:
trades, events, agent_events, performance_reviews, account_history, shadow_exits,
recovery_actions, configuration (eight collections total).

IDs are stable within kind and profile. Events reference trade_id. Trade stages
have phase=initial/recheck/submission/post_execution so graphs can distinguish
unique candidates from repeated observations. Trading-profile IDs are strings.
All timestamps are UTC ISO8601; the dashboard formats Europe/Stockholm locally.
Unknown outcomes remain null. Realized profit comes from the broker closed
ledger once per position, never a default zero or leverage re-multiplication.
Policy and entry_config preserve saved decision context; configuration provides
current rules separately. Users, password hashes, integration secrets, request
headers, and raw API bodies are excluded. Verbose lists/text are visibly capped.

The exporter currently supports the one active runtime profile (1), not multiple
independent broker runtimes. A consumer must never infer memberships from data.

`--publish` uploads only to `finbot/reporting/shared-demo/generations/<id>.json.gz`
and publishes a small latest.json manifest containing SHA256 of compressed bytes,
object_key, generation_id, generated_at, counts and compressed_bytes. Uploaders
must run as one scheduled producer; do not run Mac and Fly writers concurrently.
Latest + previous manifests protect two immutable generations; only recognized
reporting-generation names inside this reporting prefix are pruned. Recovery
backup prefixes are never touched. Reporting publishing is enabled explicitly through FINBOT_REPORTING_ENABLED. The existing backup daemon launches one bounded reporting thread after successful hourly/startup backups. A running report is not duplicated, and reporting never blocks the final shutdown backup; no second trading worker is created.

The deployed consumer downloads an individual report through a short-lived signed URL and validates checksum,
size, UTC timestamp, schema version, generation and expected source/profile, then
atomically replaces reporting rows. Failed or older imports retain prior state.
No uploaded report can create users, grant membership, or change trading settings.

Production transport is a signed notification to /api/reporting-sync.php. Finbot publishes the curated object to Tigris and sends an HMAC-SHA256 authenticated notice containing a 10-minute object download URL. The signature binds the request body and a timestamp (five-minute acceptance window). The website validates source/profile, exact Tigris host/object prefix, URL lifetime, checksum and generation, then imports atomically. It holds no recovery-bucket access key and does not accept arbitrary database uploads. Redirects and other download hosts are rejected. Duplicate generations are idempotent; older generations cannot replace newer data. A reporting failure never fails a completed recovery backup. Existing CLI imports remain available for installations with a dedicated read-only reporting key.

Legacy v1 report collections retain all lifecycle rows. In the former single-file transport, verbose nested evidence lists were capped at 24 items (known price series keep the latest 24), text at 8,000 characters and depth at 14, with explicit truncation markers. Raw agent-event detail bodies and raw broker snapshot payloads are not read from SQLite. Full source evidence remains in Finbot. Optional --input-report reuses a validated gzip report when publishing; the consumer still verifies schema, source/profile, generation and checksum.

## History boundary after the 2026-10-21 cleanup

`app_settings.legacy_history_cleanup_v1` records the first broker-confirmed lifecycle's start time and the dated recovery archive. The exporter omits older paper lifecycles (including archived analysis) once this boundary exists. Broker/order references, uncertain submissions, position-management/recovery dependencies, all shadow-linked lifecycles and unknown dates override the age filter conservatively. Post-boundary rejected candidates remain in the agent funnel. Shadow results remain counterfactual and never contribute realized broker P/L.

The one-time tool `scripts/cleanup_legacy_history.py` previews by default. Apply creates and verifies a small dated Tigris archive of the exact removed cohort (including compressed SQLite blobs), independently of the normal full recovery backups, acquires the existing workflow leases, checks dependencies, removes only the previewed historical cohort and rebuilds legacy company aggregates. Protected-table fingerprints and SQLite foreign keys are checked before commit. Optional compaction briefly pauses the worker/UI only after broker leases and the SQLite writer lock are held; processes resume in `finally`. It never calls a broker or changes position protections. Broker snapshots/closure ledgers, shadow payloads, configuration, users, memberships, notification state, usage/billing records and news reference evidence are retained. The dated archive lives outside the automatic latest/previous retention prefix.

Production v2 uses daily partitions. Immutable `trade_events` in Finbot remain the canonical full evidence; reporting exports bounded decision/level evidence with provenance instead of duplicating arbitrarily large context into the dashboard. Export failures are recorded in `reporting_status.json` beside the database without credential contents.

## Partitioned production transport (v2)

Production exports now retain decision evidence arrays and text without reporting truncation, while excluding credentials and raw operational payloads. Collections are partitioned by UTC creation day and kind, then split near 4 MiB of uncompressed JSON. A single evidence row can use a dedicated part up to 32 MiB; larger rows fail explicitly without removing evidence. Parts are gzip-compressed, SHA256 addressed and reused in Tigris when unchanged. No global 64 MiB limit applies to the full partitioned report.

Each authenticated notification stages one verified part into a hidden generation. Replay is idempotent. A final signed manifest commits only after partition checksums, kind/day, per-kind counts and unique realized broker outcomes validate. The prior complete generation remains visible until the commit transaction; interrupted imports cannot erase history. The producer reuses that committed manifest and rebuilds only affected UTC days, including creation-day partitions for active or recently changed positions. Untouched historical days are not reconstructed in memory or re-uploaded. Old positions and later events remain available through the complete merged manifest.

Tigris retains parts referenced by the latest and preceding complete exports. Recovery database backups are unaffected. Per-part import remains bounded (40 MiB decoded), with no evidence compaction. All current parts are staged per generation. Unchanged checksummed parts are reused in object storage and copied from the current verified dashboard generation using exact row IDs, so only changed evidence parts need downloading and decoding. Reporting runs in a process separate from the recovery-backup daemon; a reporting failure cannot terminate backups or restart trading. The v1 CLI snapshot format remains supported for legacy/offline imports; live publishing uses v2.
