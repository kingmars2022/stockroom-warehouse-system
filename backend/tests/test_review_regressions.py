"""Regressions for defects found in a review of the features added above.

Every test here failed when it was written. They are kept together rather than
filed into the per-module suites because what they have in common is the reason
they were missed: each sits where the original tests checked that a design
worked *as imagined*, instead of what the underlying mechanism actually does.

The recurring shapes are worth naming, because they generalise:

* **A guarantee tested only on a path that never reaches it.** The original
  rollback test exercised a stock-shortage rejection, which returns before any
  audit entry is buffered — so it passed without ever touching the case it
  claimed to cover. `after_commit` turned out to fire on SAVEPOINT release too.
* **A stand-in that cannot fail the way the real thing does.** The scripted
  model client makes the agent testable with no API key, but it ignores the
  message history it is handed — so a provider-protocol bug sat behind a green
  suite.
* **Validation that checks existence but not meaning.** A real SKU, a real
  supplier and a positive integer do not add up to a sensible order.
* **An input that passes a check by accident.** `float("NaN") <= 0` is False,
  so NaN satisfied a "must be positive" test.
* **A dependency added to the file CI installs, not the one the image does.**
"""

import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.audit_events import get_event_store, publish, query as query_events, set_event_store
from app.agent.llm import GeminiClient, LLMResponse, ScriptedClient, ToolCall
from app.agent.loop import run_agent
from app.agent.tools import ToolError, dispatch
from app.models import AuditLog, Purchase, SupplierStatus
from app.services import apply_price_submission, write_audit
from lambdas.receipt_validator import handler as validator
from lambdas.supplier_price_webhook import handler as webhook


def test_outer_rollback_must_not_leave_published_events(db, admin, store):
    # An explicit BEGIN also avoids sqlite's legacy savepoint autocommit.
    db.connection().exec_driver_sql("BEGIN")
    with db.begin_nested():
        write_audit(db, admin, "review_probe", "item", "x", "rollback probe")
    db.rollback()
    assert db.scalars(select(AuditLog)).all() == []
    assert store.query({}, 10) == []


def test_runtime_store_failure_should_preserve_fallback_event(store):
    class Broken:
        name = "mongodb"
        def append(self, documents):
            raise RuntimeError("Mongo disappeared after startup")
    set_event_store(Broken())
    publish([{"_id": "review-runtime-failure"}])
    assert get_event_store().name == "memory"


def test_container_lock_includes_mongodb_driver():
    lock = Path("requirements.lock.txt").read_text().lower()
    assert "pymongo==" in lock


def test_price_submission_is_idempotent_after_consumption(db, admin, item, supplier, store):
    submission = {"sku": item.sku, "supplier_id": str(supplier.id), "unit_cost": 13,
                  "currency": "CAD", "submission_id": "supplier-retry-123"}
    apply_price_submission(db, admin, submission, 15)
    apply_price_submission(db, admin, submission, 15)
    assert len(db.scalars(select(AuditLog)).all()) == 1


@pytest.mark.parametrize("unit_cost", ["NaN", "Infinity"])
def test_webhook_refuses_nonfinite_price(unit_cost):
    assert webhook.validate_payload({"supplier_id": "x", "sku": "SKU", "unit_cost": unit_cost,
                                     "currency": "CAD"}) is not None


def test_agent_rejects_in_stock_item_and_paused_supplier(db, item, supplier):
    supplier.status = SupplierStatus.paused
    db.commit()
    with pytest.raises(ToolError):
        dispatch(db, "propose_purchase", {"sku": item.sku, "supplier_name": supplier.name,
                 "quantity": 1000000, "reason": "untrusted model output"})


def test_agent_handles_invalid_limit_as_tool_error(db, admin):
    client = ScriptedClient([LLMResponse(tool_calls=[ToolCall("replenishment_needs", {"limit": "many"})]),
                             LLMResponse(text="recovered")])
    run = run_agent(db, admin, client, "test invalid argument")
    assert not run.steps[0]["ok"]
    assert run.summary == "recovered"


def test_gemini_second_turn_retains_function_call(db, admin, monkeypatch):
    from app.agent import llm
    captured = []
    class Response:
        def __init__(self, payload):
            self.payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self.payload
    def post(url, **kwargs):
        captured.append(kwargs["json"])
        part = ({"functionCall": {"name": "replenishment_needs", "args": {}}}
                if len(captured) == 1 else {"text": "done"})
        return Response({"candidates": [{"content": {"role": "model", "parts": [part]}}]})
    monkeypatch.setattr(llm.httpx, "post", post)
    run_agent(db, admin, GeminiClient("fake-key", "fake-model"), "review")
    model_parts = [part for message in captured[1]["contents"] if message["role"] == "model"
                   for part in message["parts"]]
    assert any("functionCall" in part for part in model_parts), captured[1]["contents"]


def test_receipt_transient_error_must_not_acknowledge_success(monkeypatch):
    class BrokenS3:
        def head_object(self, **kwargs):
            raise TimeoutError("temporary S3 failure")
    monkeypatch.setattr(validator, "_client", lambda: BrokenS3())
    with pytest.raises(Exception):
        validator.handler({"Records": [{"s3": {"bucket": {"name": "review-bucket"},
                                                 "object": {"key": "receipts/u/file.png"}}}]})


def test_attached_receipt_cannot_be_downloaded_after_failed_replacement(db, employee, monkeypatch):
    from app import main, services
    from fastapi import HTTPException
    class FakeS3:
        verdict = "passed"
        def get_object_tagging(self, **kwargs):
            return {"TagSet": [{"Key": "validation", "Value": self.verdict}]}
        def generate_presigned_url(self, *args, **kwargs):
            return "https://example.invalid/current-replaced-object"
    s3 = FakeS3()
    monkeypatch.setattr(services.get_settings(), "receipt_bucket_name", "review-bucket")
    monkeypatch.setattr(main.settings, "receipt_bucket_name", "review-bucket")
    monkeypatch.setattr(services.boto3, "client", lambda *args, **kwargs: s3)
    key = f"receipts/{employee.id}/receipt.png"
    services.create_expense(db, employee, supplier="Store", quantity=1, amount=10,
                            currency="CAD", purpose="receipt test", receipt_key=key)
    s3.verdict = "failed"
    with pytest.raises(HTTPException):
        main.create_download_intent(key=key, db=db, user=employee)


def test_cross_currency_prices_are_not_compared_as_same_currency(db, admin, item, supplier):
    db.add(Purchase(item_id=item.id, supplier_id=supplier.id, received_by_id=admin.id,
                    quantity=1, unit_cost=10, currency="USD"))
    db.commit()
    result = apply_price_submission(db, admin,
        {"sku": item.sku, "supplier_id": str(supplier.id), "unit_cost": 13.5, "currency": "CAD"}, 15)
    assert result["status"] != "alert", result


def test_savepoint_rollback_must_not_discard_events_buffered_before_it(db, admin, store):
    """A failed savepoint took the whole buffer with it, including events from
    outside it whose rows still commit — leaving `audit_logs` holding an entry
    the document store never received. The two are supposed to join on that id.
    """
    db.connection().exec_driver_sql("BEGIN")
    write_audit(db, admin, "before_savepoint", "item", "x", "buffered before the savepoint opened")
    with pytest.raises(RuntimeError):
        with db.begin_nested():
            write_audit(db, admin, "inside_savepoint", "item", "y", "buffered inside it")
            raise RuntimeError("the business rule this savepoint guards failed")
    db.commit()

    committed = {row.action for row in db.scalars(select(AuditLog)).all()}
    published = {document["action"] for document in store.query({}, 10)}
    assert committed == {"before_savepoint"}
    assert published == committed, "the relational log and the document store disagree"


def test_concurrent_store_failure_must_not_drop_the_first_thread_s_events(store):
    """Each failing writer used to build its *own* fallback and install it, so
    the second thread's store replaced the first's and everything already
    accepted into it became unreachable — silently, since publish() had
    reported those documents as written.
    """
    import threading

    class Broken:
        name = "mongodb"
        def append(self, documents):
            time.sleep(0.05)
            raise RuntimeError("Mongo disappeared under concurrent load")
        def query(self, criteria, limit):
            raise RuntimeError("Mongo disappeared under concurrent load")
        def ping(self):
            return False

    set_event_store(Broken())
    threads = [
        threading.Thread(target=publish, args=([{"_id": f"concurrent-{n}", "action": f"action_{n}", "created_at": n}],))
        for n in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    survived = {document["action"] for document in get_event_store().query({}, 10)}
    assert survived == {"action_0", "action_1"}


def test_store_failing_on_read_degrades_instead_of_erroring(store):
    """Writes degraded to the in-process buffer; reads did not, so a Mongo that
    died mid-life answered the audit search with a 500 — the opposite of what
    "audit querying gets worse, writes keep working" describes.
    """
    class BrokenOnRead:
        name = "mongodb"
        def append(self, documents):
            return len(documents)
        def query(self, criteria, limit):
            raise RuntimeError("Mongo disappeared after startup")
        def ping(self):
            return True

    set_event_store(BrokenOnRead())
    assert query_events({}, 10) == []
    assert get_event_store().name == "memory"


def test_two_suppliers_sharing_an_idempotency_key_must_not_overwrite_each_other(monkeypatch):
    """An idempotency key is only ever unique per client, but the inbox key was
    flat — so two suppliers both sending "001" landed on the same object, both
    were acknowledged, and only the later quote survived to be ingested.
    """
    written = {}

    class FakeS3:
        def put_object(self, Bucket, Key, Body, ContentType):
            written[Key] = json.loads(Body)

    monkeypatch.setattr(webhook, "SECRET", "shared-secret")
    monkeypatch.setattr(webhook, "BUCKET", "review-bucket")
    monkeypatch.setattr(webhook, "_client", lambda: FakeS3())

    for supplier_id, unit_cost in (("supplier-a", 10.0), ("supplier-b", 99.0)):
        body = json.dumps({"supplier_id": supplier_id, "sku": "BX-100", "unit_cost": unit_cost,
                           "currency": "USD", "idempotency_key": "001"})
        timestamp = str(int(time.time()))
        signature = "v1=" + hmac.new(b"shared-secret", f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
        response = webhook.handler({"body": body, "headers": {
            "x-stockroom-timestamp": timestamp, "x-stockroom-signature": signature}})
        assert response["statusCode"] == 202

    assert len(written) == 2, "both quotes must survive, not just the later one"
    assert {entry["unit_cost"] for entry in written.values()} == {10.0, 99.0}


def test_supplier_inbox_key_is_not_shaped_by_the_caller(monkeypatch):
    """Both halves of the object key come from the request body."""
    written = {}

    class FakeS3:
        def put_object(self, Bucket, Key, Body, ContentType):
            written[Key] = json.loads(Body)

    monkeypatch.setattr(webhook, "SECRET", "shared-secret")
    monkeypatch.setattr(webhook, "BUCKET", "review-bucket")
    monkeypatch.setattr(webhook, "_client", lambda: FakeS3())

    body = json.dumps({"supplier_id": "../../escape", "sku": "BX-100", "unit_cost": 5.0,
                       "currency": "USD", "idempotency_key": "a/b c" + "x" * 400})
    timestamp = str(int(time.time()))
    signature = "v1=" + hmac.new(b"shared-secret", f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
    webhook.handler({"body": body, "headers": {
        "x-stockroom-timestamp": timestamp, "x-stockroom-signature": signature}})

    # ".." carries no traversal meaning in S3 — keys are opaque strings. What
    # matters is that a caller cannot inject a separator and write into another
    # supplier's namespace, and cannot choose an unbounded key.
    key = next(iter(written))
    assert key.startswith(webhook.PREFIX)
    assert key.removeprefix(webhook.PREFIX).count("/") == 1
    assert " " not in key
    assert len(key) < 300


def test_agent_argument_colliding_with_a_handler_parameter_is_a_tool_error(db):
    """`handler(db, **arguments)` turned a model naming an argument `db` into a
    TypeError. run_agent only catches ToolError, so the run died — and with it
    `_record`, which writes the audit event for the run.
    """
    with pytest.raises(ToolError):
        dispatch(db, "item_detail", {"db": "supplied by the model"})


def test_agent_run_survives_a_model_that_names_an_argument_db(db, admin, store):
    client = ScriptedClient([
        LLMResponse(text="", tool_calls=[ToolCall(name="item_detail", arguments={"db": "nonsense"})]),
        LLMResponse(text="I could not read that item.", tool_calls=[]),
    ])
    run = run_agent(db, admin, client, "what should we reorder?")
    assert run.summary == "I could not read that item."
    assert run.steps[0]["ok"] is False
    assert db.scalars(select(AuditLog).where(AuditLog.action == "agent_replenishment_run")).all()


def test_the_validator_judges_the_version_it_was_triggered_for(monkeypatch):
    """Every S3 call used to read "current". A second upload landing mid-run
    therefore got sniffed under the first run's event, and the verdict was
    written against whichever version happened to be current — a judgement of
    one object's bytes attached to another's.
    """
    from tests.test_receipt_validator import ELF, PNG, FakeS3, event_for

    client = FakeS3(PNG, "image/png")
    # v1 is the good PNG the event names; v2 has already replaced it as current.
    client.versions = {"v1": (PNG, "image/png"), "v2": (ELF, "image/png")}
    monkeypatch.setattr(validator, "_client", lambda: client)

    result = validator.handler(event_for(version_id="v1"))

    assert result["results"][0]["validation"] == "passed"
    assert client.tags_by_version["v1"]["validation"] == "passed"
    assert "v2" not in client.tags_by_version, "the run must not tag a version it never read"


def test_an_approved_receipt_is_served_as_the_version_that_was_approved(db, employee, monkeypatch):
    """The dangerous replacement is not one that fails validation — it is one
    that passes. Swapping a different, equally valid PNG behind the same key
    got it tagged `passed` too, so the download served bytes nobody approved.
    Pinning the version means the replacement is an object this row does not
    point at.
    """
    from app import main, services

    presigned = {}

    class FakeS3:
        tags_by_version = {"v1": "passed", "v2": "passed"}
        def get_object_tagging(self, Bucket, Key, VersionId=None):
            verdict = self.tags_by_version.get(VersionId) if VersionId else "passed"
            return {"TagSet": [{"Key": "validation", "Value": verdict}]}
        def generate_presigned_url(self, operation, Params, ExpiresIn):
            presigned.update(Params)
            return "https://example.invalid/receipt"

    monkeypatch.setattr(services.get_settings(), "receipt_bucket_name", "review-bucket")
    monkeypatch.setattr(main.settings, "receipt_bucket_name", "review-bucket")
    monkeypatch.setattr(services.boto3, "client", lambda *args, **kwargs: FakeS3())
    monkeypatch.setattr(main.boto3, "client", lambda *args, **kwargs: FakeS3())

    key = f"receipts/{employee.id}/receipt.png"
    services.create_expense(db, employee, supplier="Store", quantity=1, amount=10, currency="CAD",
                            purpose="receipt test", receipt_key=key, receipt_version_id="v1")

    main.create_download_intent(key=key, db=db, user=employee)

    assert presigned["VersionId"] == "v1", "the approved version must be the one served"


def test_a_receipt_attached_before_versions_were_recorded_still_downloads(db, employee, monkeypatch):
    """Nothing was backfilled, so existing rows carry no version. They must
    keep reading as "current", which is what they were attached under.
    """
    from app import main, services

    presigned = {}

    class FakeS3:
        def get_object_tagging(self, Bucket, Key, VersionId=None):
            assert VersionId is None
            return {"TagSet": [{"Key": "validation", "Value": "passed"}]}
        def generate_presigned_url(self, operation, Params, ExpiresIn):
            presigned.update(Params)
            return "https://example.invalid/receipt"

    monkeypatch.setattr(services.get_settings(), "receipt_bucket_name", "review-bucket")
    monkeypatch.setattr(main.settings, "receipt_bucket_name", "review-bucket")
    monkeypatch.setattr(services.boto3, "client", lambda *args, **kwargs: FakeS3())
    monkeypatch.setattr(main.boto3, "client", lambda *args, **kwargs: FakeS3())

    key = f"receipts/{employee.id}/legacy.png"
    services.create_expense(db, employee, supplier="Store", quantity=1, amount=10, currency="CAD",
                            purpose="legacy receipt", receipt_key=key)

    main.create_download_intent(key=key, db=db, user=employee)

    assert "VersionId" not in presigned


def test_the_migration_chain_applies_to_an_empty_database(tmp_path, monkeypatch):
    """Nothing else here runs the migrations: conftest builds the schema from
    the ORM, so the chain only ever ran in CI's docker-compose job. The initial
    migration creates its tables from `Base.metadata` at HEAD, so every column
    added to a model later is already present by the time that column's own
    migration runs — which fails a fresh `alembic upgrade head` outright.
    """
    from alembic import command
    from alembic.config import Config

    from app.config import get_settings

    # env.py resolves the URL from settings and overrides whatever the caller
    # passes in, so the environment is the only way to aim it at a scratch db.
    url = f"sqlite:///{tmp_path / 'chain.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        get_settings.cache_clear()

    inspector = sa.inspect(sa.create_engine(url))
    for table in ("purchases", "expenses"):
        assert "receipt_version_id" in {column["name"] for column in inspector.get_columns(table)}
