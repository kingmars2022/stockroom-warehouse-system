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

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.audit_events import get_event_store, publish, set_event_store
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
