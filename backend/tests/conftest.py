import os

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["APP_ENV"] = "test"
os.environ["REDIS_URL"] = ""
# Pin the AWS-facing settings to empty so a developer's local .env can never
# leak real Cognito or S3 identifiers into a test run.
os.environ["COGNITO_REGION"] = ""
os.environ["COGNITO_USER_POOL_ID"] = ""
os.environ["COGNITO_APP_CLIENT_ID"] = ""
os.environ["RECEIPT_BUCKET_NAME"] = ""
# Same reasoning: the suite runs against the in-memory event store, never a
# real MongoDB. test_audit_events.py opts into a live one via MONGO_TEST_URL.
os.environ["MONGO_URL"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.audit_events import InMemoryEventStore, set_event_store
from app.auth import get_current_user
from app.cache import Cache, InMemoryBackend, set_cache
from app.db import Base, get_db
from app.main import app
from app.models import Item, Role, Supplier, SupplierStatus, User


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def cache():
    """Give every test a fresh in-memory cache so hit/miss counters and stored
    entries never leak between tests."""
    instance = Cache(InMemoryBackend(), ttl_seconds=60)
    set_cache(instance)
    yield instance
    set_cache(None)


@pytest.fixture(autouse=True)
def store():
    """Same reasoning as the cache: the event store is a process-wide singleton,
    so without this one test's audit events show up in the next one's query."""
    instance = InMemoryEventStore()
    set_event_store(instance)
    yield instance
    set_event_store(None)


def user(db, role: Role, email: str) -> User:
    account = User(cognito_sub=f"sub-{email}", email=email, name=email.split("@")[0], role=role)
    db.add(account)
    db.commit()
    return account


@pytest.fixture()
def admin(db):
    return user(db, Role.admin, "admin@stockroom.test")


@pytest.fixture()
def supervisor(db):
    return user(db, Role.supervisor, "supervisor@stockroom.test")


@pytest.fixture()
def employee(db):
    return user(db, Role.employee, "employee@stockroom.test")


@pytest.fixture()
def item(db):
    record = Item(sku="BX-100", name="Courier box", quantity_on_hand=50, minimum_quantity=10, unit="pcs")
    db.add(record)
    db.commit()
    return record


@pytest.fixture()
def supplier(db):
    record = Supplier(name="Parcel Supply Co", lead_days=3, rating=4.2, status=SupplierStatus.preferred)
    db.add(record)
    db.commit()
    return record


class Api:
    """Thin wrapper that lets a test switch the acting user mid-request."""

    def __init__(self, client: TestClient, state: dict):
        self._client = client
        self._state = state

    def act_as(self, user):
        self._state["user"] = user
        return self

    def get(self, *args, **kwargs):
        return self._client.get(*args, **kwargs)

    def post(self, *args, **kwargs):
        return self._client.post(*args, **kwargs)

    def patch(self, *args, **kwargs):
        return self._client.patch(*args, **kwargs)


@pytest.fixture()
def api(db, employee):
    state = {"user": employee}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    with TestClient(app) as client:
        yield Api(client, state)
    app.dependency_overrides.clear()
