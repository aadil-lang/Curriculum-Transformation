"""Env-gated S3 persistence for batch deliverables (approved sample + final CSVs).

Vibe pods have no persistent disk — every redeploy rebuilds the container, wiping
local `output/`. To keep the two deliverables that matter (the approved sample CSV
and the final/clean extracted CSV) across redeploys, they are mirrored to S3.

This module is a NO-OP unless ``DTA_S3_BUCKET`` is set, so local development is
unaffected (pure filesystem, no AWS creds required). When configured, uploads/
downloads use the default boto3 credential chain (the pod's IAM role on Vibe).

S3 layout:
    s3://<DTA_S3_BUCKET>/<DTA_S3_PREFIX>/<batch_name>/<filename>

Intermediate artifacts (schema, review, analysis plans, sample template) are NOT
persisted — only the deliverables.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# Deliverable filenames worth persisting. Others are disposable/regenerable.
_APPROVED_SAMPLE_NAME = "approved_sample.csv"


def _bucket() -> str:
    return os.getenv("DTA_S3_BUCKET", "").strip()


def _prefix() -> str:
    return os.getenv("DTA_S3_PREFIX", "data-transformation").strip().strip("/")


def is_enabled() -> bool:
    """True only when an S3 bucket is configured — otherwise every op is a no-op."""
    return bool(_bucket())


@lru_cache(maxsize=1)
def _client():
    import boto3  # imported lazily so local runs never require boto3/creds

    return boto3.client("s3")


def _key(batch_name: str, filename: str) -> str:
    return f"{_prefix()}/{batch_name}/{filename}"


def upload_file(batch_name: str, local_path: Path, remote_name: str | None = None) -> None:
    """Upload one deliverable file to S3. No-op when disabled or file missing.

    ``remote_name`` overrides the S3 filename (defaults to the local filename) so
    callers can force a canonical key regardless of the local file's name.
    """
    if not is_enabled() or not local_path.exists():
        return
    name = remote_name or local_path.name
    try:
        _client().upload_file(str(local_path), _bucket(), _key(batch_name, name))
        LOGGER.info("Persisted %s to s3://%s/%s", name, _bucket(), _key(batch_name, name))
    except Exception as exc:  # noqa: BLE001 - persistence must never break the request
        LOGGER.warning("S3 upload failed for %s (%s); continuing.", local_path, exc)


def download_file(batch_name: str, filename: str, local_path: Path) -> bool:
    """Fetch a deliverable from S3 to local_path. Returns True on success.

    No-op (False) when disabled. Used as a fallback when a redeployed pod has an
    empty disk but the file exists in S3.
    """
    if not is_enabled():
        return False
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        _client().download_file(_bucket(), _key(batch_name, filename), str(local_path))
        return True
    except Exception as exc:  # noqa: BLE001 - missing key / transient error → treat as absent
        LOGGER.debug("S3 download miss for %s/%s (%s).", batch_name, filename, exc)
        return False


def list_batch_names() -> list[str]:
    """Batch names that have at least one persisted deliverable in S3.

    Lets a redeployed pod (empty disk) still show past batches. Empty when disabled.
    """
    if not is_enabled():
        return []
    prefix = f"{_prefix()}/"
    names: set[str] = set()
    try:
        paginator = _client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix, Delimiter="/"):
            for common in page.get("CommonPrefixes", []) or []:
                sub = common.get("Prefix", "")
                name = sub[len(prefix):].strip("/")
                if name:
                    names.add(name)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("S3 batch listing failed (%s); returning none.", exc)
        return []
    return sorted(names)
