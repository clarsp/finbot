# Finbot

Finbot pursues positive **net returns** through news research, explicit strategy rules, and portfolio risk controls. It is a demo research and execution application. Positive net expectancy has **not** been established.

The active eToro workflow discovers companies from Yahoo news/screens, enriches them with company fundamentals, asks OpenAI for evidence-based research, and runs the Strategist, Risk Manager, and Execution gates. It supports positive-news LONG trades, negative-news SHORT trades, and a separate measured conditional mean reversion track. An actual rejection stops that candidate; unavailable optional data is recorded separately from execution eligibility.

- **Local simulation:** `FINBOT_BROKER_PROVIDER=dummy`; local simulated records, no broker orders.
- **eToro demo:** `FINBOT_BROKER_PROVIDER=etoro`, `ETORO_DEMO_TRADING=true`; automatic demo submission when enabled and all execution gates pass.
- **Live orders:** blocked in the eToro adapter.

## Run

Set credentials and an initial administrator password in the ignored `.env.config`, then:

```sh
docker compose up -d --build --force-recreate
docker compose logs -f finbot
```

Open http://localhost:8501. Compose persists the database in `./data/finbot.db` and operational logs in `./logs`. Rebuilding does not erase the mounted database. The local Python default database is a separate `./finbot.db`; set `FINBOT_DB_PATH` explicitly when sharing data between launch methods. Stop the workload before copying or backing up SQLite files.

## Hosted database backups

For hosting, keep SQLite on local SSD or a persistent block volume and run one
writable container. Optional S3 support restores a validated snapshot before
startup, uploads consistent versioned snapshots periodically, and makes a final
backup during a normal shutdown. Do not mount an S3 bucket as the live SQLite
filesystem. See [HOSTING.md](HOSTING.md) for configuration, IAM permissions,
restore modes, and the point at which PostgreSQL becomes necessary.

## Main controls

Under the sidebar agent configuration:

- **Execution:** `max_order_units=5`, `max_order_spend_usd=200`, and `analysis_retention_days=3` by default. Both sizing caps apply. Spend is account margin/capital with estimated costs reserved; leverage 2 can produce nearly $400 gross exposure from $200 capital. Actual market fills can differ; an excessive confirmed fill triggers a close request.
- **Risk Manager:** position count, aggregate stop risk, gross exposure, single-position exposure, gap-stress scenario, ATR stop/target, estimated costs, and maximum holding time.
- **Strategist:** confidence/fundamental gates, integer leverage, and conditional mean reversion thresholds.

In **Research Actions**, enable automated eToro demo orders and configure the research interval. Maintenance runs independently every minute; historical evaluation does not gate demo automation. Administrators control trading and settings; other users have read-only dashboard access.

The main dashboard's Trade Lifecycles distinguish executed/submitted orders from analysis. Each new sized lifecycle explains its unit calculation. **Analysis cleanup and restore** provides manual archival and restoration alongside hourly scheduled archival.

Successful eToro demo submissions can be emailed through configurable SMTP.
Open **Trade email notifications** in the sidebar to configure the recipient,
server, authentication, STARTTLS or implicit TLS, and the sanitized lifecycle
evidence attachment. Notification failure is recorded on the lifecycle but
never changes a broker-acknowledged order into a failed trade.

Every expanded trade lifecycle also provides **Download JSON report** and
**Download HTML report** controls. Both formats include the stored agent stages
and supporting data after credential redaction. SMTP settings provide a
connection/authentication test that sends no message and an explicitly labeled
sample-report delivery button.

## Tests and limitations

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary databases and mocked external providers; they submit no actual orders. They verify application behavior, not broker account permissions or profitable predictions. See [LOCAL_MVP.md](LOCAL_MVP.md) for exact strategy rules, [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md) for changes and remaining gaps, and [APPLICATION_AUDIT.md](APPLICATION_AUDIT.md) for the original audit.
