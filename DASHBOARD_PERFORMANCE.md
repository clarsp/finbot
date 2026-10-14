# Dashboard performance repairs — 14 October 2026

## Changes

- Lifecycle reads batch trade/event retrieval into two queries, replacing one duplicate and one evidence query per trade. An indexed correlated lookup retains duplicate IDs, including duplicates outside the selected date range.
- A compact lifecycle index preserves decisions, capacity reasons, search messages, dates and broker references without transferring research documents or API logs. Legacy inferred lifecycle statuses still work.
- Lifecycle pages use SQL LIMIT/OFFSET before page hydration. Heavy evidence is fetched by lifecycle ID only when its expander opens. Closing it skips that work. The compact index remains loaded for counts, filtering and chart selection.
- Streamlit tabs render only the selected executed/analysis group and the selected agent evidence stage. Closed research/settings panels and closed evidence subpanels skip their content. Historical validation charts load full documents only when explicitly enabled.
- Dashboard/page/evidence reads have five-second bounded caches. Manual refresh, research completion, order actions, cleanup/restore and relevant settings changes invalidate caches. Broker order authorization always uses fresh backend reads; cached UI data never authorizes execution.
- Default capital charts use the worker's recorded broker position snapshot with its timestamp. Normal dashboard rendering makes no broker API request. **Refresh eToro positions** remains an explicit live read; its result is used for up to 60 seconds.
- Schema initialization runs once per UI session, rather than on every widget interaction. Initialization enables SQLite WAL where available and adds indexes for lifecycle events, ticker duplicates, date ranges, agent events and news dates. Initialization logs a warning and preserves the existing journal mode if a concurrent lock prevents WAL activation.
- Performance calculations load numeric snapshot history and decode only the latest full snapshot, instead of loading every historical broker payload.
- The default dashboard time cutoff uses five-second intervals instead of excluding the entire current minute. Explicit historical cutoffs remain respected by lazy evidence reads.

## Measurement

Earlier read-only observations on the mounted database (386 lifecycles): full history used 773 SQL statements and took about 1.7 seconds; dashboard loading used 789 statements and about 1.1 seconds. The dashboard result represented about 8.5 MB when serialized as JSON.

A later rehearsal used a disposable copy obtained through a read-only connection to the mounted database. After additive index/WAL initialization, medians over three local runs were:

| Operation | Median | SQL statements | Result serialized as JSON |
|---|---:|---:|---:|
| Full lifecycle history | 74 ms | 2 | 8.338 MB |
| Compact dashboard | 28 ms | 19 | 1.181 MB |
| Compact 25-row page | 1.8 ms | 2 | 0.057 MB |
| One lifecycle with full evidence | 0.7 ms | 2 | 0.026 MB |

The compact dashboard was subsequently reduced further by omitting unused full signal payloads and the redundant decision timeline. These numbers are local backend measurements, not a browser or deployed-container benchmark. Serialized Python result size is not the exact Streamlit network payload. The original database was not modified by the rehearsal, and no actual broker requests/orders were submitted.

## Regression coverage

Run `.venv/bin/python -m unittest discover -s tests -v`.

The six added regressions check constant query count, full evidence preservation, compact payload size and gate reasons, SQL page boundaries/duplicate IDs, historical evidence cutoff, WAL/index initialization, and actual Streamlit lazy opening with no automatic broker reads. They run alongside the existing 63 regressions.

## Deploy

`docker compose up -d --build --force-recreate`

Indexes and WAL are applied during initialization. Keep the mounted database on local disk and use SQLite-aware backups or stop the workload before copying database files; WAL uses companion files while running. No database migration to another engine is needed for these repairs.
