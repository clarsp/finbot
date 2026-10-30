# Hosting Finbot locally

Finbot migrated from Fly.io to this Mac on **22 October 2026 at 21:21 Stockholm**.
The Fly machine and its 3 GB volume were deleted after local reconciliation,
verified Tigris backup and confirmed dashboard imports. The Fly app registration
is retained; it no longer has a machine or volume.

- **Trading and tuning interface:** https://localhost:8501 (local self-signed TLS).
- **Reporting dashboard:** https://finbot.kujahapa.com; local Finbot continues
  publishing full partitioned reports through Tigris and the authenticated notification API.
- **Runtime:** one Docker Compose service, `finbot`, image `finbot:research-20261022`.
- **Database:** `/data/finbot.db` inside external Docker volume
  `finbot_migrated_20261022`. The prior local volume is preserved separately.
- **Secrets:** ignored `.env.config`, copied privately from the effective Fly
  configuration. GUI runtime configuration remains inside `/data/runtime.env`.
  Neither belongs in Git or the Docker image.
- **Backups:** Tigris bucket `datadb`, existing hourly/startup/shutdown policy,
  latest plus previous validated backup. Obtain future recovery downloads from
  Tigris. Never mount the bucket as the live SQLite filesystem.
- **Automation:** demo trading enabled; 14 positions reconciled during handover.
  Existing users, profiles, protections, shadow tracking and reporting data were retained.
  The tested research relevance/neutral-reuse and net reward/risk fixes are active.
- **Power:** the existing `com.finbot.keepawake` service is running. Docker Desktop
  must remain running for the container to operate.

Normal operation from the Finbot directory:

```sh
docker compose ps
docker compose restart finbot
docker compose logs --tail 100 finbot
```

`docker compose up -d` is safe with the current `compose.yaml` configuration:
it targets the migrated external volume. To deploy local source changes use
`docker compose up -d --build --force-recreate finbot`. Keep one writable runtime;
do not start a second copy or reuse the old local database for broker automation.
Do not delete the migrated volume. Self-signed TLS can produce a browser trust
warning until the local certificate is trusted.

## Backup and recovery

SQLite is primary storage; Tigris holds recovery snapshots, not a mounted database.
Backups use SQLite's online backup API, SHA-256 metadata and integrity checks.
Existing local data wins at startup. An empty volume restores a verified snapshot
when configured; restoration pauses automated entries for reconciliation.
Credentials and `/data/runtime.env` are separate from SQLite backups.
Hourly and graceful-shutdown snapshots retain latest and previous validated copies;
abrupt shutdown may lose changes since the last successful off-site snapshot.

Never open the live Docker SQLite files from macOS. For replacement recovery,
stop the sole writer, preserve the existing volume and WAL files, validate an
isolated snapshot, and reconcile broker positions before enabling entries.
Use the dashboard backup verification controls for isolated checks.
See [CONFIGURATION.md](CONFIGURATION.md) for backup/environment settings.

Historical Fly deployment and cutover instructions are archived; Fly has no active
compute or volume. See [WORKSPACE.md](WORKSPACE.md) for archive inventory and restore.
