"""What the agent is allowed to do.

Every tool here is a read, except the last one — and the last one does not
write either. `propose_purchase` records a proposal and stops; a person still
has to place the order through the ordinary authenticated endpoint.

That line is the whole safety design. The agent reasons over inventory and
supplier data and drafts a recommendation, but it cannot move stock, cannot
spend money, and cannot change a record. The blast radius of the model
hallucinating is a bad suggestion that someone declines.

The numbers themselves are not the model's either. `replenishment_needs` calls
the same `replenishment_recommendations` the console uses — 90 days of outbound
demand, days of cover, supplier ranking by price, lead time and rating. The
agent chooses what to look at and how to explain it; arithmetic stays in code
where it can be tested.
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item, Purchase, Supplier
from ..services import replenishment_recommendations

# JSON Schema for each tool, in the shape both providers accept.
TOOL_SCHEMAS: list[dict] = [
    {
        "name": "replenishment_needs",
        "description": "List items that need reordering, with days of cover, suggested quantity, and ranked suppliers. Start here.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many items to return, most urgent first. Default 10."}
            },
        },
    },
    {
        "name": "item_detail",
        "description": "Stock level, unit, category and recent purchase history for one SKU.",
        "parameters": {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"],
        },
    },
    {
        "name": "supplier_options",
        "description": "Suppliers that have previously supplied a SKU, with last price, lead time and rating.",
        "parameters": {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"],
        },
    },
    {
        "name": "propose_purchase",
        "description": "Record a purchase proposal for human approval. This does NOT place an order and does not change stock.",
        "parameters": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "supplier_name": {"type": "string"},
                "quantity": {"type": "integer"},
                "reason": {"type": "string", "description": "Why this quantity from this supplier, in one sentence."},
            },
            "required": ["sku", "supplier_name", "quantity", "reason"],
        },
    },
]

READ_ONLY = {"replenishment_needs", "item_detail", "supplier_options"}
TERMINAL = "propose_purchase"


class ToolError(Exception):
    """A tool was called with arguments that do not resolve. Fed back to the model."""


def _replenishment_needs(db: Session, limit: int = 10, **_) -> Any:
    rows = replenishment_recommendations(db)
    trimmed = []
    for row in rows[: max(1, min(int(limit or 10), 25))]:
        recommended = row["recommended_supplier"]
        trimmed.append({
            "sku": row["sku"], "item_name": row["item_name"], "quantity_on_hand": row["quantity_on_hand"],
            "daily_usage": row["daily_usage"], "days_of_cover": row["days_of_cover"],
            "suggested_quantity": row["suggested_quantity"],
            "recommended_supplier": recommended["supplier_name"] if recommended else None,
            "recommended_unit_cost": recommended["unit_cost"] if recommended else None,
        })
    return trimmed


def _item_detail(db: Session, sku: str = "", **_) -> Any:
    item = db.scalar(select(Item).where(Item.sku == sku))
    if item is None:
        raise ToolError(f"No item with sku {sku!r}. Call replenishment_needs to see valid SKUs.")
    purchases = db.scalars(select(Purchase).where(Purchase.item_id == item.id).order_by(Purchase.created_at.desc()).limit(5)).all()
    return {
        "sku": item.sku, "name": item.name, "category": item.category, "unit": item.unit,
        "quantity_on_hand": item.quantity_on_hand, "minimum_quantity": item.minimum_quantity,
        "recent_purchases": [
            {"quantity": purchase.quantity, "unit_cost": float(purchase.unit_cost), "currency": purchase.currency}
            for purchase in purchases
        ],
    }


def _supplier_options(db: Session, sku: str = "", **_) -> Any:
    item = db.scalar(select(Item).where(Item.sku == sku))
    if item is None:
        raise ToolError(f"No item with sku {sku!r}.")
    rows = [row for row in replenishment_recommendations(db) if row["sku"] == sku]
    return rows[0]["alternatives"] if rows else []


def _propose_purchase(db: Session, sku: str = "", supplier_name: str = "", quantity: int = 0, reason: str = "", **_) -> Any:
    """Validate the proposal against reality, then return it. Nothing is written."""
    item = db.scalar(select(Item).where(Item.sku == sku))
    if item is None:
        raise ToolError(f"No item with sku {sku!r}, so it cannot be ordered.")
    supplier = db.scalar(select(Supplier).where(Supplier.name == supplier_name))
    if supplier is None:
        raise ToolError(f"No supplier named {supplier_name!r}. Call supplier_options to see who supplies this SKU.")
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise ToolError("quantity must be a whole number")
    if quantity <= 0:
        raise ToolError("quantity must be greater than zero")

    return {
        "proposal": {
            "sku": item.sku, "item_name": item.name, "item_id": str(item.id),
            "supplier_name": supplier.name, "supplier_id": str(supplier.id),
            "quantity": quantity, "unit": item.unit, "reason": reason,
            "requires_human_approval": True,
        }
    }


HANDLERS: dict[str, Callable[..., Any]] = {
    "replenishment_needs": _replenishment_needs,
    "item_detail": _item_detail,
    "supplier_options": _supplier_options,
    "propose_purchase": _propose_purchase,
}


def dispatch(db: Session, name: str, arguments: dict) -> Any:
    """Run one tool call. An unknown name is the model's mistake, not a crash."""
    handler = HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"No tool named {name!r}. Available: {', '.join(sorted(HANDLERS))}.")
    if not isinstance(arguments, dict):
        raise ToolError("tool arguments must be an object")
    return handler(db, **arguments)
