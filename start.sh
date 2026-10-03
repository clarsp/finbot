#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null 2>&1 || true
python -m pip install -r requirements.txt >/dev/null 2>&1 || true

export FINBOT_USERNAME="${FINBOT_USERNAME:-admin}"
export FINBOT_PASSWORD="${FINBOT_PASSWORD:-admin123}"
export STREAMLIT_PORT="${STREAMLIT_PORT:-8501}"
export ENABLE_HTTPS="${ENABLE_HTTPS:-false}"

LOG_DIR="logs"
mkdir -p "$LOG_DIR"

python makemoney.py > "$LOG_DIR/backend.log" 2>&1 &
BACKEND_PID=$!

if [ "${ENABLE_HTTPS:-false}" = "true" ]; then
  echo "Starting Streamlit with HTTPS enabled..."
  python run_streamlit.py
else
  echo "Starting Streamlit in local HTTP mode..."
  python run_streamlit.py
fi

wait "$BACKEND_PID"
