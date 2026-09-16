"""Accept supplier price pushes at a public endpoint, behind API Gateway.

Suppliers do not have Cognito accounts and are not people clicking a console,
so this cannot hang off the main API's auth. It authenticates the way webhooks
normally do — an HMAC-SHA256 signature over the raw body with a shared secret,
plus a timestamp so a captured request cannot be replayed later.

The handler does not write to the database, and that is deliberate twice over:

* A webhook must answer inside the caller's timeout and must not lose a
  submission because our backend happens to be down. Accepting into durable
  storage and ingesting on our own schedule decouples the public ingress from
  backend availability, which is the usual shape for receiving webhooks.
* Practically, the database is private. A function reachable from the public
  internet is the last thing that should hold a route to it.

So a valid submission lands in S3 under `price-submissions/`, and
`POST /api/supplier-prices/ingest` drains that inbox, compares each quote
against what the item actually last cost, and raises a price alert when the
rise clears the configured threshold.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import time
import uuid
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BUCKET = os.environ.get("RECEIPT_BUCKET_NAME", "")
SECRET = os.environ.get("WEBHOOK_SECRET", "")
PREFIX = "price-submissions/"
MAX_SKEW_SECONDS = int(os.environ.get("MAX_SKEW_SECONDS", 300))
MAX_BODY_BYTES = 64 * 1024
MAX_UNIT_COST = 1_000_000

REQUIRED = ("supplier_id", "sku", "unit_cost", "currency")


def _response(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def signature_is_valid(raw_body: str, timestamp: str, provided: str) -> bool:
    """Verify `v1=<hex>` over "<timestamp>.<body>", in constant time.

    Signing the timestamp alongside the body is what makes the freshness check
    meaningful: without it an attacker could keep a valid signature and simply
    replace the timestamp header.
    """
    if not SECRET or not provided or not timestamp:
        return False
    try:
        age = abs(time.time() - int(timestamp))
    except ValueError:
        return False
    if age > MAX_SKEW_SECONDS:
        return False

    expected = hmac.new(SECRET.encode(), f"{timestamp}.{raw_body}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v1={expected}", provided)


def validate_payload(payload: dict) -> str | None:
    """Return an error message, or None when the submission is well-formed."""
    if not isinstance(payload, dict):
        return "body must be a JSON object"
    missing = [field for field in REQUIRED if payload.get(field) in (None, "")]
    if missing:
        return f"missing required fields: {', '.join(missing)}"
    if isinstance(payload["unit_cost"], bool):
        return "unit_cost must be a number"
    try:
        unit_cost = float(payload["unit_cost"])
    except (TypeError, ValueError):
        return "unit_cost must be a number"
    # float() happily accepts "NaN" and "Infinity". Both survive a > 0 test
    # (NaN fails every comparison), reach the price-change arithmetic, and
    # produce alerts that mean nothing.
    if not math.isfinite(unit_cost):
        return "unit_cost must be a finite number"
    if unit_cost <= 0:
        return "unit_cost must be greater than zero"
    if unit_cost > MAX_UNIT_COST:
        return f"unit_cost must be below {MAX_UNIT_COST}"
    currency = str(payload["currency"])
    if len(currency) != 3 or not currency.isalpha():
        return "currency must be a 3-letter code"
    return None


def _key_segment(value: Any) -> str:
    """Make a caller-supplied string safe to place in an object key.

    Both halves of the key come from the request body, so neither can be
    trusted to be a tidy identifier — or to be short.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value))[:128] or "unknown"


def _client():
    return boto3.client("s3")


def handler(event: dict, _context=None) -> dict:
    headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
    raw_body = event.get("body") or ""

    if len(raw_body.encode()) > MAX_BODY_BYTES:
        return _response(413, {"error": "body too large"})

    if not signature_is_valid(raw_body, headers.get("x-stockroom-timestamp", ""), headers.get("x-stockroom-signature", "")):
        # Deliberately not saying which part failed: a signature oracle helps
        # nobody but an attacker.
        logger.warning("rejected a submission with an invalid signature")
        return _response(401, {"error": "invalid signature"})

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return _response(400, {"error": "body is not valid JSON"})

    error = validate_payload(payload)
    if error:
        return _response(422, {"error": error})

    # The supplier's own id for the submission, when it sends one, makes a
    # retry overwrite its first attempt instead of queueing a duplicate. It is
    # scoped to the sender: an idempotency key is only ever unique per client,
    # so a flat namespace let two suppliers both sending "001" overwrite each
    # other — both acknowledged, one quote silently gone. The ingest side
    # already dedupes per supplier; this makes the inbox agree with it.
    submission_id = _key_segment(payload.get("idempotency_key") or uuid.uuid4())
    key = f"{PREFIX}{_key_segment(payload['supplier_id'])}/{submission_id}.json"

    _client().put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps({**payload, "submission_id": submission_id, "received_at": int(time.time())}).encode(),
        ContentType="application/json",
    )
    logger.info("accepted price submission %s for sku %s", submission_id, payload["sku"])
    return _response(202, {"accepted": True, "submission_id": submission_id})
