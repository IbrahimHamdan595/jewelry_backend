import httpx
from fastapi import HTTPException

from app.config import settings


async def upload_image(file_bytes: bytes, filename: str, content_type: str) -> str:
    if not settings.cloudflare_account_id or not settings.cloudflare_api_token:
        raise HTTPException(status_code=503, detail="Cloudflare Images is not configured")

    url = f"https://api.cloudflare.com/client/v4/accounts/{settings.cloudflare_account_id}/images/v1"

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {settings.cloudflare_api_token}"},
            files={"file": (filename, file_bytes, content_type)},
        )
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Cloudflare upload failed: {r.text}")

        data = r.json()
        if not data.get("success"):
            errors = data.get("errors", [])
            raise HTTPException(status_code=502, detail=f"Cloudflare error: {errors}")

        # variants[0] is the public delivery URL
        return data["result"]["variants"][0]
