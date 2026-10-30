# Workspace inventory — 24 October 2026

Application Python modules at the root are imported by the worker, UI or tests.
They remain in place. `brokers/` implements the broker boundary. `tests/` retains
current regression coverage; `scripts/` retains reusable research-funnel audit,
offline exit comparisons, guarded history cleanup and structure-target preview.
`research_feed.json` is the simulation fixture. `logs/`, certificates and the
virtual environment remain available for current operation/development.

`docs/reference/` now holds current documentation. Hosting and position-management
guidance was preserved before archiving `docs/operations/` and `docs/history/`.
The old scaffold-only `.agent.md` was archived and replaced with `AGENTS.md`.

## Private archives

- `data/archive/inactive-databases-20261022.tar.gz`: inactive databases/recovery evidence from the earlier cleanup; accompanying manifest lists hashes.
- `data/archive/workspace-cleanup-20261024.tar.gz`: 153 files, 155,989,098 source bytes, 53,543,151 compressed bytes; accompanying manifest records each original path, size and SHA-256, plus the archive checksum.

The new archive preserves all former non-archive host `data/` content: Fly helper
scripts/CLI, migration checkpoints, private configuration copies, experiment
results/caches and dated audits. It also preserves root audit logs, historical
docs, the retired Fly manifest, Fly export/cutover tools, completed PG/AMZN and
economic-label correction tools and their four specific tests. Original README
and selected references are included as pre-edit snapshots. Every member was
read back and hash-verified before removal; original source hashes were rechecked.
Archives/manifests are mode 600 and ignored by Git and Docker. Archive contents
include historical credentials: keep private. Current credentials remain in
`.env.config` and the Docker volume; no credentials were printed.

Archive evidence is recoverable, not operationally active. For an explicitly
requested restore, verify the archive checksum against its manifest and extract
into a new mode-700 directory outside this checkout. Never restore over current
source or a live database. The old exit-study input is in the database archive;
new comparisons require a fresh offline snapshot rather than that historical input.

`AGENTS.md` excludes private/generated paths from routine AI analysis;
`.rgignore` excludes them from ordinary ripgrep searches. These controls do not
prevent deliberate filesystem reads. No unsupported AI-ignore file is assumed.
Reproducible bytecode, pytest caches and desktop metadata were removed. The
container, active database, protections, broker settings and schedules were untouched.
