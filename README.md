# Maison Zahab Backend

FastAPI backend for the jewellery project.

## Requirements

- Python 3.11+
- PostgreSQL (the app uses `asyncpg` / `psycopg2`)

## Setup

From the `jewelry_backend` directory:

```bash
# 1. Create and activate a virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies (editable install)
pip install -e .

# Or, to include dev dependencies (pytest, etc.)
pip install -e ".[dev]"
```

## Environment

Create a `.env` file in `jewelry_backend/` with the variables expected by `app/config.py` (e.g. database URL, JWT secret, etc.).

## Database migrations

Run Alembic migrations before starting the server:

```bash
alembic upgrade head
```

To seed initial data:

```bash
python -m app.seed
```

## Run the server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The API will be available at:

- http://localhost:8000
- Interactive docs: http://localhost:8000/docs

## Run with Docker

```bash
docker build -t maison-zahab-backend .
docker run --rm -p 8000:8000 --env-file .env maison-zahab-backend
```
