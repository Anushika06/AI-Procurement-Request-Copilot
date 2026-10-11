from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from evals import compare
from src import llm, single_agent
from src.data_access import get_request


def completion(message: dict) -> dict:
    return {"id": "c", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}]}


FINAL = completion({"role": "assistant", "content": json.dumps({
    "need_summary": "More signing seats for Finance.", "overlap_assessment": "expansion",
    "overlap_reason": "Extends the existing SignFlow licence.", "rationale": "Routine add-on within budget.",
})})
TOOL_TURN = completion({"role": "assistant", "content": None, "tool_calls": [
    {"id": "call_1", "type": "function", "function": {"name": "run_policy_checks", "arguments": "{}"},
     "extra_content": {"google": {"thought_signature": "sig"}}},
]})


class FakeProviders:
    """Mock transport that routes by host and replays scripted responses per provider."""

    def __init__(self, gemini: list, groq: list):
        self.script = {"generativelanguage.googleapis.com": gemini, "api.groq.com": groq}
        self.sent = {"gemini": [], "groq": []}

    def handler(self, request):
        host = request.url.host
        name = "gemini" if "google" in host else "groq"
        self.sent[name].append(json.loads(request.content))
        status, body, headers = self.script[host].pop(0)
        return httpx.Response(status, json=body, headers=headers or {})

    def client(self) -> llm.LLMClient:
        return llm.LLMClient(http_client=httpx.Client(transport=httpx.MockTransport(self.handler)))


def error(status: int, retry_after: str | None = None):
    message = "RESOURCE_EXHAUSTED: quota exceeded" if status == 429 else "error"
    return status, {"error": {"code": status, "message": message}}, ({"retry-after": retry_after} if retry_after else None)


@pytest.fixture
def both_keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("GROQ_API_KEY", "q-key")
    monkeypatch.setattr(llm, "RETRY_DELAYS_SECONDS", (0.0, 0.0))


def test_provider_order_and_pinning(both_keys, monkeypatch):
    assert [p.name for p in llm.configured_providers()] == ["gemini", "groq"]
    assert llm.provider_chain() == "gemini:gemini-3.8-flash -> groq:openai/gpt-oss-120b"
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    assert [p.name for p in llm.configured_providers()] == ["groq"]
    assert llm.model_name() == "openai/gpt-oss-120b"
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    assert llm.configured_providers() == []
    assert not llm.llm_enabled()


def test_groq_alone_enables_llm_mode(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "q-key")
    monkeypatch.setenv("COPILOT_OFFLINE", "")
    assert llm.llm_enabled()
    assert llm.provider_chain() == "groq:openai/gpt-oss-120b"


def test_quota_error_falls_back_to_groq_and_cools_down_gemini(both_keys):
    fake = FakeProviders(gemini=[error(429)], groq=[(200, FINAL, None), (200, FINAL, None)])
    client = fake.client()
    client.chat([{"role": "user", "content": "hi"}])
    assert client.last_used == "groq:openai/gpt-oss-120b"

    client.chat([{"role": "user", "content": "again"}])
    assert len(fake.sent["gemini"]) == 1, "exhausted provider is skipped during its cooldown"
    assert len(fake.sent["groq"]) == 2


def test_transient_outage_falls_back_after_retries(both_keys):
    fake = FakeProviders(gemini=[error(503)] * 3, groq=[(200, FINAL, None)])
    client = fake.client()
    client.chat([{"role": "user", "content": "hi"}])
    assert len(fake.sent["gemini"]) == 3
    assert client.last_used.startswith("groq:")


def test_request_errors_do_not_fall_back(both_keys):
    fake = FakeProviders(gemini=[error(400)], groq=[(200, FINAL, None)])
    with pytest.raises(llm.LLMError) as err:
        fake.client().chat([{"role": "user", "content": "hi"}])
    assert err.value.status == 400
    assert fake.sent["groq"] == []


def test_pinned_provider_never_falls_back(both_keys, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    fake = FakeProviders(gemini=[error(429)], groq=[(200, FINAL, None)])
    with pytest.raises(llm.LLMError) as err:
        fake.client().chat([{"role": "user", "content": "hi"}])
    assert err.value.is_quota
    assert fake.sent["groq"] == []


def test_short_rate_limit_waits_instead_of_switching(both_keys, monkeypatch):
    monkeypatch.setenv("LLM_RATE_LIMIT_WAIT", "5")
    fake = FakeProviders(gemini=[error(429, "0"), (200, FINAL, None)], groq=[])
    client = fake.client()
    client.chat([{"role": "user", "content": "hi"}])
    assert client.last_used.startswith("gemini:")
    assert len(fake.sent["gemini"]) == 2


def test_long_retry_after_is_not_waited_out(both_keys, monkeypatch):
    monkeypatch.setenv("LLM_RATE_LIMIT_WAIT", "5")
    fake = FakeProviders(gemini=[error(429, "3600")], groq=[(200, FINAL, None)])
    client = fake.client()
    client.chat([{"role": "user", "content": "hi"}])
    assert client.last_used.startswith("groq:")


def test_groq_gets_only_standard_message_fields():
    provider = llm.Provider("groq", "k", llm.GROQ_BASE_URL, "m")
    history = [
        {"role": "system", "content": "s"},
        {"role": "assistant", "reasoning": "hidden", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"},
             "extra_content": {"google": {"thought_signature": "sig"}}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ]
    cleaned = llm._messages_for(provider, history)
    assert cleaned[1] == {"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]}
    assert llm._messages_for(llm.Provider("gemini", "k", "u", "m"), history) is history


def test_agent_switches_provider_mid_run_and_records_both(vendor_api, both_keys, monkeypatch):
    fake = FakeProviders(gemini=[(200, TOOL_TURN, None), error(429)], groq=[(200, FINAL, None)])
    monkeypatch.setattr(single_agent, "LLMClient", fake.client)
    monkeypatch.setattr(single_agent, "llm_enabled", lambda: True)

    decision = single_agent.run_single_agent(get_request("REQ-1001"))

    assert decision.telemetry.mode == "llm"
    assert decision.telemetry.llm_providers == ["gemini:gemini-3.8-flash", "groq:openai/gpt-oss-120b"]
    groq_history = fake.sent["groq"][0]["messages"]
    assert all("extra_content" not in json.dumps(m) for m in groq_history)
    assert decision.required_approvals == ["Manager"]


def test_eval_aborts_when_providers_are_mixed(vendor_api, monkeypatch, capsys):
    labels = iter(["gemini:a", "groq:b"])

    class OneShotLLM:
        def __init__(self):
            self.last_used = next(labels)

        def chat(self, messages, tools=None):
            return FakeMessage()

    class FakeMessage:
        tool_calls = None
        content = FINAL["choices"][0]["message"]["content"]

    monkeypatch.setattr(single_agent, "LLMClient", OneShotLLM)
    monkeypatch.setattr(single_agent, "llm_enabled", lambda: True)
    monkeypatch.setattr(compare, "llm_enabled", lambda: True)
    monkeypatch.setattr(compare, "configured_providers", lambda: [llm.Provider("gemini", "k", "u", "a")])
    summary = Path(compare.RESULTS_DIR / "summary.md")
    before = summary.read_text(encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["compare.py", "--provider", "auto", "--arch", "single"])
    # compare.main() writes these to os.environ; registering them restores them afterwards.
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.setenv("LLM_RATE_LIMIT_WAIT", "")

    with pytest.raises(SystemExit):
        compare.main()
    assert "MIXED PROVIDERS" in capsys.readouterr().out
    assert summary.read_text(encoding="utf-8") == before
