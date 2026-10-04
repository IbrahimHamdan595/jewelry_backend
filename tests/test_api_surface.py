"""What the production API exposes without a session (NEX-47): no API docs,
a shallow /health, and the container that serves them.

Like tests/test_cors.py these run against the real `app.main.app`, because the
thing under test IS the line in app/main.py that ships. The suite runs with no
ENVIRONMENT variable, so the module-level app here is the production
configuration — the default a deploy gets when nobody sets anything.

The TestClient is used without its context manager so the lifespan (gold
rate poller) never starts.
"""
import importlib
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.config import Settings, settings
from app.db.session import engine
from app.main import app

client = TestClient(app)

DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


# ── API docs ──────────────────────────────────────────────────────────────────

def test_environment_defaults_to_production():
    """Fail closed: a deploy that sets nothing is a production deploy."""
    assert Settings.model_fields["environment"].default == "production"
    assert settings.is_production


@pytest.mark.parametrize("path", DOC_PATHS)
def test_docs_are_not_served_in_production(path):
    """/openapi.json publishes every route, parameter and schema."""
    assert client.get(path).status_code == 404


@pytest.fixture
def dev_client(monkeypatch):
    """The same app/main.py, built with ENVIRONMENT=development.

    The app is assembled at import time, so the module is reloaded under the
    patched setting and reloaded again afterwards to put production back.
    """
    import app.main as main

    monkeypatch.setattr(settings, "environment", "development")
    yield TestClient(importlib.reload(main).app)
    monkeypatch.undo()
    importlib.reload(main)


@pytest.mark.parametrize("path", DOC_PATHS)
def test_docs_are_served_in_development(dev_client, path):
    assert dev_client.get(path).status_code == 200


@pytest.mark.parametrize("value", ["production", "PRODUCTION", " production ", "prod", "produciton", "staging", ""])
def test_unrecognised_environment_is_treated_as_production(monkeypatch, value):
    """A typo must not publish the docs: only names known to be local opt out."""
    monkeypatch.setattr(settings, "environment", value)
    assert settings.is_production


@pytest.mark.parametrize("value", ["development", "Development", "dev", "local", "test"])
def test_local_environments_are_not_production(monkeypatch, value):
    monkeypatch.setattr(settings, "environment", value)
    assert not settings.is_production


# ── Health ────────────────────────────────────────────────────────────────────

@contextmanager
def _database_tripwire():
    """Records — and refuses — every attempt to open a database connection."""
    attempts = []

    def refuse(dialect, conn_rec, cargs, cparams):
        attempts.append(1)
        raise RuntimeError("the database was touched")

    event.listen(engine.sync_engine, "do_connect", refuse)
    try:
        yield attempts
    finally:
        event.remove(engine.sync_engine, "do_connect", refuse)


def test_health_is_public():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_does_not_touch_the_database():
    """The container HEALTHCHECK polls this. If it waited on the database, one
    slow query would mark the container unhealthy and restart it into the
    same slow query — so it must answer with the database unreachable."""
    probe = TestClient(app, raise_server_exceptions=False)
    with _database_tripwire() as attempts:
        # Control: the tripwire does catch a route that needs the database.
        login = probe.post("/api/auth/login", json={"email": "a@example.com", "password": "x"})
        assert login.status_code == 500 and attempts

        attempts.clear()
        assert probe.get("/health").status_code == 200
    assert attempts == []


# ── Container ─────────────────────────────────────────────────────────────────

def _dockerfile() -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs, continuation lines joined."""
    text = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    lines = [l for l in text.replace("\\\n", " ").splitlines() if l.strip() and not l.lstrip().startswith("#")]
    return [(l.split(None, 1)[0].upper(), l.split(None, 1)[1].strip()) for l in lines]


def _only(instruction: str) -> str:
    found = [args for name, args in _dockerfile() if name == instruction]
    assert len(found) == 1, f"expected exactly one {instruction}, found {found}"
    return found[0]


def test_container_does_not_run_as_root():
    users = [args for name, args in _dockerfile() if name == "USER"]
    assert users, "no USER instruction: the container would run as root"
    assert users[-1] not in ("root", "0")


def test_container_healthcheck_polls_the_shallow_endpoint():
    check = _only("HEALTHCHECK")
    assert "/health" in check
    assert "python" in check and "curl" not in check  # the slim image has no curl


def test_container_honours_the_platform_port():
    """Render and friends inject $PORT; 8000 is only the fallback."""
    assert "${PORT:-8000}" in _only("CMD")
    assert "PORT" in _only("HEALTHCHECK")
