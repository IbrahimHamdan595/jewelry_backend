"""Cloudflare R2 object storage for product/unit images.

Uploads go to the R2 bucket over its S3-compatible API using the R2_* settings;
the returned URL is the object's public URL (R2_PUBLIC_URL + key), which the
bucket must expose publicly (r2.dev public access or a custom domain).
"""
import os
import uuid

import boto3
from anyio import to_thread
from botocore.config import Config
from fastapi import HTTPException

from app.config import settings

# Map a content-type to a file extension when the upload has no usable one.
_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}


def _configured() -> bool:
    return all(
        (
            settings.r2_account_id,
            settings.r2_access_key_id,
            settings.r2_secret_access_key,
            settings.r2_bucket_name,
            settings.r2_public_url,
        )
    )


def _pick_extension(filename: str, content_type: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext and len(ext) <= 6 and ext.isascii() and ext.isprintable():
        return ext
    return _CONTENT_TYPE_EXT.get(content_type, ".jpg")


def _put(content: bytes, key: str, content_type: str) -> None:
    """Synchronous S3 put — run in a worker thread (boto3 is blocking)."""
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )
    client.put_object(
        Bucket=settings.r2_bucket_name,
        Key=key,
        Body=content,
        ContentType=content_type,
    )


async def upload_image(file_bytes: bytes, filename: str, content_type: str) -> str:
    """Store an image in R2 and return its public URL.

    Same signature as the previous Cloudflare-Images helper so callers are
    unchanged. Raises 503 if R2 is not configured, 502 if the upload fails.
    """
    if not _configured():
        raise HTTPException(status_code=503, detail="R2 storage is not configured")
    key = f"products/{uuid.uuid4().hex}{_pick_extension(filename, content_type)}"
    try:
        await to_thread.run_sync(_put, file_bytes, key, content_type)
    except Exception as exc:  # botocore ClientError / network errors
        raise HTTPException(status_code=502, detail=f"R2 upload failed: {exc}") from exc
    return f"{settings.r2_public_url.rstrip('/')}/{key}"
