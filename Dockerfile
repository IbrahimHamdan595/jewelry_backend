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

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
