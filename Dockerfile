FROM python:3.12.8-slim-bookworm

# System deps: ffmpeg for audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

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
RUN chown -R app:app /app /tmp/musicbot
USER app

EXPOSE 8000

CMD ["python", "run.py"]
