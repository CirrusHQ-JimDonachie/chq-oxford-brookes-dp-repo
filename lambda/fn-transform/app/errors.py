"""Writing rejected files and records to the error prefix of the landing bucket.

Everything written here is a data problem for the source's owner to look at, not
a platform failure. The prefix is a sibling of the raw prefix the S3
notification is filtered to, so writing an error record never re-triggers the
engine.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from config import ObjectPath, ObjectRef
    from mapping import Rejection
    from mypy_boto3_s3.client import S3Client
    from reasons import ReasonCode

FILE_REASON_SUFFIX = ".rejected.json"
RECORD_REJECTS_SUFFIX = ".rejects.jsonl"


def error_key(error_prefix: str, relative_key: str) -> str:
    """Where a rejected object's copy lands under the error prefix."""
    return f"{error_prefix}{relative_key}"


def reject_file(
    client: S3Client,
    *,
    ref: ObjectRef,
    error_prefix: str,
    reason: ReasonCode,
    detail: str,
) -> None:
    """Copy a whole rejected object to the error prefix and record why.

    Args:
        client: S3 client.
        ref: The delivered object.
        error_prefix: Prefix error output is written under.
        reason: Machine-readable rejection reason.
        detail: One line a data owner can act on.
    """
    destination = error_key(error_prefix, ref.relative_key)
    if ref.version_id is not None:
        client.copy_object(
            Bucket=ref.bucket,
            Key=destination,
            CopySource={
                "Bucket": ref.bucket,
                "Key": ref.key,
                "VersionId": ref.version_id,
            },
        )
    else:
        client.copy_object(
            Bucket=ref.bucket,
            Key=destination,
            CopySource={"Bucket": ref.bucket, "Key": ref.key},
        )

    body = {
        "reason_code": str(reason),
        "detail": detail,
        "source_bucket": ref.bucket,
        "source_key": ref.key,
        "source_version_id": ref.version_id,
    }
    client.put_object(
        Bucket=ref.bucket,
        Key=f"{destination}{FILE_REASON_SUFFIX}",
        Body=json.dumps(body, indent=2).encode(),
        ContentType="application/json",
    )


def write_record_rejections(
    client: S3Client,
    *,
    ref: ObjectRef,
    path: ObjectPath,
    error_prefix: str,
    rejections: Sequence[Rejection],
) -> None:
    """Write one JSON Lines object holding every rejected record from a file.

    One object per source file keeps the error prefix navigable: a data owner
    opens the file that matches the drop they made, not thousands of single
    records.

    Args:
        client: S3 client.
        ref: The delivered object.
        path: The source and type its key resolved to.
        error_prefix: Prefix error output is written under.
        rejections: The rejected records, in file order.
    """
    if not rejections:
        return

    lines = [
        json.dumps(
            {
                "reason_code": str(rejection.reason),
                "detail": rejection.detail,
                "source_key": ref.key,
                "source": path.source,
                "type": path.type,
                "record_index": rejection.record_index,
                "record": dict(rejection.record),
            }
        )
        for rejection in rejections
    ]
    client.put_object(
        Bucket=ref.bucket,
        Key=f"{error_key(error_prefix, ref.relative_key)}{RECORD_REJECTS_SUFFIX}",
        Body=("\n".join(lines) + "\n").encode(),
        ContentType="application/x-ndjson",
    )
