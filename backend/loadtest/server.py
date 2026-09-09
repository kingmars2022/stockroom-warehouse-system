"""Boots the real app (app.main:app) unmodified, against whatever
DATABASE_URL/REDIS_URL are set in the environment, with exactly one thing
swapped: real Cognito JWT verification is replaced by "trust the
X-Test-User-Id header", because a load test has no real Cognito user pool
to mint tokens from. Every route, every database query, every cache call,
every business rule in services.py runs unmodified -- this measures the
real application, not a stand-in.

Never point this at a production database. Run it only against the
disposable database that loadtest/seed.py populated.

Usage:
    DATABASE_URL=postgresql+psycopg://stockroom:stockroom@localhost:5432/stockroom_loadtest \
    REDIS_URL=redis://localhost:6379/0 \
        uvicorn loadtest.server:app --port 8000
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uuid

from fastapi import Request

from app.auth import get_current_user
from app.db import SessionLocal
from app.main import app
from app.models import User


def fake_current_user(request: Request) -> User:
    db = SessionLocal()
    try:
        user = db.get(User, uuid.UUID(request.headers["x-test-user-id"]))
        db.expunge(user)
        return user
    finally:
        db.close()


app.dependency_overrides[get_current_user] = fake_current_user
