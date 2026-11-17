"""Content-addressable S3 uploads.

Same file uploaded twice -> same key -> second call is a HEAD-only no-op.

Works against real AWS S3 and any S3-compatible store (Cloudflare R2, MinIO,
Backblaze B2, ...). Point it at a non-AWS store by setting S3_ENDPOINT_URL and
S3_PUBLIC_BASE_URL (see app.config.Settings).
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path

import boto3
import botocore.exceptions

from app.config import Settings

KEY_PREFIX = "product_flatlays/crops"


def content_hash(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def s3_key_for(file_hash: str, original_path: str,
               key_prefix: str = KEY_PREFIX) -> str:
    ext = os.path.splitext(original_path)[1].lower()
    if ext:
        return f"{key_prefix}/{file_hash}{ext}"
    return f"{key_prefix}/{file_hash}"


class S3Uploader:
    def __init__(self, s3_client, bucket: str, region: str,
                 public_base_url: str | None = None):
        self._s3 = s3_client
        self._bucket = bucket
        self._region = region
        # e.g. "https://pub-xxxx.r2.dev" or a custom domain. When set, public
        # URLs are "{base}/{key}" instead of the AWS virtual-host pattern.
        self._public_base_url = (public_base_url or "").rstrip("/")

    def upload(self, local_path: str, *, key_prefix: str = KEY_PREFIX) -> str:
        h = content_hash(local_path)
        key = s3_key_for(h, local_path, key_prefix)

        if not self._exists(key):
            content_type = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
            self._s3.upload_file(
                Filename=local_path,
                Bucket=self._bucket,
                Key=key,
                ExtraArgs={"ContentType": content_type},
            )

        return self._public_url(key)

    def _public_url(self, key: str) -> str:
        if self._public_base_url:
            return f"{self._public_base_url}/{key}"
        return f"https://{self._bucket}.s3.{self._region}.amazonaws.com/{key}"

    def _exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
            return True
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            # 404/NoSuchKey/NotFound: object is missing.
            # 403/Forbidden: bucket policy hides existence (no s3:ListBucket).
            # Treat both as "assume missing"; PUT will surface a real failure.
            if code in ("404", "NoSuchKey", "NotFound", "403", "Forbidden", "AccessDenied"):
                return False
            raise


def make_uploader(settings: Settings) -> S3Uploader:
    if not settings.aws_access_key_id or not settings.aws_secret_access_key:
        raise RuntimeError(
            "AWS credentials are not set. Add AWS_ACCESS_KEY_ID and "
            "AWS_SECRET_ACCESS_KEY to .env."
        )
    client_kwargs = dict(
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
    if settings.s3_endpoint_url:
        client_kwargs["endpoint_url"] = settings.s3_endpoint_url
        # botocore >= 1.36 defaults to adding CRC checksum trailers on uploads,
        # which R2 (and some other S3-compatible stores) reject. Only send them
        # when the operation actually requires it. Scoped to custom endpoints so
        # real-AWS behaviour is unchanged.
        from botocore.config import Config

        client_kwargs["config"] = Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )

    s3 = boto3.client("s3", **client_kwargs)
    return S3Uploader(
        s3,
        bucket=settings.aws_bucket_name,
        region=settings.aws_region,
        public_base_url=settings.s3_public_base_url,
    )
