"""CORS preflight behaviour of the real production middleware stack.

Regression tests for NEX-45: a leftover `allow_origin_regex` for
`*.devtunnels.ms` let anyone with a free Microsoft dev tunnel make
credentialed cross-site requests. These tests run preflights through the
actual `app.main.app` middleware so the config that ships is the config
tested. Unlike most tests here we deliberately import the full app —
a slimmed-down copy of the middleware would not catch a bad line in
app/main.py, which is the whole point.

The TestClient is used without its context manager so the lifespan (gold
rate poller) never starts; CORS middleware needs no startup.
"""
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

client = TestClient(app)


def preflight(origin: str):
    return client.options(
        "/api/products",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )


def test_devtunnels_origin_is_refused():
    """Any *.devtunnels.ms host is attacker-registrable — must not be allowed."""
    resp = preflight("https://evil-attacker-12345.devtunnels.ms")
    assert "access-control-allow-origin" not in resp.headers
    assert resp.status_code == 400


def test_unknown_origin_is_refused():
    resp = preflight("https://evil.example.com")
    assert "access-control-allow-origin" not in resp.headers
    assert resp.status_code == 400


def test_allowlisted_origin_gets_credentialed_cors():
    """The configured frontend origin must keep working, cookies included."""
    origin = settings.cors_origins[0]
    resp = preflight(origin)
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == origin
    assert resp.headers["access-control-allow-credentials"] == "true"
