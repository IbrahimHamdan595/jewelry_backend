import mimetypes
import uuid
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException

from app.config import settings


def _r2_client():
    if not all([
        settings.r2_account_id,
        settings.r2_access_key_id,
        settings.r2_secret_access_key,
        settings.r2_bucket_name,
        settings.r2_public_url,
    ]):
        raise HTTPException(status_code=503, detail="R2 storage is not configured")

    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


async def upload_image(file_bytes: bytes, filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix.lower() or mimetypes.guess_extension(content_type) or ""
    key = f"products/{uuid.uuid4().hex}{suffix}"

    client = _r2_client()
    try:
        client.put_object(
            Bucket=settings.r2_bucket_name,
            Key=key,
            Body=file_bytes,
            ContentType=content_type,
            CacheControl="public, max-age=31536000, immutable",
        )
    except (BotoCoreError, ClientError) as e:
        raise HTTPException(status_code=502, detail=f"R2 upload failed: {e}") from e

    return f"{settings.r2_public_url.rstrip('/')}/{key}"
