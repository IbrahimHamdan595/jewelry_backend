"""What the production API exposes without a session (NEX-47).

Like tests/test_cors.py these run against the real `app.main.app`, because the
thing under test IS the line in app/main.py that ships. The suite runs with no
ENVIRONMENT variable, so the module-level app here is the production
configuration — the default a deploy gets when nobody sets anything.

The TestClient is used without its context manager so the lifespan (gold
rate poller) never starts.
"""
import importlib

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, settings
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
