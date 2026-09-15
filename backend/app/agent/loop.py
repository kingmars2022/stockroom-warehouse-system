"""The agent loop: ask, run tools, feed results back, stop.

Three limits are enforced here rather than requested in the prompt, because a
prompt is a suggestion and a loop bound is not:

* **Iteration cap.** A model that never stops calling tools stops anyway.
* **Tool allowlist.** Anything the model names that is not in the schema list
  is refused and reported back to it, so a hallucinated tool costs one turn
  instead of raising.
* **No writes.** Every tool is a read except `propose_purchase`, which
  validates a proposal and returns it. Approving it is a separate, ordinary,
  authenticated call made by a person.

The whole run — every tool call, its arguments, and the proposals produced —
is written to the audit trail as one event, so an agent-suggested order is as
reviewable afterwards as one a person typed in.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..models import User
from ..services import write_audit
from .llm import LLMClient, LLMResponse
from .tools import TERMINAL, TOOL_SCHEMAS, ToolError, dispatch

MAX_ITERATIONS = 8

SYSTEM_PROMPT = """You are a procurement assistant for a warehouse.

Your job: find what needs reordering and propose specific purchases for a human to approve.

How to work:
- Start with replenishment_needs to see what is short.
- Use item_detail and supplier_options when you need to justify a choice.
- Call propose_purchase for each item that genuinely needs ordering.
- When you have proposed everything worth proposing, stop and summarise in two or three sentences.

Rules:
- Only propose items that replenishment_needs actually flagged. Do not invent SKUs or suppliers.
- The quantities and supplier rankings in the tool results are computed from real purchase and usage history. Use them; do not recompute them yourself.
- If nothing needs ordering, say so and propose nothing.
- You cannot place orders. Every proposal goes to a human."""


@dataclass
class AgentRun:
    proposals: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    summary: str = ""
    stopped_because: str = "completed"
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "summary": self.summary,
            "proposals": self.proposals,
            "steps": self.steps,
            "stopped_because": self.stopped_because,
            "duration_ms": self.duration_ms,
        }


def run_agent(db: Session, actor: User, client: LLMClient, instruction: str) -> AgentRun:
    started = time.monotonic()
    run = AgentRun()
    messages: list[dict] = [{"role": "user", "content": instruction}]
    allowed = {schema["name"] for schema in TOOL_SCHEMAS}

    for iteration in range(MAX_ITERATIONS):
        response: LLMResponse = client.complete(SYSTEM_PROMPT, messages, TOOL_SCHEMAS)

        if not response.tool_calls:
            run.summary = response.text.strip()
            break

        messages.append({"role": "assistant", "content": response.text or "(tool calls)"})

        for call in response.tool_calls:
            if call.name not in allowed:
                result: Any = {"error": f"No tool named {call.name!r}."}
                ok = False
            else:
                try:
                    result = dispatch(db, call.name, call.arguments)
                    ok = True
                except ToolError as error:
                    result = {"error": str(error)}
                    ok = False

            run.steps.append({"iteration": iteration, "tool": call.name, "arguments": call.arguments, "ok": ok})
            if ok and call.name == TERMINAL:
                run.proposals.append(result["proposal"])

            messages.append({"role": "tool", "name": call.name, "content": json.dumps(result, default=str)})
    else:
        run.stopped_because = "iteration_limit"

    run.duration_ms = int((time.monotonic() - started) * 1000)
    _record(db, actor, client, instruction, run)
    return run


def _record(db: Session, actor: User, client: LLMClient, instruction: str, run: AgentRun) -> None:
    write_audit(
        db, actor, "agent_replenishment_run", "agent", "replenishment",
        f"Agent proposed {len(run.proposals)} purchase(s) in {len(run.steps)} tool call(s)",
        payload={
            "provider": client.name,
            "instruction": instruction,
            "proposal_count": len(run.proposals),
            "tool_call_count": len(run.steps),
            "tools_used": sorted({step["tool"] for step in run.steps}),
            "failed_tool_calls": sum(0 if step["ok"] else 1 for step in run.steps),
            "stopped_because": run.stopped_because,
            "duration_ms": run.duration_ms,
            "proposed_skus": [proposal["sku"] for proposal in run.proposals],
        },
    )
    db.commit()
