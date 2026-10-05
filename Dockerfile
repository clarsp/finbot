FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FINBOT_SKIP_INSTALL=true \
    FINBOT_DB_PATH=/data/finbot.db \
    STREAMLIT_PORT=8501 \
    ENABLE_HTTPS=false

WORKDIR /app

COPY requirements.txt ./
RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/start.sh \
    && mkdir -p /data /app/logs

EXPOSE 8501

CMD ["/app/start.sh"]
