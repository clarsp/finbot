# Hosting Finbot with SQLite and S3 backups

## Supported architecture

Run exactly **one writable Finbot container**. Keep `/data/finbot.db` on the
container's local SSD or a persistent block volume. S3 is used for recovery
snapshots; it is not mounted as the live database filesystem.

### Web entry point

Streamlit already provides the application HTTP server. For a private hosted deployment, keep it and place a dedicated reverse proxy or managed ingress in front of the container:

```text
Internet → managed TLS / reverse proxy → Finbot Streamlit :8501
                                      → persistent block volume /data
                                      → optional periodic S3 snapshots
```

Terminate trusted HTTPS at the ingress, keep port 8501 private, inject credentials through the hosting secret store, and restrict access with an identity-aware proxy, VPN, or equivalent network policy in addition to the application login. Do not add Nginx or a Node server to the Finbot container solely to serve Streamlit; a separate proxy or the provider's load balancer has a clearer lifecycle.

If the interface later needs dense admin navigation, responsive mobile layouts, real-time tables, or chart interactions that Streamlit cannot support cleanly, expose the existing Python services through a versioned FastAPI boundary and add a React/Next.js frontend incrementally. Separate the scheduler and broker worker from HTTP handling before running multiple web replicas.

At startup, `start.sh` checks the configured restore policy before the
scheduler or dashboard opens SQLite. While Finbot runs, `s3_backup.py` uses
SQLite's online backup API to create a consistent temporary database, runs
`PRAGMA quick_check`, and uploads both:

- `s3://BUCKET/PREFIX/latest/finbot.db` for startup recovery.
- `s3://BUCKET/PREFIX/snapshots/YYYY/MM/DD/finbot-TIMESTAMP.db` for history.

On normal container shutdown, the scheduler stops first and the backup daemon
attempts one final snapshot. A forced host or container failure can lose writes
made after the last periodic snapshot, so a persistent volume is still the
preferred primary storage.

## Configuration

```dotenv
FINBOT_DB_PATH=/data/finbot.db
FINBOT_S3_BACKUP_ENABLED=true
FINBOT_S3_BUCKET=my-private-finbot-backups
FINBOT_S3_PREFIX=production/finbot
FINBOT_S3_BACKUP_INTERVAL_SECONDS=900
FINBOT_S3_RESTORE_MODE=if_newer
FINBOT_S3_RESTORE_REQUIRED=false
FINBOT_S3_SERVER_SIDE_ENCRYPTION=AES256
AWS_REGION=eu-north-1
```

Restore modes:

- `if_newer` (recommended): restore when the local database is absent or the
  S3 `latest` object is newer.
- `if_missing`: restore only on ephemeral/new storage.
- `always`: replace the local database from S3 at every container start.
- `never`: upload backups but do not restore automatically.

Set `FINBOT_S3_RESTORE_REQUIRED=true` when starting without an existing S3
snapshot must fail rather than initialize an empty database.

For AWS, attach an IAM task/instance role to the workload instead of putting
long-lived access keys in `.env.config`. The role needs these permissions for
the configured bucket prefix:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:PutObject"],
    "Resource": "arn:aws:s3:::my-private-finbot-backups/production/finbot/*"
  }]
}
```

If bucket policy validation requires it, also grant `s3:ListBucket` scoped to
the prefix. Enable S3 bucket versioning and a lifecycle rule to expire old
`snapshots/` objects after your chosen retention period. When using KMS, set
`FINBOT_S3_SERVER_SIDE_ENCRYPTION=aws:kms`, set `FINBOT_S3_KMS_KEY_ID`, and add
the corresponding KMS permissions to the workload role.

## Deployment rules

1. Keep the service replica count at one. Two local SQLite copies can diverge
   and both workers can submit orders.
2. Prefer a persistent block volume at `/data`, with S3 as disaster recovery.
3. Use `if_missing` with a persistent volume, or `if_newer` when the host can
   restart on a new machine.
4. Use a private bucket, block public access, encryption, and versioning.
5. Move to PostgreSQL before adding replicas, separate scheduler workers, or
   zero-downtime rolling deployments with overlapping containers.

## Operations

Create a backup on demand inside a running container:

```sh
docker compose exec finbot /app/.venv/bin/python /app/s3_backup.py backup
```

Review backup activity:

```sh
docker compose logs finbot | grep finbot.s3_backup
```

The application never sends the WAL or journal files to S3. The uploaded file
is a standalone, validated SQLite database produced through SQLite itself.
