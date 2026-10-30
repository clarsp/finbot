# Paired exit experiments

`paired_exit_comparison.py` and `scripts/compare_trade_exits.py` evaluate exit
rules on recorded broker fills. They do not import the trading worker, submit
orders, or change application settings. This is an exit-policy counterfactual,
not historical validation of news research, full portfolio execution, or profit
expectancy.

Create a consistent SQLite snapshot **inside Docker**, then copy that snapshot
out for offline analysis. Never open the live Docker database from macOS:

```sh
mkdir -p data/experiments
docker compose exec -T finbot /app/.venv/bin/python -c 'import sqlite3; from pathlib import Path; Path("/data/backups").mkdir(exist_ok=True); s=sqlite3.connect("/data/finbot.db"); d=sqlite3.connect("/data/backups/exit-input.db"); s.backup(d); assert d.execute("PRAGMA quick_check").fetchall()==[("ok",)]; d.close(); s.close()'
docker compose cp finbot:/data/backups/exit-input.db data/experiments/exit-input.db
.venv/bin/python scripts/compare_trade_exits.py --database data/experiments/exit-input.db --output data/experiments/exit-study-YYYYMMDD
```

The primary design compares 2-ATR stop/3-ATR target without structure, its
structure-capped counterpart, 2-stop/1-target, 3-stop/1-target and 3-stop/2-target.
The optional `--design atr-controls` removes structure caps from the latter
three alternatives to isolate ATR distances. Treat diagnostic controls added
after inspecting a primary result as exploratory, not independent validation.
`--as-of` can share an observation cutoff, and `--market-cache` can share the
same frozen market-data files. Each output directory saves an input SHA-256,
fixed policies and observation cutoff in `manifest.json` before replaying.

Each design tests the saved holding limit (48 hours explicitly assumed for
legacy records with no policy), seven days and fourteen days. It reports both
actual historical units and units reduced to maintain equal baseline 2-ATR
planned stop risk for the wider stop. ATR/entry percentage is reported; price
bands are descriptive diagnostics and do not gate trades.

The input includes all verified broker fills in the snapshot, with fixture
trades, partially altered positions and conditional mean-reversion positions
excluded where identified. Missing fill timestamps can come from stored broker
portfolio snapshots, never the lifecycle creation time. Missing ATR is
reconstructed from completed pre-entry bars and labelled. The whole entry day
is excluded from daily/weekly/monthly level formation. Corporate splits after
entry invalidate a replay. Retrospective vendor corrections remain a limitation.

Five-minute OHLC comes from Yahoo with pre/post-market data. It is not eToro
executable bid/ask history and does not cover all overnight trading. Partial
entry candles are excluded, adverse gaps fill at the observed open, favorable
improvement is not assumed, and simultaneous stop/target candles assume the
stop first. Barrier ambiguity in a candle straddling the time deadline is
flagged. A time exit needs a subsequent observed candle open; an unfinished
path is censored rather than assigned a realised profit.

Pairwise summaries use only completed baseline/alternative paths with no
initial gap greater than ten minutes, same-session gaps greater than thirty
minutes, or deadline-bar ambiguity. These coverage checks do not certify all
unobserved overnight periods. Different alternatives can have different paired
subsets; compare deltas on their matched subset. Raw dollar totals are not a
portfolio return, and large legacy position sizes can dominate them. Mean R
metrics normalise each result by its own baseline stop budget.

Cost models use captured rules or labelled defaults (10bps roundtrip and 8%
annual financing), known entry-time broker fees and simulated duration, plus a
10bps sensitivity. These are not final broker fees. Existing entry reward/risk
threshold violations are reported, not used to suppress the counterfactual.
The experimental design intentionally tests policies the production gates
would reject. Production rules remain unchanged.

Outputs include `report.md`, `results.json`, `trades.csv`, the manifest, and
cached market data. For a later observation cutoff, use a new output directory
and fresh cache. Retain the same input cohort to finish censored paths. An
additional later cohort is needed for genuinely prospective validation.
