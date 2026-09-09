"""Seed a disposable database with N employees and a couple of high-stock
items for load testing. Never point this at a real database -- it inserts
plain, non-Cognito-linked users and large stock quantities.

Usage:
    DATABASE_URL=postgresql+psycopg://stockroom:stockroom@localhost:5432/stockroom_loadtest \
        python loadtest/seed.py --users 50
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import sessionmaker

from app.db import engine
from app.models import Item, Role, Supplier, SupplierStatus, User


def seed(user_count: int, ids_path: Path) -> None:
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()

    users = [User(cognito_sub=f"loadtest-sub-{i}", email=f"loadtest{i}@stockroom.test", name=f"Load Test Employee {i}", role=Role.employee) for i in range(user_count)]
    db.add_all(users)

    items = [
        Item(sku="LT-001", name="Load test widget A", category="Electronics", location="A-01", quantity_on_hand=100_000, minimum_quantity=100, unit="pcs"),
        Item(sku="LT-002", name="Load test widget B", category="Packaging", location="B-01", quantity_on_hand=100_000, minimum_quantity=100, unit="pcs"),
    ]
    db.add_all(items)
    db.add(Supplier(name="Load Test Supply Co", contact="", lead_days=3, rating=4.5, status=SupplierStatus.preferred))
    db.commit()

    ids_path.write_text(",".join(str(u.id) for u in users) + "\n" + ",".join(str(i.id) for i in items) + "\n")
    print(f"seeded {len(users)} users and {len(items)} items -> {ids_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=50)
    parser.add_argument("--ids-out", default="loadtest/.ids.txt")
    args = parser.parse_args()
    seed(args.users, Path(args.ids_out))
