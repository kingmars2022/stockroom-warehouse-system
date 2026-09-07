"""Row-level locking under real concurrency.

conftest.py hardcodes DATABASE_URL to SQLite for every other test in this
suite (see the comment there), which keeps the suite fast and isolated but
means the SELECT ... FOR UPDATE lock in record_movement is never actually
exercised against real concurrent access -- SQLite has no row-level locking
to exercise. This test proves it holds up on PostgreSQL, the database this
service actually runs on: 20 threads race to issue stock for one item with
only 10 units on hand.

Requires a real, disposable PostgreSQL database. Set POSTGRES_TEST_URL to
run it, e.g.:

    POSTGRES_TEST_URL=postgresql+psycopg://stockroom:stockroom@localhost:5432/stockroom_test \
        pytest tests/test_concurrency.py --no-cov
"""

import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Item, MovementKind, Role, User
from app.services import record_movement

POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL")

CONCURRENT_REQUESTS = 20
STARTING_STOCK = 10


@pytest.mark.skipif(not POSTGRES_TEST_URL, reason="requires a real PostgreSQL instance for row-level locking (set POSTGRES_TEST_URL)")
def test_concurrent_outbound_issues_never_oversell_a_single_items_stock():
    engine = create_engine(POSTGRES_TEST_URL)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    try:
        setup = Session()
        item = Item(sku=f"CONC-{uuid.uuid4().hex[:8]}", name="Contested item", quantity_on_hand=STARTING_STOCK, minimum_quantity=0)
        actor = User(cognito_sub=f"sub-{uuid.uuid4()}", email=f"{uuid.uuid4()}@stockroom.test", name="Concurrency Tester", role=Role.employee)
        setup.add_all([item, actor])
        setup.commit()
        item_id, actor_id = item.id, actor.id
        setup.close()

        results: list[str] = []
        results_lock = threading.Lock()
        start_barrier = threading.Barrier(CONCURRENT_REQUESTS)

        def issue_one() -> None:
            session = Session()
            start_barrier.wait()  # maximize the number of requests racing for the same row
            try:
                current_actor = session.get(User, actor_id)
                record_movement(session, current_actor, item_id, MovementKind.outbound, 1, "Dispatch", "concurrent run")
                outcome = "issued"
            except Exception:
                outcome = "rejected"
            finally:
                session.close()
            with results_lock:
                results.append(outcome)

        threads = [threading.Thread(target=issue_one) for _ in range(CONCURRENT_REQUESTS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        verify = Session()
        final_item = verify.get(Item, item_id)

        assert results.count("issued") == STARTING_STOCK, "exactly the starting stock should succeed, no more"
        assert results.count("rejected") == CONCURRENT_REQUESTS - STARTING_STOCK
        assert final_item.quantity_on_hand == 0, "the row lock must prevent the balance from going negative"
        verify.close()
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
