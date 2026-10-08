"""Turns gathered evidence + the agent's judgement into a ProcurementDecision.

The model only contributes interpretation (need summary, whether an existing
tool covers the need, rationale wording). Approvals, risk flags, missing
information and the human-review requirement always come from the
deterministic policy checks and cannot be removed by model output.
"""
from __future__ import annotations

import re

from src.contracts import EvidenceItem, ProcurementDecision, RunTelemetry
from src.policy import EXPANSION_PATTERN, GAP_PATTERN, REVIEW_APPROVALS, data_triggers_security, find_injection
from src.telemetry import RunTelemetryCounter
from src.tools import ToolBox

OVERLAP_VALUES = ("none", "expansion", "overlap_review", "existing_tool_sufficient")

CLARIFY = "request_clarification"
USE_EXISTING = "use_existing_tool"
VERIFY = "verify_vendor_evidence"
ROUTE = "route_for_review"
STANDARD = "standard_approval"

APPROVAL_CLAIM = re.compile(
    r"\b(has been|is|was|are|already)\s+(pre-?)?approved\b|\bcfo-approved\b|\bapproved? (it|this) immediately\b"
    r"|\b(purchase|order) (has been|was) (made|placed)\b",
    re.I,
)


def heuristic_overlap(request: dict, catalog: dict) -> tuple[str, str]:
    """Offline stand-in for the model's overlap judgement."""
    matches = catalog.get("category_matches", [])
    if not matches:
        return "none", "No approved product in the same category."
    text = request.get("business_justification") or ""
    vendor = (request.get("vendor_name") or "").strip().lower()
    names = ", ".join(m["product_name"] for m in matches)

    same_vendor = [m for m in matches if m["vendor_name"].strip().lower() == vendor]
    if same_vendor and EXPANSION_PATTERN.search(text):
        return "expansion", f"Request extends the existing {same_vendor[0]['product_name']} footprint."
    mentioned = [m for m in matches if m["vendor_name"].lower() in text.lower() or m["product_name"].lower() in text.lower()]
    if mentioned or GAP_PATTERN.search(text):
        return "overlap_review", f"Requester states a gap versus existing {names}; Procurement should validate it."
    limited = [m for m in matches if "limited" in m["status"].lower()]
    if limited and data_triggers_security(request.get("data_access_level")):
        return "overlap_review", f"{limited[0]['product_name']} is limited-use and does not cover this data class."
    covering = [m for m in matches if m["covers_requester"]]
    if covering:
        return "existing_tool_sufficient", f"{covering[0]['product_name']} already covers {covering[0]['scope']} and no gap is stated."
    return "overlap_review", f"Similar tools exist ({names}) but outside the requester's scope."


def heuristic_judgement(request: dict, toolbox: ToolBox) -> dict:
    overlap, reason = heuristic_overlap(request, toolbox.results["search_catalog"])
    return {"need_summary": need_summary(request, toolbox), "overlap_assessment": overlap, "overlap_reason": reason, "rationale": None}


def need_summary(request: dict, toolbox: ToolBox) -> str:
    dept = toolbox.results.get("lookup_budget", {}).get("department") or "unknown department"
    users = request.get("user_count") or "an unspecified number of"
    return (
        f"{dept} requests {request.get('product_name')} ({request.get('category')}) from {request.get('vendor_name')} "
        f"for {users} users with '{request.get('data_access_level')}' data access."
    )


def safe_model_text(text: object, limit: int = 700) -> str | None:
    """Drop model text that repeats embedded instructions or claims an approval exists."""
    if not isinstance(text, str) or not text.strip():
        return None
    if find_injection(text) or APPROVAL_CLAIM.search(text):
        return None
    return text.strip()[:limit]


def choose_action(checks: dict, overlap: str) -> str:
    if checks["missing_information"]:
        return CLARIFY
    if overlap == "existing_tool_sufficient":
        return USE_EXISTING
    if checks["vendor_assessment"]["state"] in {"unavailable", "conflicting"}:
        return VERIFY
    if REVIEW_APPROVALS & set(checks["required_approvals"]) or checks["budget_status"] in {"insufficient", "unverified"}:
        return ROUTE
    return STANDARD


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def describe_action(action: str, request: dict, checks: dict, catalog: dict, requester: dict | None) -> tuple[str, str]:
    approvals = checks["required_approvals"]
    reviews = [a for a in approvals if a in REVIEW_APPROVALS]
    business = [a for a in approvals if a not in REVIEW_APPROVALS]
    budget_note = " and a Finance budget exception" if checks["budget_status"] == "insufficient" else ""

    if action == CLARIFY:
        who = requester["name"] if requester else "the requester"
        fields = "; ".join(checks["missing_information"])
        extra = ""
        if catalog.get("category_matches"):
            extra = f" Also ask whether existing {catalog['category_matches'][0]['product_name']} already meets the need."
        return (
            "Request clarification - the request is incomplete and cannot be routed for approval",
            f"Return the request to {who} for: {fields}. Re-run the review once complete.{extra}",
        )
    if action == USE_EXISTING:
        m = next(m for m in catalog["category_matches"] if m["covers_requester"])
        return (
            f"Use the existing {m['product_name']} licence instead of a new purchase",
            f"Procurement to confirm with the requester that {m['product_name']} (scope {m['scope']}, "
            f"{m['licensed_seats']} seats) covers the need. Open a new purchase only if a specific gap is documented; "
            f"it would then need {_join(approvals)}.",
        )
    if action == VERIFY:
        state = checks["vendor_assessment"]["state"]
        problem = "could not be retrieved" if state == "unavailable" else "is conflicting between sources"
        return (
            "Hold for manual verification - vendor security evidence " + problem,
            f"Security and Procurement to verify the vendor's current security assessment manually, then complete "
            f"{_join(reviews) or 'the'} review{budget_note} before routing to {_join(business)} for approval.",
        )
    if action == ROUTE:
        parts = reviews + (["Finance (budget exception)"] if checks["budget_status"] in {"insufficient", "unverified"} else [])
        return (
            f"Route to {_join(parts)} review before business approval",
            f"Send the evidence pack to {_join(parts)}. After their sign-off, route to {_join(business)} for approval. "
            "No purchase or commitment until all approvals are recorded.",
        )
    return (
        f"Ready for {_join(approvals)} approval - no additional reviews triggered",
        f"Send to {_join(approvals)} for approval with the evidence pack attached. Procurement places the order only "
        "after approval is recorded.",
    )


def default_rationale(action: str, checks: dict, judgement: dict) -> str:
    parts = []
    if checks["missing_information"]:
        parts.append(f"{len(checks['missing_information'])} required field(s) are missing")
    if judgement["overlap_assessment"] != "none":
        parts.append(judgement["overlap_reason"])
    if checks["budget_status"] == "insufficient":
        parts.append("cost exceeds the department's available budget")
    state = checks["vendor_assessment"]["state"]
    if state != "current":
        parts.append(f"vendor security assessment is {state.replace('_', ' ')}")
    reviews = [a for a in checks["required_approvals"] if a in REVIEW_APPROVALS]
    if reviews:
        parts.append(f"policy requires {_join(reviews)} review")
    if checks["prompt_injection"]:
        parts.append("embedded instructions in the request were ignored")
    if action == STANDARD:
        parts.append("budget is sufficient and no Security, Privacy or Legal trigger applies")
    text = "; ".join(p.rstrip(".") for p in parts)
    return text[0].upper() + text[1:] + "."


def build_decision(
    request: dict,
    toolbox: ToolBox,
    judgement: dict,
    telemetry: RunTelemetryCounter,
    mode: str,
) -> ProcurementDecision:
    if "run_policy_checks" not in toolbox.results:
        toolbox.call("run_policy_checks")
    checks = toolbox.results["run_policy_checks"]
    catalog = toolbox.results["search_catalog"]

    overlap = judgement.get("overlap_assessment")
    if overlap not in OVERLAP_VALUES:
        overlap, judgement["overlap_reason"] = heuristic_overlap(request, catalog)
    if not catalog.get("category_matches"):
        overlap = "none"
    elif overlap == "none":
        overlap = "overlap_review"
    if overlap == "existing_tool_sufficient" and not any(m["covers_requester"] for m in catalog["category_matches"]):
        overlap = "overlap_review"
    judgement["overlap_assessment"] = overlap
    judgement["overlap_reason"] = safe_model_text(judgement.get("overlap_reason")) or heuristic_overlap(request, catalog)[1]

    flags = list(checks["risk_flags"])
    if overlap in {"overlap_review", "existing_tool_sufficient"}:
        flags.append("existing_tool_overlap")

    action = choose_action(checks, overlap)
    requester = toolbox.results["lookup_budget"].get("requester")
    recommendation, next_step = describe_action(action, request, checks, catalog, requester)

    evidence = [EvidenceItem(**e) for e in toolbox.evidence()]
    evidence.append(EvidenceItem(source="overlap_assessment", finding=judgement["overlap_reason"], reference="Policy §3"))

    rationale = safe_model_text(judgement.get("rationale")) or default_rationale(action, checks, judgement)
    summary = safe_model_text(judgement.get("need_summary"), 300) or need_summary(request, toolbox)

    return ProcurementDecision(
        request_id=request.get("request_id") or "AD-HOC",
        recommendation=recommendation,
        action=action,
        rationale=f"{summary} {rationale}",
        evidence=evidence,
        required_approvals=checks["required_approvals"],
        missing_information=checks["missing_information"],
        risk_flags=flags,
        next_step=next_step,
        human_review_required=True,
        telemetry=RunTelemetry(
            llm_calls=telemetry.llm_calls,
            tool_calls=telemetry.tool_calls,
            tool_names=telemetry.tool_names,
            mode=mode,
        ),
    )
