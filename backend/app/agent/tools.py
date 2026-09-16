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

import inspect
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item, Purchase, Supplier, SupplierStatus
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


MAX_PROPOSAL_QUANTITY = 100_000


def _as_int(value: Any, field: str, default: int | None = None) -> int:
    """Coerce a model-supplied number, reporting a bad one back rather than raising.

    Tool arguments come from a language model, so `{"limit": "many"}` is an
    ordinary occurrence. It has to reach the model as a tool error it can
    correct, not as a ValueError that ends the whole run.
    """
    if value in (None, "") and default is not None:
        return default
    if isinstance(value, bool):
        raise ToolError(f"{field} must be a whole number")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{field} must be a whole number, got {value!r}")


def _replenishment_needs(db: Session, limit: Any = 10, **_) -> Any:
    rows = replenishment_recommendations(db)
    trimmed = []
    for row in rows[: max(1, min(_as_int(limit, "limit", default=10), 25))]:
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


def _propose_purchase(db: Session, sku: str = "", supplier_name: str = "", quantity: Any = 0, reason: str = "", **_) -> Any:
    """Check the proposal against the engine's own findings, then return it.

    Existence checks are not enough. The model can name a real SKU that is not
    short, a real supplier that is paused or has never supplied it, or a
    quantity with an extra six zeros — and the description of this system says
    proposals are grounded in the replenishment engine. So the engine's current
    result is the authority on all three. Nothing is written either way.
    """
    item = db.scalar(select(Item).where(Item.sku == sku))
    if item is None:
        raise ToolError(f"No item with sku {sku!r}, so it cannot be ordered.")
    supplier = db.scalar(select(Supplier).where(Supplier.name == supplier_name))
    if supplier is None:
        raise ToolError(f"No supplier named {supplier_name!r}. Call supplier_options to see who supplies this SKU.")
    if supplier.status is SupplierStatus.paused:
        raise ToolError(f"{supplier.name} is paused and cannot be ordered from.")

    quantity = _as_int(quantity, "quantity")
    if quantity <= 0:
        raise ToolError("quantity must be greater than zero")
    if quantity > MAX_PROPOSAL_QUANTITY:
        raise ToolError(f"quantity {quantity} exceeds the {MAX_PROPOSAL_QUANTITY} cap for a single proposal.")

    flagged = next((row for row in replenishment_recommendations(db) if row["sku"] == sku), None)
    if flagged is None:
        raise ToolError(f"{sku} is not short — replenishment_needs did not flag it, so do not order it.")
    if supplier.id not in {option["supplier_id"] for option in flagged["alternatives"]}:
        raise ToolError(f"{supplier.name} has never supplied {sku}. Call supplier_options for the ones that have.")

    suggested = flagged["suggested_quantity"]
    return {
        "proposal": {
            "sku": item.sku, "item_name": item.name, "item_id": str(item.id),
            "supplier_name": supplier.name, "supplier_id": str(supplier.id),
            "quantity": quantity, "unit": item.unit, "reason": reason,
            # Both numbers are shown so a reviewer can see where the model
            # departed from the engine, instead of having to recompute it.
            "engine_suggested_quantity": suggested,
            "differs_from_engine": quantity != suggested,
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
    # Bound before the call, so a model that names an argument after one of
    # this module's own parameters — `db` is the one that collides — comes back
    # as a tool error it can correct. Calling straight through made that a
    # TypeError, which `run_agent` does not catch: the run died and took its
    # own audit record with it. A TypeError from inside a handler still
    # surfaces, because that one is a real defect.
    try:
        bound = inspect.signature(handler).bind(db, **arguments)
    except TypeError as error:
        raise ToolError(f"{name} does not take those arguments: {error}")
    return handler(*bound.args, **bound.kwargs)
