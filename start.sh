#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

set -a
for env_file in .env .env.config; do
  if [ -f "$env_file" ]; then
    source "$env_file"
  fi
done
set +a

if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate
if [ "${FINBOT_SKIP_INSTALL:-false}" != "true" ]; then
  python -m pip install --upgrade pip >/dev/null 2>&1 || true
  python -m pip install -r requirements.txt
fi

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
