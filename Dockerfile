FROM python:3.11-slim

WORKDIR /app

# WeasyPrint (PDF export) needs Pango/HarfBuzz/FriBidi for text layout + Arabic
# shaping & bidi, fontconfig + an Arabic-capable font (Noto Naskh Arabic ships in
# fonts-noto-core), and gdk-pixbuf for raster images in templates.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        libfribidi0 \
        libgdk-pixbuf-2.0-0 \
        libffi8 \
        fontconfig \
        fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

# Liveness only: /health does no database work, so a slow query cannot turn
# into a restart loop. The slim image has no curl, hence python. urlopen
# raises on a non-2xx answer or a timeout, which is the non-zero exit Docker
# needs. Follows $PORT when the platform sets it, like the CMD below.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/health', timeout=4)"

# Shell form so ${PORT} is expanded; `exec` keeps uvicorn as PID 1 so it still
# receives SIGTERM on shutdown.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
