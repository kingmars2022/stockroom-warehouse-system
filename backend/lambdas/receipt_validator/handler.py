"""Verify server-side what actually landed in the receipt bucket.

`/api/attachments/presign` can only check what the *client says* it is about to
upload — a declared content type and a declared size. The browser then uploads
straight to S3 through the presigned URL, so nothing on the server ever sees
the bytes. A client that declares `image/png` and sends a 40 MB executable
passes every check the API is able to make.

This closes that gap on the S3 side. It runs on ObjectCreated, reads only the
first few bytes, and decides whether the file's actual signature matches what
was declared. The verdict is written back as object tags, and the API refuses
to attach a receipt that did not pass.

Event-driven, bursty, and stateless — nothing here justifies a server sitting
idle between month-end expense runs. That is the argument for Lambda, and it is
the same argument for why the work does not belong in the request path: the
upload has already finished by the time this runs.
"""

from __future__ import annotations

import logging
import os
import urllib.parse

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_BYTES = int(os.environ.get("MAX_RECEIPT_SIZE_BYTES", 10 * 1024 * 1024))
SNIFF_BYTES = 16

# Only the types the API is willing to presign in the first place.
SIGNATURES: list[tuple[bytes, str]] = [
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
]

PASSED = "passed"
FAILED = "failed"


def detect_type(head: bytes) -> str | None:
    """Identify the file from its leading bytes, or None if unrecognised."""
    for signature, content_type in SIGNATURES:
        if head.startswith(signature):
            return content_type
    # WebP is RIFF with a WEBP marker four bytes later, so it needs its own check.
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def verdict(head: bytes, declared_type: str, size_bytes: int) -> tuple[str, str, str]:
    """Return (validation, detected_type, reason).

    Reasons are short slugs rather than prose: S3 tag values accept a limited
    character set, and these are read by code, not by a person.
    """
    detected = detect_type(head)
    if detected is None:
        return FAILED, "unknown", "unrecognised-file-signature"
    if size_bytes > MAX_BYTES:
        return FAILED, detected, "exceeds-max-size"
    if declared_type and detected != declared_type:
        return FAILED, detected, "declared-type-mismatch"
    return PASSED, detected, "ok"


def _client():
    return boto3.client("s3")


def process_record(bucket: str, key: str, client) -> dict:
    head_object = client.head_object(Bucket=bucket, Key=key)
    declared_type = head_object.get("ContentType", "")
    size_bytes = head_object["ContentLength"]

    # A ranged GET so a large object is never pulled into the function just to
    # read its first bytes.
    body = client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{SNIFF_BYTES - 1}")["Body"].read()

    validation, detected, reason = verdict(body, declared_type, size_bytes)
    client.put_object_tagging(
        Bucket=bucket,
        Key=key,
        Tagging={
            "TagSet": [
                {"Key": "validation", "Value": validation},
                {"Key": "detected-type", "Value": detected},
                {"Key": "reason", "Value": reason},
            ]
        },
    )
    logger.info("receipt %s: %s (%s, declared %s)", key, validation, reason, declared_type or "none")
    return {"key": key, "validation": validation, "detected_type": detected, "reason": reason}


def handler(event: dict, _context=None) -> dict:
    """S3 delivers records in batches, and one bad object must not strand the rest.

    A file that fails validation is a *result* — it gets tagged `failed` and the
    batch carries on. S3 or IAM failing is different: nothing was tagged, so the
    object would sit untagged forever and the API would answer 409 on it
    indefinitely. Lambda only retries an asynchronous invocation when the
    function *raises*, so the remaining records are processed first and then the
    infrastructure error is re-raised to buy those retries.
    """
    client = _client()
    results = []
    failure: Exception | None = None
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        try:
            results.append(process_record(bucket, key, client))
        except Exception as error:
            logger.exception("could not validate %s", key)
            results.append({"key": key, "validation": "errored"})
            failure = failure or error

    if failure is not None:
        raise failure
    return {"processed": len(results), "results": results}
