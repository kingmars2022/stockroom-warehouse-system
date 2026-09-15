"""The model boundary, kept deliberately thin.

The agent's value is in its tools, its loop, and the limits placed on it —
none of which depend on who serves the model. So the provider sits behind one
small interface with two methods' worth of surface, and everything above it is
written against that.

Three implementations ship:

* ``GeminiClient`` — Google's free API tier, the default. No card required.
* ``OllamaClient`` — a model running on localhost. No account at all.
* ``ScriptedClient`` — returns a canned sequence. This is what the tests and CI
  use, so the whole agent is exercised on every push with no API key, no
  network, no cost, and no flakiness from a model that words things differently
  on Tuesday.

That last one is the reason this interface exists at all. An agent whose tests
require a live model is an agent that is not tested.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """One model turn: prose, tool calls, or both."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse: ...


class ScriptedClient:
    """Replays a fixed list of responses. Used by the tests.

    Not a mock of the provider's wire format — a stand-in for the model's
    *decisions*, which is the part the agent logic actually reacts to.
    """

    name = "scripted"

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        self.calls.append(messages)
        if not self._responses:
            return LLMResponse(text="(scripted client exhausted)")
        return self._responses.pop(0)


class GeminiClient:
    """Google Generative Language API. The free tier needs an API key but no card.

    Only the subset the agent uses is mapped: a system instruction, a flat
    message history, function declarations, and function calls coming back.
    """

    name = "gemini"

    def __init__(self, api_key: str, model: str, timeout: float = 60.0):
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [self._to_content(message) for message in messages],
            "tools": [{"function_declarations": tools}] if tools else [],
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self._model}:generateContent"
        response = httpx.post(url, params={"key": self._api_key}, json=body, timeout=self._timeout)
        response.raise_for_status()
        return self._from_payload(response.json())

    @staticmethod
    def _to_content(message: dict) -> dict:
        role = "model" if message["role"] == "assistant" else "user"
        if message["role"] == "tool":
            # Gemini carries tool output as a functionResponse part on a user turn.
            return {"role": "user", "parts": [{"functionResponse": {"name": message["name"], "response": {"result": message["content"]}}}]}
        return {"role": role, "parts": [{"text": message["content"]}]}

    @staticmethod
    def _from_payload(payload: dict) -> LLMResponse:
        candidates = payload.get("candidates") or []
        if not candidates:
            return LLMResponse(text="")
        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        calls = [
            ToolCall(name=part["functionCall"]["name"], arguments=dict(part["functionCall"].get("args") or {}))
            for part in parts
            if "functionCall" in part
        ]
        return LLMResponse(text=text, tool_calls=calls)


class OllamaClient:
    """A model served by a local Ollama. No account, no network egress."""

    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout: float = 120.0):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        body = {
            "model": self._model,
            "stream": False,
            "messages": [{"role": "system", "content": system}] + [self._to_message(message) for message in messages],
            "tools": [{"type": "function", "function": tool} for tool in tools],
        }
        response = httpx.post(f"{self._base_url}/api/chat", json=body, timeout=self._timeout)
        response.raise_for_status()
        message = response.json().get("message", {})
        calls = [
            ToolCall(name=call["function"]["name"], arguments=_as_dict(call["function"].get("arguments")))
            for call in message.get("tool_calls") or []
        ]
        return LLMResponse(text=message.get("content", ""), tool_calls=calls)

    @staticmethod
    def _to_message(message: dict) -> dict:
        if message["role"] == "tool":
            return {"role": "tool", "content": message["content"]}
        return {"role": message["role"], "content": message["content"]}


def _as_dict(arguments: Any) -> dict[str, Any]:
    """Ollama sends arguments as an object on some models and a JSON string on others."""
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_client(settings) -> LLMClient | None:
    """Pick a provider from configuration, or None when the agent is switched off.

    Returning None rather than raising keeps the rest of the API working with
    no model configured, which is how the local demo and the test suite run.
    """
    provider = (settings.agent_provider or "").lower()
    if provider == "gemini" and settings.gemini_api_key:
        return GeminiClient(settings.gemini_api_key, settings.agent_model or "gemini-2.0-flash")
    if provider == "ollama":
        return OllamaClient(settings.ollama_base_url or "http://localhost:11434", settings.agent_model or "llama3.1")
    return None
