from __future__ import annotations

import json

import pytest
from openai.types.chat import ChatCompletionMessage

from src import single_agent, staged_agents
from src.data_access import get_request, load_requests
from src.llm import LLMError
from src.solution import analyze_request, handle_request


def tool_call(name: str, idx: int, signature: str | None = None) -> dict:
    call = {"id": f"call_{idx}", "type": "function", "function": {"name": name, "arguments": "{}"}}
    if signature:
        call["extra_content"] = {"google": {"thought_signature": signature}}
    return call


def reply(content: str | None = None, calls: list | None = None) -> ChatCompletionMessage:
    return ChatCompletionMessage.model_validate({"role": "assistant", "content": content, "tool_calls": calls})


class ScriptedLLM:
    """Stands in for the Gemini client and replays a fixed script of replies."""

    script: list = []
    seen: list = []

    def __init__(self):
        self.model = "scripted"
        self.last_used = "scripted:test-model"

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


def test_thought_signatures_are_sent_back(vendor_api, scripted):
    scripted.script = [
        reply(calls=[tool_call("lookup_budget", 1, "sig-1"), tool_call("search_catalog", 2)]),
        reply(content=json.dumps({"need_summary": "x", "overlap_assessment": "expansion",
                                  "overlap_reason": "More signing seats.", "rationale": "Routine add-on."})),
    ]
    decision = handle_request("REQ-1001", "single")
    assert decision.telemetry.mode == "llm"

    second_request = scripted.seen[-1]
    assistant = next(m for m in second_request if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "sig-1"}}
    assert "extra_content" not in assistant["tool_calls"][1]
    assert [m["tool_call_id"] for m in second_request if m["role"] == "tool"] == ["call_1", "call_2"]


def test_fallback_records_the_model_error(vendor_api, scripted):
    scripted.script = [LLMError("BadRequestError: Error code: 400 - invalid argument", 400)]
    decision = handle_request("REQ-1001", "single")
    assert decision.telemetry.mode == "llm_fallback"
    assert "400" in decision.telemetry.llm_error


def judgement(overlap: str, reason: str = "See catalog evidence.") -> str:
    return json.dumps({"need_summary": "Three more signing identities.", "overlap_assessment": overlap,
                       "overlap_reason": reason, "rationale": "Follows the policy-engine output."})


def test_staged_reviewer_none_on_same_product_addon_is_not_overlap(vendor_api, scripted):
    """PUB-01 regression: an add-on to the existing SignFlow licence must not be flagged as tool overlap."""
    scripted.script = [reply(content=judgement("expansion", "More SignFlow seats.")), reply(content=judgement("none"))]
    decision = handle_request("REQ-1001", "staged")
    assert decision.action == "standard_approval"
    assert "existing_tool_overlap" not in decision.risk_flags
    assert decision.required_approvals == ["Manager"]
    overlap = next(e for e in decision.evidence if e.source == "overlap_assessment")
    assert overlap.reference == "Policy §3 (expansion)"


def test_single_agent_none_on_same_product_addon_is_not_overlap(vendor_api, scripted):
    scripted.script = [reply(content=judgement("none"))]
    decision = handle_request("REQ-1001", "single")
    assert "existing_tool_overlap" not in decision.risk_flags
    assert decision.action == "standard_approval"


def test_model_cannot_hide_overlap_with_a_competing_tool(vendor_api, scripted):
    scripted.script = [reply(content=judgement("none")), reply(content=judgement("none"))]
    decision = handle_request("REQ-1002", "staged")
    assert "existing_tool_overlap" in decision.risk_flags


def test_none_on_redundant_tier_upgrade_still_redirects_to_existing_tool(vendor_api, scripted):
    scripted.script = [reply(content=judgement("none"))]
    decision = handle_request("REQ-1008", "single")
    assert decision.action == "use_existing_tool"
    assert "existing_tool_overlap" in decision.risk_flags


def run_with(scripted, request_id: str, architecture: str, overlap: str):
    """Run one request with the model answering `overlap` at every LLM step."""
    scripted.script = [reply(content=judgement(overlap))] * (1 if architecture == "single" else 2)
    return handle_request(request_id, architecture)


@pytest.mark.parametrize("architecture", ["single", "staged"])
@pytest.mark.parametrize("overlap", ["none", "overlap_review", "existing_tool_sufficient", "expansion", "not-a-valid-value"])
def test_pub01_same_product_addon_is_never_competing_overlap(vendor_api, scripted, architecture, overlap):
    decision = run_with(scripted, "REQ-1001", architecture, overlap)
    assert decision.action == "standard_approval"
    assert "existing_tool_overlap" not in decision.risk_flags
    assert decision.required_approvals == ["Manager"]
    evidence = next(e for e in decision.evidence if e.source == "overlap_assessment")
    assert evidence.reference == "Policy §3 (expansion)"


def test_pub01_reviewer_overriding_analyst_with_overlap_review_is_corrected(vendor_api, scripted):
    scripted.script = [reply(content=judgement("expansion", "More SignFlow seats.")),
                       reply(content=judgement("overlap_review", "Check spare seats."))]
    decision = handle_request("REQ-1001", "staged")
    assert "existing_tool_overlap" not in decision.risk_flags
    evidence = next(e for e in decision.evidence if e.source == "overlap_assessment")
    assert "SignFlow" in evidence.finding and "not a competing tool" in evidence.finding


@pytest.mark.parametrize("architecture", ["single", "staged"])
@pytest.mark.parametrize("overlap", ["none", "overlap_review", "expansion"])
def test_pub02_competing_tool_overlap_is_kept(vendor_api, scripted, architecture, overlap):
    """BrandBoard vs PixelCraft/CreativeSuite: a model cannot call a different vendor's tool an expansion."""
    decision = run_with(scripted, "REQ-1002", architecture, overlap)
    assert "existing_tool_overlap" in decision.risk_flags
    assert decision.action == "route_for_review"


@pytest.mark.parametrize("architecture", ["single", "staged"])
@pytest.mark.parametrize("overlap, action", [
    ("existing_tool_sufficient", "use_existing_tool"),
    ("none", "use_existing_tool"),
    ("overlap_review", "standard_approval"),
])
def test_ext03_tier_upgrade_without_stated_gap_keeps_overlap(vendor_api, scripted, architecture, overlap, action):
    """TaskFlow Pro has no expansion signal, so it is not treated as a same-product expansion."""
    decision = run_with(scripted, "REQ-1008", architecture, overlap)
    assert "existing_tool_overlap" in decision.risk_flags
    assert decision.action == action


@pytest.mark.parametrize("overlap", ["overlap_review", "existing_tool_sufficient"])
def test_limited_use_approval_is_not_turned_into_expansion(vendor_api, scripted, overlap):
    """Same vendor and an expansion word, but the existing approval is limited-use and the data is customer PII."""
    request = dict(get_request("REQ-1004"))
    request["business_justification"] = "Additional seats so support agents can draft replies from ticket history."
    scripted.script = [reply(content=judgement(overlap))]
    decision = analyze_request(request, "single")
    assert "existing_tool_overlap" in decision.risk_flags
    assert {"Security", "Privacy"} <= set(decision.required_approvals)
    evidence = next(e for e in decision.evidence if e.source == "overlap_assessment")
    assert evidence.reference == f"Policy §3 ({overlap})"


def test_same_product_expansion_helper():
    from src.decision import same_product_expansion

    signflow = {"software_id": "SW010", "product_name": "SignFlow", "vendor_name": "SignFlow",
                "status": "Approved", "covers_requester": True}
    catalog = {"category_matches": [signflow]}
    base = {"vendor_name": "SignFlow", "data_access_level": "internal_documents"}
    assert same_product_expansion({**base, "business_justification": "Three additional signing identities."}, catalog) == signflow
    assert same_product_expansion({**base, "business_justification": "We want e-signatures."}, catalog) is None
    assert same_product_expansion({**base, "vendor_name": "OtherSign",
                                   "business_justification": "Additional seats."}, catalog) is None


def test_prompts_define_same_product_addons_as_expansion():
    from src.prompts import ANALYST_SYSTEM, reviewer_system, single_agent_system

    for prompt in (ANALYST_SYSTEM, reviewer_system(), single_agent_system()):
        assert "More seats or an add-on for the same product is expansion, not overlap" in prompt
    assert "Keep the analyst's overlap_assessment unless" in reviewer_system()
