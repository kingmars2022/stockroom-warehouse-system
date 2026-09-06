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

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cache import Cache, InMemoryBackend, set_cache
from app.db import Base
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
