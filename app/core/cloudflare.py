import uuid
import boto3
from botocore.config import Config
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
        raise HTTPException(status_code=503, detail="Cloudflare R2 is not configured")

    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


async def upload_image(file_bytes: bytes, filename: str, content_type: str) -> str:
    client = _r2_client()

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    key = f"products/{uuid.uuid4().hex}.{ext}"

    client.put_object(
        Bucket=settings.r2_bucket_name,
        Key=key,
        Body=file_bytes,
        ContentType=content_type,
    )

    base = settings.r2_public_url.rstrip("/")
    return f"{base}/{key}"
