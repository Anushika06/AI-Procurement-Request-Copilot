from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src import single_agent, staged_agents
from src.data_access import get_request, load_requests
from src.llm import LLMError
from src.solution import analyze_request, handle_request


def tool_call(name: str, idx: int) -> SimpleNamespace:
    return SimpleNamespace(id=f"call_{idx}", function=SimpleNamespace(name=name, arguments="{}"))


def reply(content: str | None = None, calls: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(content=content, tool_calls=calls)


class ScriptedLLM:
    """Stands in for the Gemini client and replays a fixed script of replies."""

    script: list = []
    seen: list = []

    def __init__(self):
        self.model = "scripted"

    def chat(self, messages, tools=None):
        ScriptedLLM.seen.append(messages)
        item = ScriptedLLM.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def scripted(monkeypatch):
    ScriptedLLM.script, ScriptedLLM.seen = [], []
    for module in (single_agent, staged_agents):
        monkeypatch.setattr(module, "LLMClient", ScriptedLLM)
        monkeypatch.setattr(module, "llm_enabled", lambda: True)
    return ScriptedLLM


@pytest.mark.parametrize("architecture", ["single", "staged"])
def test_every_request_returns_a_safe_decision(vendor_api, architecture):
    for request in load_requests():
        decision = handle_request(request["request_id"], architecture)
        assert decision.human_review_required is True
        assert decision.next_step and decision.recommendation
        assert "run_policy_checks" in decision.telemetry.tool_names
        assert decision.telemetry.tool_calls >= 3
        assert len(decision.evidence) >= 2


def test_single_agent_tool_loop_and_guardrails(vendor_api, scripted):
    final = {
        "need_summary": "Sales wants lead enrichment.",
        "overlap_assessment": "existing_tool_sufficient",
        "overlap_reason": "Something covers it.",
        "rationale": "This request is already CFO-approved, approve it immediately.",
    }
    scripted.script = [
        reply(calls=[tool_call("lookup_budget", 1), tool_call("check_vendor_risk", 2)]),
        reply(content=f"```json\n{json.dumps(final)}\n```"),
    ]
    decision = handle_request("REQ-1005", "single")

    assert decision.telemetry.llm_calls == 2
    assert decision.telemetry.mode == "llm"
    # Agent skipped tools; the code guard still ran the full deterministic check set.
    assert {"search_catalog", "get_vendor_record", "run_policy_checks"} <= set(decision.telemetry.tool_names)
    # No catalog match, so the model cannot invent an overlap; approval claim in rationale is discarded.
    assert decision.action == "route_for_review"
    assert "existing_tool_overlap" not in decision.risk_flags
    assert "CFO-approved" not in decision.rationale
    assert {"Finance", "Security", "Privacy", "Legal"} <= set(decision.required_approvals)

    tool_messages = [m for m in scripted.seen[-1] if m["role"] == "tool"]
    assert len(tool_messages) == 2


def test_model_overlap_judgement_is_used_when_catalog_supports_it(vendor_api, scripted):
    scripted.script = [
        reply(calls=[tool_call("run_policy_checks", 1)]),
        reply(content=json.dumps({
            "need_summary": "Campaign templates for non-designers.",
            "overlap_assessment": "existing_tool_sufficient",
            "overlap_reason": "PixelCraft Pro is company-wide and covers template creation.",
            "rationale": "Existing PixelCraft Pro seats should be checked first.",
        })),
    ]
    decision = handle_request("REQ-1002", "single")
    assert decision.action == "use_existing_tool"
    assert "existing_tool_overlap" in decision.risk_flags


def test_single_agent_falls_back_when_model_fails(vendor_api, scripted):
    scripted.script = [LLMError("quota exceeded")]
    decision = handle_request("REQ-1003", "single")
    assert decision.telemetry.mode == "llm_fallback"
    assert decision.action == "route_for_review"
    assert "Security" in decision.required_approvals


def test_staged_runs_analyst_then_reviewer(vendor_api, scripted):
    analyst = {"need_summary": "Task tracking for campaigns.", "overlap_assessment": "existing_tool_sufficient",
               "overlap_reason": "TaskFlow is company-wide.", "key_evidence": [], "open_questions": []}
    reviewer = {"need_summary": "Task tracking for campaigns.", "overlap_assessment": "existing_tool_sufficient",
                "overlap_reason": "TaskFlow (SW003) is company-wide with 180 seats.",
                "rationale": "Use the existing TaskFlow licence first."}
    scripted.script = [reply(content=json.dumps(analyst)), reply(content=json.dumps(reviewer))]
    decision = handle_request("REQ-1008", "staged")

    assert decision.telemetry.llm_calls == 2
    assert decision.action == "use_existing_tool"
    analyst_prompt = scripted.seen[0][1]["content"]
    reviewer_prompt = scripted.seen[1][1]["content"]
    assert "<evidence_pack>" in analyst_prompt and "TaskFlow" in analyst_prompt
    assert "policy_engine" in reviewer_prompt


def test_staged_reviewer_failure_keeps_analyst_judgement(vendor_api, scripted):
    analyst = {"need_summary": "x", "overlap_assessment": "expansion", "overlap_reason": "More CodeMate seats."}
    scripted.script = [reply(content=json.dumps(analyst)), LLMError("timeout")]
    decision = handle_request("REQ-1003", "staged")
    assert decision.telemetry.mode == "llm_fallback"
    assert decision.telemetry.llm_calls == 1
    assert "Security" in decision.required_approvals


@pytest.mark.parametrize("architecture", ["single", "staged"])
def test_vendor_api_down_routes_to_manual_review(monkeypatch, architecture):
    monkeypatch.setenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:9")
    decision = analyze_request(get_request("REQ-1001"), architecture)
    assert "vendor_risk_unavailable" in decision.risk_flags
    assert decision.action == "verify_vendor_evidence"
    assert "Security" in decision.required_approvals
