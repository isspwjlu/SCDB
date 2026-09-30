"""MinIO / S3 client wrapper.

Spec §4 requires MinIO for PDF and markdown caching. We expose:

- ``get_minio_client(endpoint, access_key, secret_key)`` -- lazily-built
  ``Minio`` instance.
- ``ensure_bucket(name)`` -- idempotent bucket creation.
- ``get_presigned_url(bucket, key, expires)`` -- download URL helper.
- ``upload_file``, ``download_file`` -- thin pass-throughs used by the
  submission endpoint (Task 31) and the ingest worker.

The client is created lazily so importing this module at startup does not
trigger a network connection.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from minio import Minio

log = logging.getLogger(__name__)


def get_minio_client(endpoint: str, access_key: str, secret_key: str) -> "Minio":
    """Build a :class:`minio.Minio` client.

    ``endpoint`` is ``host:port`` (no scheme). We force ``secure=False`` for
    local compose -- production should override via the helper that
    introspects the scheme.
    """
    from minio import Minio

    secure = endpoint.startswith("https://") or ":443" in endpoint
    bare_endpoint = endpoint.replace("https://", "").replace("http://", "")
    log.debug("building minio client endpoint=%s secure=%s", bare_endpoint, secure)
    return Minio(
        bare_endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )


def ensure_bucket(client: "Minio", bucket: str) -> None:
    """Create the bucket if it does not exist. Idempotent."""
    if not client.bucket_exists(bucket):
        log.info("creating minio bucket %s", bucket)
        client.make_bucket(bucket)


def get_presigned_url(
    client: "Minio",
    bucket: str,
    key: str,
    expires: int = 3600,
) -> str:
    """Return a presigned GET URL for ``bucket/key`` valid for ``expires`` seconds."""
    from datetime import timedelta

    return client.presigned_get_object(
        bucket,
        key,
        expires=timedelta(seconds=expires),
    )


def upload_file(
    client: "Minio",
    bucket: str,
    key: str,
    file_path: str,
    content_type: str | None = None,
) -> None:
    """Upload a local file to ``bucket/key``. Creates the bucket if needed."""
    ensure_bucket(client, bucket)
    log.debug("uploading %s -> s3://%s/%s", file_path, bucket, key)
    client.fput_object(bucket, key, file_path, content_type=content_type)


def download_file(client: "Minio", bucket: str, key: str, file_path: str) -> None:
    """Download ``bucket/key`` to ``file_path``."""
    log.debug("downloading s3://%s/%s -> %s", bucket, key, file_path)
    client.fget_object(bucket, key, file_path)


@lru_cache(maxsize=4)
def _cached_client(endpoint: str, access_key: str, secret_key: str) -> "Minio":
    """Memoised client builder keyed by endpoint+creds (safe per-host cache)."""
    return get_minio_client(endpoint, access_key, secret_key)


def upload_submission_pdf(
    client: "Minio",
    bucket: str,
    submission_id: str,
    file_path: str,
    original_filename: str,
    content_type: str | None = None,
) -> str:
    """Upload a submission PDF under ``submissions/{id}/{safe_name}``.

    Used by :mod:`superconductor_db.api.submissions` (Task 31). Returns the
    object key so the caller can persist it on the row. The bucket is created
    on demand.
    """
    safe_name = original_filename.replace("/", "_").replace("\\", "_") or "upload.pdf"
    key = f"submissions/{submission_id}/{safe_name}"
    upload_file(client, bucket, key, file_path, content_type=content_type)
    return key