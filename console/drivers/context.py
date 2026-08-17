"""The console-level facilities every driver reads from ``context`` — and
their PRODUCTION defaults.

Drivers are handed a context dict carrying ``document_reader(uri) -> str``
and ``object_stat(uri) -> iso-8601 | None`` (`console/config.py::
_driver_context`). Tests inject fakes. Until 2026-08-17 nothing built the
real ones: ``document_reader`` existed only as the ``_driver_context`` test
hook, so on the live box every ``document-fields``, ``s3-records`` and
``emitted-envelope`` binding failed with "no document reader available"
(alpha-engine-config-I7425) — the one-file onboarding promise of
`console-policy.md` §2.6 was inert for any S3-bound descriptor. Only
``object-store`` carried its own boto3 default, privately.

So this is the ONE place the defaults live: boto3-backed when the optional
``aws`` extra is installed (through `console/aws.py`'s per-service client
cache, config-I7124), ``None`` otherwise so a driver fails loud with
``unavailable=("reader",)`` rather than rendering absence (§2.3). URIs are
``s3://bucket/key``; a plain path or ``file://`` reads the local filesystem,
which is what a descriptor committed beside a module writing to disk needs.
Nothing here knows which bucket any component uses (§2.7).
"""
from __future__ import annotations

import os
from typing import Any, Callable

from ..aws import client as _aws_client

DocumentReader = Callable[[str], Any]
ObjectStat = Callable[[str], "str | None"]


def _split_s3(uri: str) -> tuple[str, str]:
    bucket, _, key = uri[len("s3://"):].partition("/")
    if not bucket or not key:
        raise ValueError(f"not a bucket/key s3:// URI: {uri!r}")
    return bucket, key


def _local_path(uri: str) -> str:
    return uri[len("file://"):] if uri.startswith("file://") else uri


def _boto3_available() -> bool:
    try:
        import boto3  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


def default_document_reader() -> DocumentReader | None:
    """``uri -> body text``; raises ``FileNotFoundError`` when the object or
    file is not there (the drivers' "declared and never written" branch)."""
    s3_ok = _boto3_available()

    def read(uri: str) -> str:
        if uri.startswith("s3://"):
            if not s3_ok:
                raise RuntimeError("s3:// document reader needs the `aws` extra")
            client = _aws_client("s3")
            bucket, key = _split_s3(uri)
            try:
                obj = client.get_object(Bucket=bucket, Key=key)
            except client.exceptions.NoSuchKey as exc:
                raise FileNotFoundError(uri) from exc
            except client.exceptions.ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                    raise FileNotFoundError(uri) from exc
                raise
            return obj["Body"].read().decode("utf-8")
        with open(_local_path(uri), encoding="utf-8") as fh:  # FileNotFoundError propagates
            return fh.read()

    return read


def default_object_stat() -> ObjectStat | None:
    """``uri -> last-modified iso-8601``, ``None`` when the object is absent
    (declared and not there — a finding, not an error)."""
    s3_ok = _boto3_available()

    def stat(uri: str) -> str | None:
        if uri.startswith("s3://"):
            if not s3_ok:
                raise RuntimeError("s3:// object stat needs the `aws` extra")
            client = _aws_client("s3")
            bucket, key = _split_s3(uri)
            try:
                head = client.head_object(Bucket=bucket, Key=key)
            except client.exceptions.ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                    return None
                raise
            return head["LastModified"].isoformat()
        path = _local_path(uri)
        if not os.path.exists(path):
            return None
        from datetime import datetime, timezone
        return datetime.fromtimestamp(os.stat(path).st_mtime, tz=timezone.utc).isoformat()

    return stat


def defaults() -> dict[str, Any]:
    """What `_driver_context` merges UNDER the injected values."""
    return {"document_reader": default_document_reader(),
            "object_stat": default_object_stat()}
