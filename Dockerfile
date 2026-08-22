FROM python:3.12-slim

# Runtime env: unbuffered logs, no .pyc, src/ on the path so `-m claude_proxy` works.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    CLAUDE_PROXY_DATA_DIR=/app/data

WORKDIR /app

# Non-root runtime user. The bind-mounted ./data dir is chowned to this uid on
# the host so the app can create and write its two SQLite files there —
# claude_proxy.db and audit.db, each with its own -wal and -shm siblings.
RUN useradd -m -u 10001 app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY manage.py ./

USER app

EXPOSE 8080 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

CMD ["python", "-m", "claude_proxy"]
