# Hosting this Finbot deployment on Fly.io

## What you need to add to Fly

**Your eToro API keys, OpenAI API key and SMTP password are NOT in SQLite.**
They must be provided to Fly. Entering connection settings in the GUI does not
put them in the database: the GUI writes a separate environment file.

For this deployment, use these three sources:

| Source | What it supplies | What you need to do |
|---|---|---|
| `fly.toml` | Standard hosting, demo broker, news/OpenAI defaults, Tigris bucket, TLS and volume settings | Deploy the repository with this file. No manual re-entry of those settings. |
| Fly Secrets | API credentials plus your current SMTP configuration | Import the private file prepared below once. |
| Tigris SQLite backup | Users, memberships, agent rules, schedules, toggles, trades and evidence | Finbot restores automatically onto an empty volume. |

### One import instead of entering variables individually

A private import file has been prepared locally at **data/fly-secrets.env**.
It is ignored by Git and has mode 600. It contains real credentials; do not
commit it, paste it into chat, or include it in the Docker image.

It contains these seven currently configured credentials:

| Fly variable | Meaning |
|---|---|
| `AWS_ACCESS_KEY_ID` | Tigris Access Key ID |
| `AWS_SECRET_ACCESS_KEY` | Tigris Secret Access Key |
| `ETORO_API_KEY` | eToro public API key |
| `ETORO_USER_KEY` | eToro user key |
| `OPENAI_API_KEY` | OpenAI API key |
| `FINBOT_SMTP_PASSWORD` | SMTP password |
| `FINBOT_PASSWORD` | Required launcher/bootstrap password; does not reset existing DB users |

It also contains your effective email settings: `FINBOT_SMTP_HOST`,
`FINBOT_SMTP_PORT`, `FINBOT_SMTP_SECURITY`, `FINBOT_SMTP_USERNAME`,
`FINBOT_SMTP_FROM`, `FINBOT_TRADE_EMAIL_ENABLED`, `FINBOT_TRADE_EMAIL_TO`,
`FINBOT_TRADE_EMAIL_ATTACH_JSON`, `FINBOT_SMTP_TIMEOUT_SECONDS`, and
`FINBOT_TRADE_EMAIL_SUBJECT_PREFIX`.
These preferences are included so you do not have to copy runtime.env or
reconfigure the GUI on Fly. Fly Secrets can store ordinary settings too.

With Fly CLI installed and authenticated, run from the repository:

```sh
fly secrets import --stage -a finbot-tbfs1a < data/fly-secrets.env
```

This stages the values without restarting an existing Fly deployment. The next
deploy applies them. The export/import has **not** been uploaded automatically.

If credentials or GUI email settings change before migration, regenerate the file:

```sh
.venv/bin/python scripts/export_fly_secrets.py
```

The exporter reads the running local Docker container, captures credentials in
memory and writes the private file. It prints no values, sends no email and
does not contact Fly. It intentionally excludes unrelated environment variables.
Optional OpenAI admin/org settings and eToro account IDs are included only if
configured; none are currently needed for this setup.

Fly keeps imported secrets in its vault and injects them when Machines boot.
You do not need to re-enter them on every restart or deploy.
Do not import the entire local .env.config: that could override Fly-specific
paths and hosting policies. If this Fly app already has a runtime.env, its SMTP
values can override imported email settings; review that file before migration.

## Why /data/runtime.env is not on your Mac

`/data` is a path **inside the Docker container**, backed by the named Docker
volume `finbot_finbot_data`. It is not `/data` on your Mac, nor the repository's
`data/` directory. Docker Desktop stores that volume inside its Linux VM.

At the configuration check on 18 October, this file existed inside Docker and
contained eight GUI-saved SMTP preferences. The API keys and SMTP password
were supplied separately from your host `.env.config`. Both sources are merged
into the private Fly import file above. **You do not need to locate or transfer
runtime.env for this migration route.**

On Fly, the same path is on the Fly volume. Future GUI edits can create/update
it there. SQLite backups do not include this file or API credentials.
See [CONFIGURATION.md](CONFIGURATION.md#which-settings-live-where) for the exact
precedence rules. This import consolidates the current effective values without
changing the application's existing configuration mechanism.

## Deployment when ready

The prepared manifest targets **finbot-tbfs1a**, region **ams**, with one shared
CPU, **2 GiB RAM**, and a **3 GB** persistent volume.

1. Make the current source and fly.toml available to the build. A GitHub build
   cannot see uncommitted local changes.
2. Import the file above using `--stage`.
3. Stop the local trading container and let its final successful backup finish
   before starting Fly. Never run both schedulers against the same account and
   backup prefix.
4. Check `fly status -a finbot-tbfs1a` and `fly volumes list -a finbot-tbfs1a`.
   Reuse the correct existing volume, or create it once:
   `fly volumes create finbot_data --region ams --size 3 -a finbot-tbfs1a`.
   If the app already has multiple Machines, reduce it to the intended single
   writer before deployment.
5. Deploy with `fly deploy --ha=false -a finbot-tbfs1a`. The manifest does not
   enforce a maximum Machine count; verify exactly one is running.
6. Check recovered users, positions and broker reconciliation before enabling
   new automated demo entries. Check email configuration; no test mail is sent
   by this procedure.

There has been no Fly deployment from this workspace. The Fly CLI was not
installed at preparation time; the TOML was parsed and checked locally, but
server-side validation is still pending. After successful import, keep the
private export securely or delete it; it can be regenerated from local Docker.

## What persists, and when Tigris is used

The mounted `/data` holds the live SQLite database and GUI runtime file.
Normal restart/redeploy with the same volume reuses that database and skips
Tigris restoration. An empty/replacement volume downloads the latest validated
snapshot from `s3://datadb/finbot/shared-demo/latest/finbot.db`.

Recovery pauses new automated entries. This Fly manifest requires a remote
snapshot when the local DB is absent; it will not silently initialize an empty
account. An existing damaged database needs explicit stopped-service recovery;
startup does not overwrite existing local DB/WAL data.

Backups use SQLite's online backup API, SHA256 metadata and SQLite integrity
checks. Uploads run every 15 minutes and a final snapshot is attempted after
writers stop during graceful shutdown. Abrupt failure can lose changes after
the last successful off-site snapshot. Keep the persistent volume as primary
storage; Tigris is recovery storage, not a mounted database filesystem.

Fly terminates public HTTPS; the app uses internal HTTP port 8501.
Local Compose continues to use HTTPS at https://localhost:8501.
Fly idle stopping is disabled so the scheduler runs without browser traffic.
The manifest allows 120 seconds for graceful shutdown and enables daily
volume snapshots with five-day retention. Tigris versioned objects accumulate;
choose bucket lifecycle retention separately.

For local administration, run SQLite operations inside Docker. Never open
Docker's live database/WAL from host tools, remove its named volume with
`docker compose down -v`, or prune that volume.

Official references:
[app configuration](https://docs.fly.io/reference/configuration),
[Fly Secrets](https://docs.fly.io/apps/secrets),
[secret import](https://fly.io/docs/flyctl/secrets-import/),
[persistent volumes](https://docs.fly.io/volumes/overview),
[single-Machine deployment](https://fly.io/docs/apps/app-availability/).
