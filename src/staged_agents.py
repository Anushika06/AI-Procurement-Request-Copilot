"""Architecture B: Procurement Analyst -> Policy/Risk Reviewer.

Evidence is gathered by a fixed tool plan, the analyst interprets the need and
overlap, and the reviewer checks that judgement against the policy-engine
output before the decision is assembled.
"""
from __future__ import annotations

import json

from src.contracts import ProcurementDecision
from src.decision import build_decision, heuristic_judgement
from src.llm import LLMClient, LLMError, llm_enabled, parse_json
from src.prompts import ANALYST_SYSTEM, request_block, reviewer_system
from src.telemetry import RunTelemetryCounter
from src.tools import EVIDENCE_TOOLS, ToolBox


def run_staged(request: dict) -> ProcurementDecision:
    telemetry = RunTelemetryCounter()
    toolbox = ToolBox(request, telemetry)

    for name in EVIDENCE_TOOLS:
        toolbox.call(name)
    evidence_pack = {name: [e["finding"] for e in toolbox.results[name]["evidence"]] for name in EVIDENCE_TOOLS}

    if not llm_enabled():
        toolbox.call("run_policy_checks")
        return build_decision(request, toolbox, heuristic_judgement(request, toolbox), telemetry, "offline")

    llm = LLMClient()
    try:
        analyst = parse_json(_ask(llm, telemetry, ANALYST_SYSTEM,
                                  f"{request_block(request)}\n<evidence_pack>\n{json.dumps(evidence_pack, indent=2)}\n</evidence_pack>"))
    except LLMError as exc:
        toolbox.call("run_policy_checks")
        judgement = heuristic_judgement(request, toolbox)
        judgement["rationale"] = f"Analyst model unavailable ({str(exc)[:120]}); deterministic fallback used."
        return build_decision(request, toolbox, judgement, telemetry, "llm_fallback")

    toolbox.call("run_policy_checks")
    handoff = {
        "analyst_assessment": analyst,
        "policy_engine": json.loads(toolbox.for_llm("run_policy_checks")),
        "catalog_evidence": evidence_pack["search_catalog"],
    }
    try:
        judgement = parse_json(_ask(llm, telemetry, reviewer_system(),
                                    f"{request_block(request)}\n<handoff>\n{json.dumps(handoff, indent=2)}\n</handoff>"))
        mode = "llm"
    except LLMError as exc:
        judgement = {
            "need_summary": analyst.get("need_summary"),
            "overlap_assessment": analyst.get("overlap_assessment"),
            "overlap_reason": analyst.get("overlap_reason"),
            "rationale": f"Reviewer model unavailable ({str(exc)[:120]}); analyst judgement used with policy-engine output.",
        }
        mode = "llm_fallback"
    return build_decision(request, toolbox, judgement, telemetry, mode)


def _ask(llm: LLMClient, telemetry: RunTelemetryCounter, system: str, user: str) -> str:
    message = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}])
    telemetry.record_llm_call()
    return message.content
