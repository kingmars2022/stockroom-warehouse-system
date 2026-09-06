import os

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["APP_ENV"] = "test"

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Role, User


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
