FROM python:3.12.8-slim-bookworm

# System deps: ffmpeg for audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp lives in its own venv owned by the app user, so the bot can upgrade
# it at runtime — YouTube breaks old versions within weeks. The bot runs
# `pip install -U` here on startup and periodically (bot/tasks.py). Only the
# yt-dlp entrypoint is linked onto PATH; the venv's python/pip must not shadow
# the system ones that hold the app's dependencies.
ENV YTDLP_VENV=/opt/ytdlp
RUN python -m venv /opt/ytdlp \
    && /opt/ytdlp/bin/pip install --no-cache-dir -U "yt-dlp[default]" \
    && ln -s /opt/ytdlp/bin/yt-dlp /usr/local/bin/yt-dlp

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Create dirs
RUN mkdir -p /tmp/musicbot data

# Run as non-root
RUN addgroup --system app && adduser --system --ingroup app app
RUN chown -R app:app /app /tmp/musicbot /opt/ytdlp
USER app

EXPOSE 8000

CMD ["python", "run.py"]
