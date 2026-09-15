"""The public price webhook, and the ingestion that gives its submissions meaning.

The webhook is reachable by anyone on the internet, so most of what matters
here is what it *refuses*: a wrong signature, a replayed one, a body that
disagrees with the signature, and malformed content. The ingestion half is
where a submission is finally compared against what the item last cost.
"""

import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi import HTTPException

from app.models import Purchase
from app.services import apply_price_submission, ingest_supplier_prices
from lambdas.supplier_price_webhook import handler as webhook

SECRET = "test-secret-value"


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(webhook, "SECRET", SECRET)
    monkeypatch.setattr(webhook, "BUCKET", "test-bucket")


class FakeS3:
    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = dict(objects or {})
        self.deleted: list[str] = []

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body

    def list_objects_v2(self, Bucket, Prefix):
        return {"Contents": [{"Key": key} for key in sorted(self.objects) if key.startswith(Prefix)]}

    def get_object(self, Bucket, Key):
        return {"Body": _Body(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


def signed(payload: dict, secret: str = SECRET, timestamp: int | None = None) -> dict:
    body = json.dumps(payload)
    stamp = str(timestamp if timestamp is not None else int(time.time()))
    digest = hmac.new(secret.encode(), f"{stamp}.{body}".encode(), hashlib.sha256).hexdigest()
    return {
        "body": body,
        "headers": {"x-stockroom-timestamp": stamp, "x-stockroom-signature": f"v1={digest}"},
    }


def quote(**overrides) -> dict:
    return {"supplier_id": str(uuid.uuid4()), "sku": "SKU-1", "unit_cost": 12.5, "currency": "CAD", **overrides}


# --------------------------------------------------------------------------
# What the webhook refuses
# --------------------------------------------------------------------------

def test_a_correctly_signed_submission_is_accepted(monkeypatch):
    client = FakeS3()
    monkeypatch.setattr(webhook, "_client", lambda: client)

    response = webhook.handler(signed(quote(idempotency_key="abc")))

    assert response["statusCode"] == 202
    assert list(client.objects) == ["price-submissions/abc.json"]


def test_an_unsigned_request_is_rejected(monkeypatch):
    monkeypatch.setattr(webhook, "_client", lambda: FakeS3())

    response = webhook.handler({"body": json.dumps(quote()), "headers": {}})

    assert response["statusCode"] == 401


def test_a_signature_from_the_wrong_secret_is_rejected(monkeypatch):
    monkeypatch.setattr(webhook, "_client", lambda: FakeS3())

    response = webhook.handler(signed(quote(), secret="not-the-secret"))

    assert response["statusCode"] == 401


def test_a_tampered_body_no_longer_matches_its_signature(monkeypatch):
    client = FakeS3()
    monkeypatch.setattr(webhook, "_client", lambda: client)
    event = signed(quote(unit_cost=12.5))
    event["body"] = event["body"].replace("12.5", "1.0")

    assert webhook.handler(event)["statusCode"] == 401
    assert client.objects == {}


def test_a_replayed_request_expires(monkeypatch):
    """The timestamp is inside the signed material, so it cannot be swapped."""
    monkeypatch.setattr(webhook, "_client", lambda: FakeS3())

    stale = signed(quote(), timestamp=int(time.time()) - 3600)

    assert webhook.handler(stale)["statusCode"] == 401


def test_a_non_numeric_timestamp_is_rejected():
    assert webhook.signature_is_valid("{}", "not-a-number", "v1=whatever") is False


def test_an_oversized_body_is_rejected_before_any_work(monkeypatch):
    monkeypatch.setattr(webhook, "_client", lambda: FakeS3())

    response = webhook.handler({"body": "x" * (webhook.MAX_BODY_BYTES + 1), "headers": {}})

    assert response["statusCode"] == 413


def test_invalid_json_that_is_correctly_signed_is_a_400(monkeypatch):
    monkeypatch.setattr(webhook, "_client", lambda: FakeS3())
    stamp = str(int(time.time()))
    digest = hmac.new(SECRET.encode(), f"{stamp}.not json".encode(), hashlib.sha256).hexdigest()

    response = webhook.handler({"body": "not json", "headers": {"x-stockroom-timestamp": stamp, "x-stockroom-signature": f"v1={digest}"}})

    assert response["statusCode"] == 400


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        (quote(unit_cost=0), "greater than zero"),
        (quote(unit_cost="free"), "must be a number"),
        (quote(currency="DOLLARS"), "3-letter"),
        ({"sku": "SKU-1"}, "missing required fields"),
    ],
)
def test_malformed_submissions_are_unprocessable(monkeypatch, payload, fragment):
    monkeypatch.setattr(webhook, "_client", lambda: FakeS3())

    response = webhook.handler(signed(payload))

    assert response["statusCode"] == 422
    assert fragment in json.loads(response["body"])["error"]


def test_an_idempotency_key_makes_a_retry_overwrite_not_duplicate(monkeypatch):
    client = FakeS3()
    monkeypatch.setattr(webhook, "_client", lambda: client)

    webhook.handler(signed(quote(idempotency_key="same")))
    webhook.handler(signed(quote(idempotency_key="same", unit_cost=13.0)))

    assert list(client.objects) == ["price-submissions/same.json"]
    assert json.loads(client.objects["price-submissions/same.json"])["unit_cost"] == 13.0


# --------------------------------------------------------------------------
# Ingestion: comparing the quote against what the item actually last cost
# --------------------------------------------------------------------------

def last_purchase(db, item, supplier, admin, unit_cost):
    purchase = Purchase(item_id=item.id, supplier_id=supplier.id, received_by_id=admin.id, quantity=1, unit_cost=unit_cost, currency="CAD", invoice_number="SEED")
    db.add(purchase)
    db.commit()
    return purchase


def test_a_rise_past_the_threshold_raises_an_alert(db, admin, item, supplier, store):
    last_purchase(db, item, supplier, admin, 10.00)

    result = apply_price_submission(db, admin, {"sku": item.sku, "supplier_id": str(supplier.id), "unit_cost": 13.00, "currency": "cad"}, threshold=15)

    assert result["status"] == "alert"
    assert result["price_change_percent"] == 30.0
    event = store.query({"action": "supplier_price_alert"}, 5)[0]
    assert event["payload"]["threshold_breached"] is True
    assert event["payload"]["previous_unit_cost"] == 10.0


def test_a_rise_below_the_threshold_is_only_recorded(db, admin, item, supplier, store):
    last_purchase(db, item, supplier, admin, 10.00)

    result = apply_price_submission(db, admin, {"sku": item.sku, "supplier_id": str(supplier.id), "unit_cost": 10.50, "currency": "cad"}, threshold=15)

    assert result["status"] == "recorded"
    assert store.query({"action": "supplier_price_alert"}, 5) == []
    assert len(store.query({"action": "supplier_price_quoted"}, 5)) == 1


def test_an_item_with_no_purchase_history_cannot_have_risen(db, admin, item, supplier, store):
    result = apply_price_submission(db, admin, {"sku": item.sku, "supplier_id": str(supplier.id), "unit_cost": 99.0, "currency": "cad"}, threshold=15)

    assert result["status"] == "recorded"
    assert result["price_change_percent"] == 0


@pytest.mark.parametrize(
    ("submission", "expected"),
    [
        ({"sku": "NOPE", "supplier_id": str(uuid.uuid4()), "unit_cost": 1.0, "currency": "cad"}, "unknown_sku"),
        ({"sku": "__item__", "supplier_id": str(uuid.uuid4()), "unit_cost": 1.0, "currency": "cad"}, "unknown_supplier"),
        ({"sku": "__item__", "supplier_id": "not-a-uuid", "unit_cost": 1.0, "currency": "cad"}, "unknown_supplier"),
    ],
)
def test_submissions_that_do_not_resolve_are_reported_not_raised(db, admin, item, submission, expected):
    submission = {**submission, "sku": item.sku if submission["sku"] == "__item__" else submission["sku"]}

    assert apply_price_submission(db, admin, submission, threshold=15)["status"] == expected


def configure_bucket(monkeypatch, client):
    from app import services

    monkeypatch.setattr(services.get_settings(), "receipt_bucket_name", "test-bucket", raising=False)
    monkeypatch.setattr(services.boto3, "client", lambda *args, **kwargs: client)


def test_ingest_drains_the_inbox_and_deletes_what_it_processed(monkeypatch, db, admin, item, supplier, store):
    last_purchase(db, item, supplier, admin, 10.00)
    client = FakeS3({
        "price-submissions/a.json": json.dumps({"sku": item.sku, "supplier_id": str(supplier.id), "unit_cost": 13.0, "currency": "cad"}).encode(),
        "price-submissions/b.json": json.dumps({"sku": item.sku, "supplier_id": str(supplier.id), "unit_cost": 10.2, "currency": "cad"}).encode(),
    })
    configure_bucket(monkeypatch, client)

    results = ingest_supplier_prices(db, admin)

    assert [result["status"] for result in results] == ["alert", "recorded"]
    assert sorted(client.deleted) == ["price-submissions/a.json", "price-submissions/b.json"]
    assert client.objects == {}


def test_an_unreadable_submission_does_not_stop_the_batch(monkeypatch, db, admin, item, supplier, store):
    last_purchase(db, item, supplier, admin, 10.00)
    client = FakeS3({
        "price-submissions/bad.json": b"{not json",
        "price-submissions/good.json": json.dumps({"sku": item.sku, "supplier_id": str(supplier.id), "unit_cost": 13.0, "currency": "cad"}).encode(),
    })
    configure_bucket(monkeypatch, client)

    results = ingest_supplier_prices(db, admin)

    assert {result["status"] for result in results} == {"unreadable", "alert"}
    # The unreadable one is left in place to be looked at, not silently dropped.
    assert client.deleted == ["price-submissions/good.json"]


def test_only_administrators_can_ingest(db, employee):
    with pytest.raises(HTTPException) as raised:
        ingest_supplier_prices(db, employee)
    assert raised.value.status_code == 403


def test_ingest_without_a_bucket_configured_is_unavailable(db, admin):
    with pytest.raises(HTTPException) as raised:
        ingest_supplier_prices(db, admin)
    assert raised.value.status_code == 503
