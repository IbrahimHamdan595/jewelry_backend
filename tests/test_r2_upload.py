import pytest

from app.core import r2


@pytest.fixture
def r2_settings(monkeypatch):
    monkeypatch.setattr(r2.settings, "r2_account_id", "acct123", raising=False)
    monkeypatch.setattr(r2.settings, "r2_access_key_id", "ak", raising=False)
    monkeypatch.setattr(r2.settings, "r2_secret_access_key", "sk", raising=False)
    monkeypatch.setattr(r2.settings, "r2_bucket_name", "gold-pos", raising=False)
    monkeypatch.setattr(r2.settings, "r2_public_url", "https://cdn.example.com/", raising=False)


@pytest.mark.asyncio
async def test_upload_returns_public_url_and_calls_put(monkeypatch, r2_settings):
    calls = {}

    def fake_put(content, key, content_type):
        calls["content"] = content
        calls["key"] = key
        calls["content_type"] = content_type

    monkeypatch.setattr(r2, "_put", fake_put)
    url = await r2.upload_image(b"\xff\xd8\xff data", "Ring Photo.JPG", "image/jpeg")

    # public url = R2_PUBLIC_URL (trailing slash trimmed) + "/" + key
    assert url == f"https://cdn.example.com/{calls['key']}"
    assert calls["key"].startswith("products/")
    assert calls["key"].endswith(".jpg")  # extension taken from filename
    assert calls["content"] == b"\xff\xd8\xff data"
    assert calls["content_type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_extension_falls_back_to_content_type(monkeypatch, r2_settings):
    seen = {}
    monkeypatch.setattr(r2, "_put", lambda content, key, ct: seen.update(key=key))
    await r2.upload_image(b"x", "noextension", "image/png")
    assert seen["key"].endswith(".png")


@pytest.mark.asyncio
async def test_not_configured_raises_503(monkeypatch):
    monkeypatch.setattr(r2.settings, "r2_bucket_name", "", raising=False)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await r2.upload_image(b"x", "a.jpg", "image/jpeg")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_put_failure_raises_502(monkeypatch, r2_settings):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(r2, "_put", boom)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await r2.upload_image(b"x", "a.jpg", "image/jpeg")
    assert ei.value.status_code == 502
