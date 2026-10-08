"""Deterministic procurement policy checks (no LLM involved).

Everything here maps directly to a section of data/procurement_policy.md, so
thresholds, review triggers and date checks are reproducible and testable.
"""
from __future__ import annotations

import re
from datetime import date
from functools import lru_cache

from src.data_access import load_policy_text

ASSESSMENT_VALIDITY_DAYS = 365

APPROVAL_ORDER = ["Manager", "Department Head", "Finance", "CFO", "Procurement", "Security", "Privacy", "Legal"]
REVIEW_APPROVALS = {"Security", "Privacy", "Legal"}

UNKNOWN_VALUES = {"", "unknown", "tbd", "n/a", "na", "not sure", "?"}

SECURITY_DATA_KEYWORDS = ("source_code", "source code", "confidential", "pii", "personal", "credential", "secret", "production")
PRIVACY_DATA_KEYWORDS = ("pii", "personal")
SECURITY_INTEGRATION_KEYWORDS = (
    "production", "cloud account", "aws", "azure", "gcp", "git", "repositor", "database", "credential", "secret",
)

INJECTION_PATTERNS = [
    r"\bignore\b.{0,40}\b(rules?|instructions?|polic(y|ies)|controls?|guidelines?|checks?)\b",
    r"\bdisregard\b.{0,40}\b(rules?|instructions?|polic(y|ies)|controls?)\b",
    r"\b(treat|mark|consider|record)\b.{0,40}\bas\b.{0,20}\b(pre-?)?approved\b",
    r"\b(cfo|ceo|vp|director|security|legal|finance)[- ](approved|signed[- ]off)\b",
    r"\bapprove (it|this|the request|the purchase)\b",
    r"\b(bypass|skip|override)\b.{0,30}\b(security|legal|privacy|review|approval|polic(y|ies)|controls?)\b",
    r"\b(system prompt|you are now|new instructions|developer mode)\b",
    r"\b(reveal|print|show|expose|send)\b.{0,30}\b(api key|secret|password|credential|token)s?\b",
]

EXPANSION_PATTERN = re.compile(
    r"\b(additional|expand\w*|extra|more (seats|licen[cs]es|users)|add-?on|upgrade\w*|advanced|new (hires|users|team members))\b",
    re.I,
)
GAP_PATTERN = re.compile(
    r"\b(too|lacks?|missing|doesn'?t|does not|cannot|can'?t|not (suitable|enough|sufficient|support\w*)|insufficient)\b",
    re.I,
)


@lru_cache(maxsize=1)
def reference_date() -> date:
    """Policy reference date, read from the policy document instead of the system clock."""
    match = re.search(r"reference date:\W*(\d{4}-\d{2}-\d{2})", load_policy_text(), re.I)
    if not match:
        raise RuntimeError("Reference date not found in data/procurement_policy.md")
    return date.fromisoformat(match.group(1))


def sort_approvals(names: set[str] | list[str]) -> list[str]:
    return sorted(set(names), key=lambda n: APPROVAL_ORDER.index(n) if n in APPROVAL_ORDER else len(APPROVAL_ORDER))


def financial_approvals(amount: float | None) -> list[str]:
    """Policy section 4 thresholds on the annualized amount."""
    if amount is None:
        return []
    if amount <= 1_000:
        return ["Manager"]
    if amount <= 10_000:
        return ["Department Head", "Procurement"]
    if amount <= 25_000:
        return ["Department Head", "Finance", "Procurement"]
    return ["Department Head", "Finance", "CFO", "Procurement"]


def is_unknown(value: object) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in UNKNOWN_VALUES)


def split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def find_injection(text: str) -> list[str]:
    """Return sentences that try to instruct the copilot instead of describing the request."""
    hits = []
    for sentence in split_sentences(text or ""):
        if any(re.search(p, sentence, re.I) for p in INJECTION_PATTERNS):
            hits.append(sentence)
    return hits


def scan_untrusted_text(fields: dict[str, str]) -> list[dict]:
    findings = []
    for field_name, text in fields.items():
        if isinstance(text, str):
            for sentence in find_injection(text):
                findings.append({"field": field_name, "text": sentence})
    return findings


def find_missing_information(request: dict, requester: dict | None) -> list[str]:
    """Policy section 1 required fields."""
    missing = []
    if requester is None or is_unknown(requester.get("department")):
        missing.append("Requester and department (requester not found in employee directory)")
    if is_unknown(request.get("product_name")) or is_unknown(request.get("vendor_name")):
        missing.append("Product and vendor name")
    cost = request.get("annual_cost_usd")
    if not isinstance(cost, (int, float)) or cost < 0:
        missing.append("Annual cost or a reasonable annual cost estimate (annual_cost_usd)")
    users = request.get("user_count")
    if not isinstance(users, int) or users <= 0:
        missing.append("Number of users / licenses (user_count)")

    justification = request.get("business_justification") or ""
    injected = set(find_injection(justification))
    genuine = " ".join(s for s in split_sentences(justification) if s not in injected)
    if len(re.findall(r"[A-Za-z]{2,}", genuine)) < 4:
        missing.append("Business purpose (justification is missing or too vague to assess the need)")

    if is_unknown(request.get("data_access_level")):
        missing.append("Intended data-access level (data_access_level)")
    if request.get("requested_integrations") is None:
        missing.append("Required integrations (list them, or confirm none)")
    return missing


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    text = text.lower()
    return any(k in text for k in keywords)


def data_triggers_security(data_access_level: str | None) -> bool:
    return not is_unknown(data_access_level) and _contains_any(data_access_level, SECURITY_DATA_KEYWORDS)


def data_triggers_privacy(data_access_level: str | None) -> bool:
    return not is_unknown(data_access_level) and _contains_any(data_access_level, PRIVACY_DATA_KEYWORDS)


def sensitive_integrations(integrations: list[str] | None) -> list[str]:
    return [i for i in integrations or [] if _contains_any(i, SECURITY_INTEGRATION_KEYWORDS)]


def _norm_status(value: str | None) -> str:
    v = (value or "").strip().lower().replace(" ", "_")
    if v in {"approved", "current"}:
        return "approved"
    if v in {"expired", "lapsed"}:
        return "expired"
    if v in {"pending", "not_completed", "in_progress", "draft"}:
        return "not_completed"
    if v in {"rejected", "failed"}:
        return "rejected"
    return "unknown"


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def assess_vendor_security(registry: dict | None, risk: dict | None, risk_error: str | None, ref: date) -> dict:
    """Policy section 5: is there a current (<= 365 days) vendor security assessment, and do sources agree?"""
    reg_status = _norm_status(registry.get("security_status")) if registry else "unknown"
    reg_date = _parse_date(registry.get("security_review_date")) if registry else None
    api_status = _norm_status(risk.get("security_review_status")) if risk else "unknown"
    api_date = _parse_date(risk.get("last_review_date")) if risk else None

    def age(d: date | None) -> int | None:
        return (ref - d).days if d else None

    conflicts = []
    if risk and registry:
        if reg_status != "unknown" and api_status != "unknown" and reg_status != api_status:
            conflicts.append(
                f"Internal registry security status is '{registry.get('security_status')}' but the vendor-risk "
                f"service reports '{risk.get('security_review_status')}'"
            )
        if reg_date and api_date and reg_date != api_date:
            conflicts.append(f"Review dates differ: registry {reg_date}, vendor-risk service {api_date}")

    expired_sources = []
    for label, status, d in (("registry", reg_status, reg_date), ("vendor-risk service", api_status, api_date)):
        days = age(d)
        if status == "expired" or (days is not None and days > ASSESSMENT_VALIDITY_DAYS):
            expired_sources.append(label)

    if risk is None:
        state = "unavailable"
    elif conflicts:
        state = "conflicting"
    elif expired_sources:
        state = "expired"
    elif api_status == "approved" and api_date and age(api_date) <= ASSESSMENT_VALIDITY_DAYS:
        state = "current"
    else:
        state = "not_completed"

    return {
        "state": state,
        "registry_status": reg_status,
        "registry_review_date": reg_date.isoformat() if reg_date else None,
        "service_status": api_status,
        "service_review_date": api_date.isoformat() if api_date else None,
        "expired_sources": expired_sources,
        "conflicts": conflicts,
        "service_error": risk_error,
    }


def run_policy_checks(
    request: dict,
    budget_result: dict,
    catalog_result: dict,
    registry_result: dict,
    risk_result: dict,
) -> dict:
    """Apply policy sections 1-10 to gathered evidence. Returns approvals, flags and cited findings."""
    ref = reference_date()
    requester = budget_result.get("requester")
    registry = registry_result.get("record")
    risk = risk_result.get("data") if risk_result.get("available") else None
    cost = request.get("annual_cost_usd") if isinstance(request.get("annual_cost_usd"), (int, float)) else None
    data_level = request.get("data_access_level")

    approvals: set[str] = set()
    flags: list[str] = []
    findings: list[dict] = []

    def add(finding: str, section: str, flag: str | None = None, approval: str | list[str] | None = None) -> None:
        findings.append({"source": "run_policy_checks", "finding": finding, "reference": f"Policy §{section}"})
        if flag and flag not in flags:
            flags.append(flag)
        if approval:
            approvals.update([approval] if isinstance(approval, str) else approval)

    missing = find_missing_information(request, requester)
    if missing:
        add(f"{len(missing)} required field(s) missing; request is not ready for approval", "1", "missing_information")

    tier = financial_approvals(cost)
    if tier:
        add(f"Annual amount ${cost:,.2f} requires: {', '.join(tier)}", "4", approval=tier)
    else:
        add("Financial approval tier cannot be determined without an annual cost", "4", approval="Procurement")

    budget_status = budget_result.get("status")
    if budget_status == "insufficient":
        add(
            f"Cost exceeds the {budget_result.get('department')} available software budget "
            f"(${budget_result['budget']['available_usd']:,}); Finance budget-exception review needed",
            "2", "budget_insufficient", "Finance",
        )
    elif budget_status == "unverified":
        add("Department budget could not be verified; Finance must confirm funding", "2", "budget_unverified", "Finance")

    overlap = catalog_result.get("category_matches", [])
    if overlap:
        names = ", ".join(f"{m['product_name']} ({m['software_id']})" for m in overlap)
        add(f"Existing approved software in the same category: {names}", "3")

    # Security (section 5)
    vendor = assess_vendor_security(registry, risk, risk_result.get("error"), ref)
    if data_triggers_security(data_level):
        add(f"Data-access level '{data_level}' requires Security review", "5", "security_review_required", "Security")
    risky_integrations = sensitive_integrations(request.get("requested_integrations"))
    if risky_integrations:
        add(
            f"Integration(s) {', '.join(risky_integrations)} touch production/cloud/code systems; Security review required",
            "5", "security_review_required", "Security",
        )
    if vendor["state"] == "unavailable":
        add(
            "Vendor security assessment could not be verified (vendor-risk service unavailable); no favorable status assumed",
            "10", "vendor_risk_unavailable", "Security",
        )
    elif vendor["state"] == "conflicting":
        for c in vendor["conflicts"]:
            add(c, "5", "conflicting_vendor_evidence", "Security")
    if vendor["expired_sources"]:
        add(
            f"Vendor security assessment is older than {ASSESSMENT_VALIDITY_DAYS} days as of {ref} "
            f"(per {', '.join(vendor['expired_sources'])})",
            "5", "vendor_review_expired", "Security",
        )
    if vendor["state"] == "not_completed":
        add("Vendor security assessment is missing or not completed", "5", "security_review_required", "Security")
    if "Security" in approvals and "security_review_required" not in flags:
        flags.append("security_review_required")

    # Privacy (section 6)
    outside_region = bool(risk and risk.get("stores_data_outside_region"))
    sensitive = data_triggers_security(data_level)
    if data_triggers_privacy(data_level):
        add(f"Tool will process personal data ('{data_level}'); Privacy review required", "6", "privacy_review_required", "Privacy")
    if outside_region and sensitive:
        add("Vendor stores data outside the operating region and the request involves sensitive data", "6",
            "privacy_review_required", "Privacy")
    elif risk is None and sensitive:
        add("Data residency could not be verified for sensitive data; Privacy review required as a precaution", "6",
            "privacy_review_required", "Privacy")

    # Legal (section 7)
    is_new_vendor = registry is None or str(registry.get("procurement_status", "")).strip().lower() != "approved"
    if is_new_vendor and cost is not None and cost >= 10_000:
        add(f"New vendor with annual spend ${cost:,.0f} (>= $10,000) requires Legal review", "7", "legal_review_required", "Legal")
    terms = (registry or {}).get("legal_terms_status") or "Unknown"
    if str(terms).strip().lower() != "approved":
        add(f"Vendor legal terms are '{terms}', not approved/standard", "7", "legal_review_required", "Legal")
    if outside_region and sensitive:
        add("Cross-region processing of sensitive data is a material legal issue", "7", "legal_review_required", "Legal")
    if is_new_vendor:
        approvals.add("Procurement")

    # AI tools (section 8)
    limited = [m for m in catalog_result.get("same_vendor_products", []) if "limited" in m["status"].lower()]
    if limited and sensitive:
        add(
            f"{limited[0]['product_name']} is approved for limited use only; the prior approval does not cover "
            f"'{data_level}' data",
            "8",
        )

    # Untrusted content (section 9)
    untrusted = {k: v for k, v in request.items() if isinstance(v, str)}
    untrusted["vendor_registry_notes"] = (registry or {}).get("notes", "")
    untrusted["vendor_risk_notes"] = (risk or {}).get("notes", "")
    injection = scan_untrusted_text(untrusted)
    for hit in injection:
        add(f"Ignored embedded instruction in {hit['field']}: \"{hit['text']}\"", "9", "prompt_injection_detected")

    return {
        "reference_date": ref.isoformat(),
        "missing_information": missing,
        "required_approvals": sort_approvals(approvals),
        "risk_flags": flags,
        "findings": findings,
        "financial_tier": tier,
        "budget_status": budget_status,
        "vendor_assessment": vendor,
        "new_vendor": is_new_vendor,
        "prompt_injection": injection,
    }
