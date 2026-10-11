from __future__ import annotations

import json

import httpx
import pytest

from src import llm
from src.tools import TOOL_SCHEMAS

COMPLETION = {
    "id": "c1", "object": "chat.completion", "created": 0, "model": "test",
    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup_budget", "arguments": "{}"},
                        "extra_content": {"google": {"thought_signature": "abc"}}}],
    }}],
}


def make_client(monkeypatch, statuses: list[int]) -> tuple[llm.LLMClient, list]:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm, "RETRY_DELAYS_SECONDS", (0.0, 0.0))
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        status = statuses.pop(0)
        if status == 200:
            return httpx.Response(200, json=COMPLETION)
        return httpx.Response(status, json={"error": {"code": status, "message": "RESOURCE_EXHAUSTED" if status == 429 else "busy"}})

    return llm.LLMClient(http_client=httpx.Client(transport=httpx.MockTransport(handler))), sent


def test_transient_errors_are_retried(monkeypatch):
    client, sent = make_client(monkeypatch, [503, 503, 200])
    message = client.chat([{"role": "user", "content": "hi"}], tools=TOOL_SCHEMAS)
    assert len(sent) == 3
    assert message.tool_calls[0].function.name == "lookup_budget"


def test_quota_errors_are_not_retried(monkeypatch):
    client, sent = make_client(monkeypatch, [429, 200])
    with pytest.raises(llm.LLMError) as err:
        client.chat([{"role": "user", "content": "hi"}])
    assert err.value.is_quota and err.value.status == 429
    assert len(sent) == 1


def test_persistent_outage_gives_up(monkeypatch):
    client, sent = make_client(monkeypatch, [503, 503, 503])
    with pytest.raises(llm.LLMError) as err:
        client.chat([{"role": "user", "content": "hi"}])
    assert err.value.status == 503 and not err.value.is_quota
    assert len(sent) == 3


def test_signature_round_trips_through_the_sdk(monkeypatch):
    client, sent = make_client(monkeypatch, [200])
    message = client.chat([{"role": "user", "content": "hi"}], tools=TOOL_SCHEMAS)
    turn = llm.assistant_turn(message)
    assert turn["role"] == "assistant"
    assert turn["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "abc"
    json.dumps(turn)


def test_request_settings(monkeypatch):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    client, sent = make_client(monkeypatch, [200])
    client.chat([{"role": "user", "content": "hi"}], tools=TOOL_SCHEMAS)
    body = sent[0]
    assert body["model"] == llm.model_name()
    assert body["reasoning_effort"] == "low"
    assert body["temperature"] == 0


def test_tool_schemas_have_no_empty_parameter_objects():
    for tool in TOOL_SCHEMAS:
        assert tool["type"] == "function"
        assert "parameters" not in tool["function"]


def test_default_model(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert llm.model_name() == "gemini-3.8-flash"
    monkeypatch.setenv("LLM_MODEL", "")
    assert llm.model_name() == "gemini-3.8-flash"


# ---------------------------------------------------------------------------
# Groq JSON-hallucination normalisation tests
# ---------------------------------------------------------------------------

def _groq_client(monkeypatch, completion_body: dict):
    """Create a Groq-pinned LLMClient backed by a mock HTTP transport."""
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setattr(llm, "RETRY_DELAYS_SECONDS", (0.0, 0.0))
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=completion_body)

    return llm.LLMClient(http_client=httpx.Client(transport=httpx.MockTransport(handler))), sent


def test_groq_json_tool_injected_into_request(monkeypatch):
    """The dummy 'json' tool is appended for Groq; TOOL_SCHEMAS itself is not mutated."""
    original_len = len(TOOL_SCHEMAS)
    client, sent = _groq_client(monkeypatch, {
        "id": "c1", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "hi", "tool_calls": None,
        }}],
    })
    client.chat([{"role": "user", "content": "hi"}], tools=TOOL_SCHEMAS)
    sent_tools = sent[0]["tools"]
    assert any(t["function"]["name"] == "json" for t in sent_tools), "json dummy not sent"
    assert len(TOOL_SCHEMAS) == original_len, "global TOOL_SCHEMAS was mutated"
    declared = [t["function"]["name"] for t in sent_tools if t["function"]["name"] != "json"]
    assert declared == [t["function"]["name"] for t in TOOL_SCHEMAS], "declared tools changed"


def test_groq_pure_json_hallucination_is_unwrapped(monkeypatch):
    """A lone 'json' tool call is unwrapped into message.content; tool_calls becomes None."""
    client, sent = _groq_client(monkeypatch, {
        "id": "c1", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "json", "arguments": '{"need_summary": "x"}'}}],
        }}],
    })
    message = client.chat([{"role": "user", "content": "hi"}], tools=TOOL_SCHEMAS)
    assert message.content == '{"need_summary": "x"}'
    assert message.tool_calls is None


def test_groq_json_with_real_tools_strips_json_only(monkeypatch):
    """When real tools co-occur with 'json', only 'json' is stripped; real calls pass through."""
    client, sent = _groq_client(monkeypatch, {
        "id": "c1", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": "r1", "type": "function", "function": {"name": "lookup_budget", "arguments": "{}"}},
                {"id": "j1", "type": "function", "function": {"name": "json", "arguments": "{}"}},
            ],
        }}],
    })
    message = client.chat([{"role": "user", "content": "hi"}], tools=TOOL_SCHEMAS)
    assert message.tool_calls is not None
    names = [c.function.name for c in message.tool_calls]
    assert names == ["lookup_budget"], f"expected only real tool, got {names}"


def test_groq_real_tool_not_unwrapped(monkeypatch):
    """A genuine declared tool call from Groq is returned as-is, unmodified."""
    client, sent = _groq_client(monkeypatch, {
        "id": "c1", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "r1", "type": "function",
                            "function": {"name": "lookup_budget", "arguments": "{}"}}],
        }}],
    })
    message = client.chat([{"role": "user", "content": "hi"}], tools=TOOL_SCHEMAS)
    assert message.tool_calls is not None
    assert message.tool_calls[0].function.name == "lookup_budget"
    assert message.content is None


def test_groq_json_empty_arguments_safe_failure(monkeypatch):
    """Empty arguments in a 'json' tool call produce empty content; parse_json fails safely."""
    client, sent = _groq_client(monkeypatch, {
        "id": "c1", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "json", "arguments": ""}}],
        }}],
    })
    message = client.chat([{"role": "user", "content": "hi"}], tools=TOOL_SCHEMAS)
    assert message.content == ""
    assert message.tool_calls is None
    with pytest.raises(llm.LLMError, match="no content"):
        llm.parse_json(message.content)


def _gemini_429_then_ok(monkeypatch, error_body: dict):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_RATE_LIMIT_WAIT", "30")
    slept = []
    monkeypatch.setattr(llm.time, "sleep", slept.append)
    responses = [httpx.Response(429, json=error_body), httpx.Response(200, json=COMPLETION)]
    sent = []

    def handler(request):
        sent.append(request)
        return responses.pop(0)

    return llm.LLMClient(http_client=httpx.Client(transport=httpx.MockTransport(handler))), sent, slept


def test_gemini_retry_delay_in_body_is_honoured(monkeypatch):
    body = [{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Rate limit. Please retry in 12.5s.",
                       "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"}]}}]
    client, sent, slept = _gemini_429_then_ok(monkeypatch, body)
    client.chat([{"role": "user", "content": "hi"}])
    assert len(sent) == 2
    assert 12.0 in slept


def test_gemini_daily_quota_is_not_waited_out(monkeypatch):
    body = [{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                       "message": "Quota exceeded for metric generate_content_free_tier_requests, limit: 20",
                       "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                    "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
                                   {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "5s"}]}}]
    client, sent, slept = _gemini_429_then_ok(monkeypatch, body)
    with pytest.raises(llm.LLMError) as err:
        client.chat([{"role": "user", "content": "hi"}])
    assert err.value.is_quota
    assert len(sent) == 1 and slept == []
    assert llm._cooldown_until["gemini"] - llm.time.monotonic() > 3000


def test_min_call_interval_spaces_calls(monkeypatch):
    client, sent = make_client(monkeypatch, [200, 200])
    monkeypatch.setenv("LLM_MIN_CALL_INTERVAL", "10")
    slept = []
    monkeypatch.setattr(llm.time, "sleep", slept.append)
    client.chat([{"role": "user", "content": "a"}])
    client.chat([{"role": "user", "content": "b"}])
    assert len(slept) == 1 and 9.0 < slept[0] <= 10.0
