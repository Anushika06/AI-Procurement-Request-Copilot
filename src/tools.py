"""Tools available to the agents.

All tools are scoped to the request being analysed (no free-form arguments), so
text inside a request cannot steer a tool towards another vendor or record.
Every tool returns plain JSON-serialisable data plus evidence items derived
directly from that data.
"""
from __future__ import annotations

import json
import time
from typing import Callable

import requests

from src import data_access, policy
from src.telemetry import RunTelemetryCounter
from src.vendor_client import get_vendor_risk


def _records(df) -> list[dict]:
    return json.loads(df.to_json(orient="records"))


def _ev(source: str, finding: str, reference: str | None = None) -> dict:
    return {"source": source, "finding": finding, "reference": reference}


def _money(value: float | None) -> str:
    return "unknown" if value is None else f"${value:,.0f}"


def lookup_budget(request: dict) -> dict:
    """Requester's department and available software budget vs. the request cost."""
    employees = {e["employee_id"]: e for e in _records(data_access.load_employees())}
    budgets = {b["department"]: b for b in _records(data_access.load_budgets())}
    requester = employees.get(request.get("requester_id"))
    department = requester["department"] if requester else None
    budget = budgets.get(department) if department else None
    cost = request.get("annual_cost_usd") if isinstance(request.get("annual_cost_usd"), (int, float)) else None

    evidence = []
    if requester is None:
        status = "unverified"
        evidence.append(_ev("lookup_budget", f"Requester '{request.get('requester_id')}' not found in employees.csv", "employees.csv"))
    elif budget is None:
        status = "unverified"
        evidence.append(_ev("lookup_budget", f"No software budget record for department '{department}'", "department_budgets.csv"))
    elif cost is None:
        status = "unknown_cost"
        evidence.append(_ev(
            "lookup_budget",
            f"{department} has {_money(budget['available_usd'])} available, but the request has no annual cost to compare",
            f"department_budgets.csv:{department}",
        ))
    else:
        status = "within" if cost <= budget["available_usd"] else "insufficient"
        verb = "within" if status == "within" else "exceeds"
        evidence.append(_ev(
            "lookup_budget",
            f"Annual cost {_money(cost)} {verb} {department}'s available software budget of {_money(budget['available_usd'])}",
            f"department_budgets.csv:{department}",
        ))
    if requester:
        evidence.insert(0, _ev(
            "lookup_budget",
            f"Requester {requester['name']} ({requester['level']}, {department}), manager {requester['manager_id'] or 'none'}",
            f"employees.csv:{requester['employee_id']}",
        ))
    return {"requester": requester, "department": department, "budget": budget, "status": status, "evidence": evidence}


def search_catalog(request: dict) -> dict:
    """Approved software in the same category or from the same vendor, plus purchase history."""
    employees = {e["employee_id"]: e for e in _records(data_access.load_employees())}
    department = (employees.get(request.get("requester_id")) or {}).get("department")
    category = (request.get("category") or "").strip().lower()
    vendor = (request.get("vendor_name") or "").strip().lower()

    category_matches, same_vendor = [], []
    for row in _records(data_access.load_software_catalog()):
        row["covers_requester"] = row["scope"] == "Company-wide" or row["scope"] == department
        if category and row["category"].strip().lower() == category:
            category_matches.append(row)
        if vendor and row["vendor_name"].strip().lower() == vendor:
            same_vendor.append(row)
    history = [p for p in _records(data_access.load_purchase_history()) if p["vendor_name"].strip().lower() == vendor]

    evidence = []
    for row in category_matches:
        evidence.append(_ev(
            "search_catalog",
            f"Existing {row['status'].lower()} tool {row['product_name']} ({row['category']}), scope {row['scope']}, "
            f"{row['licensed_seats']} seats, {_money(row['annual_cost_usd'])}/yr - {row['notes']}",
            f"software_catalog.csv:{row['software_id']}",
        ))
    for row in same_vendor:
        if row not in category_matches:
            evidence.append(_ev(
                "search_catalog",
                f"Company already uses {row['product_name']} from this vendor ({row['status']}, scope {row['scope']})",
                f"software_catalog.csv:{row['software_id']}",
            ))
    for p in history:
        evidence.append(_ev(
            "search_catalog",
            f"Prior purchase {p['product_name']} on {p['purchase_date']}: {_money(p['annual_amount_usd'])} ({p['status']}; {p['notes']})",
            f"purchase_history.csv:{p['purchase_id']}",
        ))
    if not category_matches and not same_vendor:
        evidence.append(_ev("search_catalog", f"No approved catalog product in category '{request.get('category')}' or from this vendor", "software_catalog.csv"))
    return {"category_matches": category_matches, "same_vendor_products": same_vendor, "purchase_history": history, "evidence": evidence}


def get_vendor_record(request: dict) -> dict:
    """Internal vendor registry entry (procurement, security and legal status)."""
    vendor = (request.get("vendor_name") or "").strip().lower()
    record = next((v for v in _records(data_access.load_vendors()) if v["vendor_name"].strip().lower() == vendor), None)
    if record is None:
        return {"found": False, "record": None, "evidence": [
            _ev("get_vendor_record", f"Vendor '{request.get('vendor_name')}' is not in the internal registry (treated as new)", "vendors.csv")
        ]}
    finding = (
        f"Registry: procurement {record['procurement_status']}, security {record['security_status']}"
        f" (reviewed {record['security_review_date'] or 'never'}), legal terms {record['legal_terms_status']}"
    )
    return {"found": True, "record": record, "evidence": [
        _ev("get_vendor_record", finding, f"vendors.csv:{record['vendor_id']}"),
    ]}


def check_vendor_risk(request: dict) -> dict:
    """External vendor-risk service (mock API). Failures are reported, never guessed."""
    vendor = request.get("vendor_name") or ""
    try:
        data = get_vendor_risk(vendor)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else None
        detail = "no record for this vendor" if code == 404 else f"service error HTTP {code}"
        return {"available": False, "data": None, "error": detail, "evidence": [
            _ev("check_vendor_risk", f"Vendor-risk lookup failed: {detail}; status could not be verified", f"GET /vendor-risk/{vendor}")
        ]}
    except requests.RequestException as exc:
        return {"available": False, "data": None, "error": type(exc).__name__, "evidence": [
            _ev("check_vendor_risk", "Vendor-risk service unreachable; status could not be verified", f"GET /vendor-risk/{vendor}")
        ]}
    finding = (
        f"Vendor-risk service: risk {data.get('risk_level')}, security review {data.get('security_review_status')}"
        f" (last {data.get('last_review_date') or 'never'}), personal data {data.get('processes_personal_data')}, "
        f"stores data outside region {data.get('stores_data_outside_region')}"
    )
    return {"available": True, "data": data, "error": None, "evidence": [
        _ev("check_vendor_risk", finding, f"GET /vendor-risk/{vendor}")
    ]}


EVIDENCE_TOOLS: dict[str, Callable[[dict], dict]] = {
    "lookup_budget": lookup_budget,
    "search_catalog": search_catalog,
    "get_vendor_record": get_vendor_record,
    "check_vendor_risk": check_vendor_risk,
}

TOOL_DESCRIPTIONS = {
    "lookup_budget": "Look up the requester, their department and the department's available software budget, and compare it with the request cost.",
    "search_catalog": "Search the approved software catalog and purchase history for products in the same category or from the same vendor.",
    "get_vendor_record": "Read the internal vendor registry entry (procurement, security review and legal terms status).",
    "check_vendor_risk": "Call the external vendor-risk service for the vendor's current security assessment and data-handling profile.",
    "run_policy_checks": "Run the deterministic procurement policy engine (missing fields, approval thresholds, budget, security/privacy/legal triggers, review expiry, conflicts, prompt-injection scan). Gathers any evidence not yet collected.",
}

TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": name, "description": desc, "parameters": {"type": "object", "properties": {}}}}
    for name, desc in TOOL_DESCRIPTIONS.items()
]


class ToolBox:
    """Runs tools for one request, caches results and records telemetry."""

    def __init__(self, request: dict, telemetry: RunTelemetryCounter):
        self.request = request
        self.telemetry = telemetry
        self.results: dict[str, dict] = {}
        self.timings_ms: dict[str, float] = {}

    def call(self, name: str) -> dict:
        if name not in TOOL_DESCRIPTIONS:
            return {"error": f"Unknown tool '{name}'"}
        if name in self.results:
            return self.results[name]
        start = time.perf_counter()
        if name == "run_policy_checks":
            deps = {dep: self.call(dep) for dep in EVIDENCE_TOOLS}
            result = policy.run_policy_checks(
                self.request, deps["lookup_budget"], deps["search_catalog"], deps["get_vendor_record"], deps["check_vendor_risk"]
            )
            result["evidence"] = result["findings"]
        else:
            result = EVIDENCE_TOOLS[name](self.request)
        self.telemetry.record_tool_call(name)
        self.timings_ms[name] = round((time.perf_counter() - start) * 1000, 1)
        self.results[name] = result
        return result

    def gather_all(self) -> dict:
        for name in TOOL_DESCRIPTIONS:
            self.call(name)
        return self.results

    def evidence(self) -> list[dict]:
        items, seen = [], set()
        for name in TOOL_DESCRIPTIONS:
            for item in self.results.get(name, {}).get("evidence", []):
                key = (item["source"], item["finding"])
                if key not in seen:
                    seen.add(key)
                    items.append(item)
        return items

    def for_llm(self, name: str) -> str:
        """Compact tool output for the model (evidence lines are what it may cite)."""
        result = self.results[name]
        if name == "run_policy_checks":
            payload = {k: result[k] for k in ("missing_information", "required_approvals", "risk_flags", "budget_status", "new_vendor")}
            payload["vendor_assessment_state"] = result["vendor_assessment"]["state"]
            payload["findings"] = [f["finding"] for f in result["findings"]]
        else:
            payload = {"evidence": [e["finding"] for e in result.get("evidence", [])]}
        return json.dumps(payload, default=str)
