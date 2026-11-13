# PolyBot in a container. One process (`polybot run`) runs every job on the same
# timetable as the Windows scheduled tasks, so an always-on server keeps collecting
# data while your own computer sleeps. Research and paper trading only: nothing in
# here can place a real order, and no API keys are needed.
#
#   docker compose up -d --build        (the full guide is docs/always-on.md)

FROM python:3.12-slim-bookworm

# tzdata makes the TZ setting work. Everything else the bot needs comes from pip.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

# The bot runs as an ordinary user, not as root.
RUN useradd --create-home --uid 1000 polybot
WORKDIR /app

# Install the libraries first, in a layer of their own. They change only when
# pyproject.toml does, so rebuilding after a code change skips this slow step.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt

# Then the code (.dockerignore keeps data, databases, logs and secrets out of the image).
# "-e" installs it in place, so the bot still finds config.yaml, models/ and scripts/ in /app.
COPY . .
RUN pip install --no-cache-dir --no-deps -e . \
 && mkdir -p /app/data \
 && chown -R polybot:polybot /app

# Everything the bot keeps goes in /app/data: both databases, the recorded order books
# and trades, logs and reports. compose.yaml puts that folder in a volume, so it
# survives restarts and rebuilds.
ENV POLYBOT_DATA_DIR=/app/data \
    POLYBOT_DB=/app/data/polybot.db \
    POLYBOT_LOG_DIR=/app/data/logs \
    POLYBOT_REPORTS_DIR=/app/data/reports \
    POLYBOT_HEARTBEAT_DIR=/app/data \
    POLYBOT_LOG_CONSOLE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC

USER polybot
VOLUME ["/app/data"]

# `docker compose ps` shows "unhealthy" when no market data has arrived for 30 minutes.
HEALTHCHECK --interval=5m --timeout=2m --start-period=20m --retries=3 \
    CMD polybot health > /dev/null || exit 1

CMD ["polybot", "run"]
