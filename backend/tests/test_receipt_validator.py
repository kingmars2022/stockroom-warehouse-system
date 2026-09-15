"""The S3 upload validator, and the API gate that acts on its verdict.

The point of this Lambda is that the API cannot check an upload it never sees:
presign validates what the client *declares*, then the browser uploads
straight to S3. These tests cover the case that motivates the whole thing — a
file whose real signature disagrees with the declared content type.
"""

import pytest
from fastapi import HTTPException

from app.services import assert_receipt_validated
from lambdas.receipt_validator.handler import FAILED, PASSED, detect_type, handler, process_record, verdict

PDF = b"%PDF-1.7\n%\xc7\xec\x8f\xa2"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x02\x00\x00\x01"
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 \x18\x00"
ELF = b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00"


class FakeS3:
    def __init__(self, body: bytes, content_type: str, size: int | None = None, missing: bool = False):
        self.body = body
        self.content_type = content_type
        self.size = len(body) if size is None else size
        self.missing = missing
        self.tags: dict[str, str] = {}

    def head_object(self, Bucket, Key):
        if self.missing:
            raise RuntimeError("NoSuchKey")
        return {"ContentType": self.content_type, "ContentLength": self.size}

    def get_object(self, Bucket, Key, Range=None):
        return {"Body": _Body(self.body)}

    def put_object_tagging(self, Bucket, Key, Tagging):
        self.tags = {tag["Key"]: tag["Value"] for tag in Tagging["TagSet"]}

    def get_object_tagging(self, Bucket, Key):
        if self.missing:
            raise RuntimeError("NoSuchKey")
        return {"TagSet": [{"Key": key, "Value": value} for key, value in self.tags.items()]}


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


def event_for(key: str = "receipts/u1/abc-receipt.png", bucket: str = "b") -> dict:
    return {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}


# --------------------------------------------------------------------------
# Signature detection
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("head", "expected"),
    [(PDF, "application/pdf"), (PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp"), (ELF, None), (b"", None)],
)
def test_detect_type_reads_the_leading_bytes(head, expected):
    assert detect_type(head) == expected


def test_riff_that_is_not_webp_is_not_accepted():
    assert detect_type(b"RIFF\x24\x00\x00\x00WAVEfmt ") is None


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------

def test_a_matching_file_passes():
    assert verdict(PNG, "image/png", 1024) == (PASSED, "image/png", "ok")


def test_an_executable_declared_as_an_image_is_rejected():
    """The case the API cannot catch on its own."""
    validation, detected, reason = verdict(ELF, "image/png", 1024)

    assert validation == FAILED
    assert detected == "unknown"
    assert reason == "unrecognised-file-signature"


def test_a_real_image_declared_as_the_wrong_image_type_is_rejected():
    validation, detected, reason = verdict(JPEG, "image/png", 1024)

    assert validation == FAILED
    assert detected == "image/jpeg"
    assert reason == "declared-type-mismatch"


def test_an_oversized_file_is_rejected_even_when_the_type_is_right():
    validation, detected, reason = verdict(PDF, "application/pdf", 50 * 1024 * 1024)

    assert validation == FAILED
    assert reason == "exceeds-max-size"


def test_an_object_with_no_declared_type_is_judged_on_its_signature_alone():
    assert verdict(PDF, "", 1024) == (PASSED, "application/pdf", "ok")


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------

def test_process_record_tags_the_object_with_its_verdict():
    client = FakeS3(PNG, "image/png")

    result = process_record("b", "receipts/u1/x.png", client)

    assert result["validation"] == PASSED
    assert client.tags == {"validation": "passed", "detected-type": "image/png", "reason": "ok"}


def test_process_record_tags_a_failure_with_the_reason():
    client = FakeS3(ELF, "application/pdf")

    process_record("b", "receipts/u1/x.pdf", client)

    assert client.tags["validation"] == "failed"
    assert client.tags["reason"] == "unrecognised-file-signature"


def test_handler_url_decodes_the_key(monkeypatch):
    client = FakeS3(PNG, "image/png")
    monkeypatch.setattr("lambdas.receipt_validator.handler._client", lambda: client)

    result = handler(event_for("receipts/u1/my+receipt%20scan.png"))

    assert result["results"][0]["key"] == "receipts/u1/my receipt scan.png"


def test_an_infrastructure_failure_is_raised_so_lambda_retries(monkeypatch):
    """Lambda only retries an async invocation when the function raises.

    Returning normally here would leave the object untagged forever, and the
    API answering 409 on that receipt indefinitely — so a transient S3 error
    must not be reported as a successful run.
    """
    monkeypatch.setattr("lambdas.receipt_validator.handler._client", lambda: FakeS3(PNG, "image/png", missing=True))

    with pytest.raises(RuntimeError):
        handler(event_for())


def test_a_file_that_fails_validation_is_a_result_not_an_error(monkeypatch):
    """The other half of that distinction: a bad file is tagged and the run succeeds."""
    client = FakeS3(ELF, "image/png")
    monkeypatch.setattr("lambdas.receipt_validator.handler._client", lambda: client)

    result = handler(event_for())

    assert result["processed"] == 1
    assert client.tags["validation"] == "failed"


def test_handler_on_an_empty_event(monkeypatch):
    monkeypatch.setattr("lambdas.receipt_validator.handler._client", lambda: FakeS3(PNG, "image/png"))

    assert handler({}) == {"processed": 0, "results": []}


# --------------------------------------------------------------------------
# The API gate that gives the verdict teeth
# --------------------------------------------------------------------------

def configure_bucket(monkeypatch, client):
    from app import services

    monkeypatch.setattr(services.get_settings(), "receipt_bucket_name", "receipts-bucket", raising=False)
    monkeypatch.setattr(services.boto3, "client", lambda *args, **kwargs: client)


def test_no_bucket_configured_leaves_the_gate_open():
    """Local demo and the test suite have no bucket; the gate must stand aside."""
    assert assert_receipt_validated("receipts/u1/x.png") is None


def test_a_passed_receipt_can_be_attached(monkeypatch):
    client = FakeS3(PNG, "image/png")
    client.tags = {"validation": "passed"}
    configure_bucket(monkeypatch, client)

    assert assert_receipt_validated("receipts/u1/x.png") is None


def test_a_failed_receipt_cannot_be_attached(monkeypatch):
    client = FakeS3(ELF, "image/png")
    client.tags = {"validation": "failed", "reason": "unrecognised-file-signature"}
    configure_bucket(monkeypatch, client)

    with pytest.raises(HTTPException) as raised:
        assert_receipt_validated("receipts/u1/x.png")
    assert raised.value.status_code == 422


def test_an_unscanned_receipt_asks_the_caller_to_retry(monkeypatch):
    """Validation is asynchronous, so racing the upload is a retry, not a rejection."""
    client = FakeS3(PNG, "image/png")
    configure_bucket(monkeypatch, client)

    with pytest.raises(HTTPException) as raised:
        assert_receipt_validated("receipts/u1/x.png")
    assert raised.value.status_code == 409


def test_a_missing_object_is_reported_as_not_found(monkeypatch):
    configure_bucket(monkeypatch, FakeS3(PNG, "image/png", missing=True))

    with pytest.raises(HTTPException) as raised:
        assert_receipt_validated("receipts/u1/gone.png")
    assert raised.value.status_code == 404
