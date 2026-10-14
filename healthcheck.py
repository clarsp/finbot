"""Container health: frontend responds and the trading worker has a heartbeat."""
import os
import sqlite3
from datetime import datetime, timezone
from urllib.request import urlopen


def main():
    path = os.environ.get("FINBOT_DB_PATH", "/data/finbot.db")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    row = conn.execute("SELECT value FROM app_settings WHERE key='worker_heartbeat'").fetchone()
    conn.close()
    if not row or (datetime.now(timezone.utc)-datetime.fromisoformat(row[0])).total_seconds() > 120:
        raise RuntimeError("Trading worker heartbeat is stale.")
    port = int(os.environ.get("STREAMLIT_PORT", "8501"))
    with urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("Frontend health failed.")


if __name__ == "__main__":
    main()
