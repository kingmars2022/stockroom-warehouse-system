"""The replenishment agent.

Every test here drives the real loop, the real tools, and the real database
with a scripted model. No API key, no network, no cost, and the same result
every run — which is the point of putting the provider behind an interface.

What is worth protecting is not "the model said something sensible" (it is not
our code and cannot be asserted on) but the guarantees around it: it cannot
write, it cannot loop forever, a hallucinated tool or SKU costs one turn
instead of raising, and the whole run lands in the audit trail.
"""

import pytest

from app.agent.llm import GeminiClient, LLMResponse, OllamaClient, ScriptedClient, ToolCall, _as_dict, build_client
from app.agent.loop import MAX_ITERATIONS, run_agent
from app.agent.tools import ToolError, dispatch
from app.models import Purchase


def call(name: str, **arguments) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(name=name, arguments=arguments)])


def says(text: str) -> LLMResponse:
    return LLMResponse(text=text)


@pytest.fixture()
def stocked(db, admin, item, supplier):
    """An item that is actually short, with purchase history behind it.

    The engine skips anything that does not need reordering, so the default
    fixture (50 on hand against a minimum of 10) produces no recommendation
    and gives the agent nothing to work with.
    """
    item.quantity_on_hand = 4
    db.add(Purchase(item_id=item.id, supplier_id=supplier.id, received_by_id=admin.id, quantity=5, unit_cost=10.0, currency="CAD", invoice_number="SEED"))
    db.commit()
    return item


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

def test_a_proposal_is_returned_and_nothing_is_written(db, admin, stocked, supplier, store):
    before = stocked.quantity_on_hand
    client = ScriptedClient([
        call("replenishment_needs", limit=5),
        call("propose_purchase", sku=stocked.sku, supplier_name=supplier.name, quantity=20, reason="Two weeks of cover left."),
        says("Proposed one order."),
    ])

    run = run_agent(db, admin, client, "What should we reorder?")

    assert run.summary == "Proposed one order."
    assert len(run.proposals) == 1
    assert run.proposals[0]["sku"] == stocked.sku
    assert run.proposals[0]["requires_human_approval"] is True
    # The agent proposed an order; it must not have placed one.
    db.refresh(stocked)
    assert stocked.quantity_on_hand == before
    assert db.query(Purchase).count() == 1  # only the seeded one


def test_the_loop_stops_at_the_iteration_cap(db, admin, stocked, store):
    """A model that keeps calling tools is stopped by the loop, not by the prompt."""
    client = ScriptedClient([call("replenishment_needs") for _ in range(MAX_ITERATIONS + 5)])

    run = run_agent(db, admin, client, "go")

    assert run.stopped_because == "iteration_limit"
    assert len(run.steps) == MAX_ITERATIONS


def test_a_hallucinated_tool_costs_one_turn_instead_of_raising(db, admin, stocked, store):
    client = ScriptedClient([call("delete_everything", confirm=True), says("Sorry, I cannot do that.")])

    run = run_agent(db, admin, client, "go")

    assert run.steps[0]["ok"] is False
    assert run.summary == "Sorry, I cannot do that."


def test_a_hallucinated_sku_is_reported_back_to_the_model(db, admin, stocked, supplier, store):
    client = ScriptedClient([
        call("propose_purchase", sku="MADE-UP", supplier_name=supplier.name, quantity=5, reason="guessing"),
        says("That SKU does not exist."),
    ])

    run = run_agent(db, admin, client, "go")

    assert run.proposals == []
    assert run.steps[0]["ok"] is False
    # The error reaches the model as a tool result, so it can correct itself.
    tool_messages = [message for message in client.calls[-1] if message["role"] == "tool"]
    assert "MADE-UP" in tool_messages[-1]["content"]


def test_the_run_is_recorded_in_the_audit_trail(db, admin, stocked, supplier, store):
    client = ScriptedClient([
        call("replenishment_needs"),
        call("propose_purchase", sku=stocked.sku, supplier_name=supplier.name, quantity=7, reason="low"),
        says("done"),
    ])

    run_agent(db, admin, client, "weekly check")

    event = store.query({"action": "agent_replenishment_run"}, 5)[0]
    assert event["payload"]["provider"] == "scripted"
    assert event["payload"]["proposal_count"] == 1
    assert event["payload"]["proposed_skus"] == [stocked.sku]
    assert sorted(event["payload"]["tools_used"]) == ["propose_purchase", "replenishment_needs"]
    assert event["payload"]["instruction"] == "weekly check"


def test_a_run_that_proposes_nothing_is_still_a_valid_outcome(db, admin, stocked, store):
    client = ScriptedClient([call("replenishment_needs"), says("Nothing needs ordering.")])

    run = run_agent(db, admin, client, "go")

    assert run.proposals == []
    assert run.stopped_because == "completed"


def test_several_tool_calls_in_one_turn_are_all_executed(db, admin, stocked, store):
    client = ScriptedClient([
        LLMResponse(tool_calls=[ToolCall("item_detail", {"sku": stocked.sku}), ToolCall("supplier_options", {"sku": stocked.sku})]),
        says("Looked at both."),
    ])

    run = run_agent(db, admin, client, "go")

    assert [step["tool"] for step in run.steps] == ["item_detail", "supplier_options"]


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def test_replenishment_needs_reuses_the_real_engine(db, admin, stocked, store):
    rows = dispatch(db, "replenishment_needs", {"limit": 5})

    assert isinstance(rows, list)
    assert {"sku", "days_of_cover", "suggested_quantity"} <= set(rows[0])


def test_the_limit_is_clamped(db, admin, stocked, store):
    assert len(dispatch(db, "replenishment_needs", {"limit": 10_000})) <= 25


def test_item_detail_reports_stock_and_recent_purchases(db, admin, stocked, store):
    detail = dispatch(db, "item_detail", {"sku": stocked.sku})

    assert detail["sku"] == stocked.sku
    assert detail["recent_purchases"][0]["unit_cost"] == 10.0


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("item_detail", {"sku": "NOPE"}),
        ("supplier_options", {"sku": "NOPE"}),
        ("propose_purchase", {"sku": "NOPE", "supplier_name": "x", "quantity": 1, "reason": "r"}),
        ("not_a_tool", {}),
    ],
)
def test_tools_raise_tool_error_rather_than_crashing(db, stocked, tool, arguments, store):
    with pytest.raises(ToolError):
        dispatch(db, tool, arguments)


def test_propose_purchase_rejects_a_nonsense_quantity(db, stocked, supplier, store):
    for quantity in (0, -5, "many"):
        with pytest.raises(ToolError):
            dispatch(db, "propose_purchase", {"sku": stocked.sku, "supplier_name": supplier.name, "quantity": quantity, "reason": "r"})


def test_propose_purchase_rejects_an_unknown_supplier(db, stocked, store):
    with pytest.raises(ToolError):
        dispatch(db, "propose_purchase", {"sku": stocked.sku, "supplier_name": "Nobody Ltd", "quantity": 5, "reason": "r"})


def test_non_object_arguments_are_refused(db, stocked, store):
    with pytest.raises(ToolError):
        dispatch(db, "item_detail", ["not", "an", "object"])


# --------------------------------------------------------------------------
# Provider adapters
# --------------------------------------------------------------------------

def test_gemini_maps_a_function_call_out_of_the_payload():
    response = GeminiClient._from_payload({
        "candidates": [{"content": {"parts": [
            {"text": "Looking now."},
            {"functionCall": {"name": "replenishment_needs", "args": {"limit": 3}}},
        ]}}]
    })

    assert response.text == "Looking now."
    assert response.tool_calls == [ToolCall("replenishment_needs", {"limit": 3})]


def test_gemini_handles_an_empty_candidate_list():
    assert GeminiClient._from_payload({}).text == ""


def test_gemini_sends_tool_output_as_a_function_response():
    content = GeminiClient._to_content({"role": "tool", "name": "item_detail", "content": "{}"})

    assert content["parts"][0]["functionResponse"]["name"] == "item_detail"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [({"a": 1}, {"a": 1}), ('{"a": 1}', {"a": 1}), ("not json", {}), (None, {}), ("[1,2]", {})],
)
def test_ollama_tolerates_both_argument_encodings(raw, expected):
    """Some Ollama models return arguments as an object, others as a JSON string."""
    assert _as_dict(raw) == expected


class _Captured:
    """Stands in for httpx.post so the request body can be asserted on."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.url = None
        self.body = None
        self.params = None

    def __call__(self, url, params=None, json=None, timeout=None):
        self.url, self.params, self.body = url, params, json
        return _Response(self.payload)


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_gemini_request_carries_the_system_prompt_tools_and_key(monkeypatch):
    import app.agent.llm as llm

    captured = _Captured({"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})
    monkeypatch.setattr(llm.httpx, "post", captured)

    response = llm.GeminiClient("secret-key", "gemini-2.0-flash").complete(
        "system text", [{"role": "user", "content": "go"}], [{"name": "t", "parameters": {}}]
    )

    assert response.text == "hi"
    assert captured.params == {"key": "secret-key"}
    assert "gemini-2.0-flash:generateContent" in captured.url
    assert captured.body["systemInstruction"]["parts"][0]["text"] == "system text"
    assert captured.body["tools"] == [{"function_declarations": [{"name": "t", "parameters": {}}]}]
    assert captured.body["contents"] == [{"role": "user", "parts": [{"text": "go"}]}]


def test_gemini_maps_an_assistant_turn_to_the_model_role(monkeypatch):
    import app.agent.llm as llm

    captured = _Captured({"candidates": []})
    monkeypatch.setattr(llm.httpx, "post", captured)

    llm.GeminiClient("k", "m").complete("s", [{"role": "assistant", "content": "thinking"}], [])

    assert captured.body["contents"][0]["role"] == "model"


def test_ollama_request_shape_and_tool_call_mapping(monkeypatch):
    import app.agent.llm as llm

    captured = _Captured({"message": {"content": "sure", "tool_calls": [
        {"function": {"name": "item_detail", "arguments": '{"sku": "BX-100"}'}}
    ]}})
    monkeypatch.setattr(llm.httpx, "post", captured)

    response = llm.OllamaClient("http://localhost:11434/", "llama3.1").complete(
        "system text", [{"role": "user", "content": "go"}, {"role": "tool", "name": "x", "content": "{}"}], [{"name": "t"}]
    )

    assert response.text == "sure"
    assert response.tool_calls == [ToolCall("item_detail", {"sku": "BX-100"})]
    assert captured.url == "http://localhost:11434/api/chat"
    assert captured.body["messages"][0] == {"role": "system", "content": "system text"}
    assert captured.body["messages"][-1]["role"] == "tool"
    assert captured.body["tools"] == [{"type": "function", "function": {"name": "t"}}]
    assert captured.body["stream"] is False


def test_ollama_handles_a_reply_with_no_tool_calls(monkeypatch):
    import app.agent.llm as llm

    monkeypatch.setattr(llm.httpx, "post", _Captured({"message": {"content": "done"}}))

    response = llm.OllamaClient("http://localhost:11434", "llama3.1").complete("s", [], [])

    assert response.text == "done"
    assert response.tool_calls == []


def test_a_scripted_client_that_runs_out_ends_the_run(db, admin, stocked, store):
    run = run_agent(db, admin, ScriptedClient([]), "go")

    assert "exhausted" in run.summary
    assert run.proposals == []


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

class _Settings:
    def __init__(self, **values):
        self.agent_provider = ""
        self.agent_model = ""
        self.gemini_api_key = ""
        self.ollama_base_url = ""
        self.__dict__.update(values)


def test_no_provider_configured_disables_the_agent():
    assert build_client(_Settings()) is None


def test_gemini_needs_a_key_to_be_selected():
    assert build_client(_Settings(agent_provider="gemini")) is None
    assert isinstance(build_client(_Settings(agent_provider="gemini", gemini_api_key="k")), GeminiClient)


def test_ollama_needs_no_key():
    assert isinstance(build_client(_Settings(agent_provider="ollama")), OllamaClient)


def test_an_unknown_provider_is_treated_as_disabled():
    assert build_client(_Settings(agent_provider="something-else")) is None
