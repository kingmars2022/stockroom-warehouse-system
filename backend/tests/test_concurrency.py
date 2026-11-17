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


# --------------------------------------------------------------------------
# Check-then-insert races
#
# Three handlers look for a row and insert it when there is none. Those are two
# statements, so requests that arrive together can all pass the look and all
# try the insert; every one after the first then meets a unique index. On
# SQLite the suite has one connection and cannot produce that, so these run
# here, with real transactions racing.
# --------------------------------------------------------------------------

RACERS = 12


def _race(Session, attempt) -> list:
    """Run `attempt(session)` on RACERS threads released together; collect what
    each returned or raised."""
    outcomes: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(RACERS)

    def run() -> None:
        session = Session()
        barrier.wait()
        try:
            outcome = attempt(session)
        except Exception as error:  # the assertion is about which errors these are
            outcome = error
        finally:
            session.close()
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=run) for _ in range(RACERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return outcomes


@pytest.fixture()
def postgres():
    if not POSTGRES_TEST_URL:
        pytest.skip("requires a real PostgreSQL instance (set POSTGRES_TEST_URL)")
    engine = create_engine(POSTGRES_TEST_URL, pool_size=RACERS + 2)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_the_first_requests_after_sign_in_create_one_account_between_them(postgres, monkeypatch):
    """A browser fires several requests the moment sign-in completes. Each
    found no row for the subject and inserted one, and all but the first came
    back as a 500 on `users.cognito_sub`."""
    import app.auth as auth

    subject = f"sub-{uuid.uuid4()}"
    claims = {"sub": subject, "email": f"{subject}@stockroom.test", "name": "New Starter"}
    monkeypatch.setattr(auth, "decode_cognito_token", lambda token: claims)

    class Token:
        credentials = "token"

    outcomes = _race(postgres, lambda session: auth.get_current_user(credentials=Token(), db=session).cognito_sub)

    assert outcomes == [subject] * RACERS, [outcome for outcome in outcomes if outcome != subject]
    verify = postgres()
    assert verify.query(User).filter(User.cognito_sub == subject).count() == 1
    verify.close()


def test_racing_to_create_one_sku_yields_one_item_and_conflicts(postgres):
    from fastapi import HTTPException

    import app.main as main
    from app.schemas import ItemCreate

    setup = postgres()
    admin = User(cognito_sub=f"sub-{uuid.uuid4()}", email=f"{uuid.uuid4()}@stockroom.test", name="Admin", role=Role.admin)
    setup.add(admin)
    setup.commit()
    admin_id = admin.id
    setup.close()
    sku = f"RACE-{uuid.uuid4().hex[:8]}"

    outcomes = _race(postgres, lambda session: main.create_item(ItemCreate(sku=sku, name="Raced item"), db=session, user=session.get(User, admin_id)).sku)

    created = [outcome for outcome in outcomes if outcome == sku]
    refused = [outcome for outcome in outcomes if isinstance(outcome, HTTPException) and outcome.status_code == 409]
    assert len(created) == 1, outcomes
    assert len(refused) == RACERS - 1, [outcome for outcome in outcomes if outcome not in created and outcome not in refused]


def test_racing_to_create_one_supplier_yields_one_supplier_and_conflicts(postgres):
    from fastapi import HTTPException

    import app.main as main
    from app.schemas import SupplierCreate

    setup = postgres()
    admin = User(cognito_sub=f"sub-{uuid.uuid4()}", email=f"{uuid.uuid4()}@stockroom.test", name="Admin", role=Role.admin)
    setup.add(admin)
    setup.commit()
    admin_id = admin.id
    setup.close()
    name = f"Raced Supply {uuid.uuid4().hex[:8]}"

    outcomes = _race(postgres, lambda session: main.create_supplier(SupplierCreate(name=name), db=session, user=session.get(User, admin_id)).name)

    created = [outcome for outcome in outcomes if outcome == name]
    refused = [outcome for outcome in outcomes if isinstance(outcome, HTTPException) and outcome.status_code == 409]
    assert len(created) == 1, outcomes
    assert len(refused) == RACERS - 1, [outcome for outcome in outcomes if outcome not in created and outcome not in refused]


def test_a_reimbursement_for_a_missing_item_is_a_404_where_the_key_is_enforced(postgres):
    """The SQLite half of the suite cannot show this one failing: it does not
    enforce foreign keys, so the dangling reference was simply stored. Here the
    insert is refused by the database, which used to surface as a 500."""
    from fastapi import HTTPException

    from app.services import create_expense

    session = postgres()
    actor = User(cognito_sub=f"sub-{uuid.uuid4()}", email=f"{uuid.uuid4()}@stockroom.test", name="Claimant", role=Role.employee)
    session.add(actor)
    session.commit()

    with pytest.raises(HTTPException) as refused:
        create_expense(session, actor, item_id=uuid.uuid4(), supplier="Corner Store", quantity=1, amount=9.5, currency="CAD", purpose="Replacement tape", receipt_key=None)

    assert refused.value.status_code == 404
    session.close()
