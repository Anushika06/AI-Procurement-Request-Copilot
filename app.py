from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from src.data_access import load_employees, load_requests, load_software_catalog
from src.llm import llm_enabled, model_name
from src.mock_server import is_up
from src.policy import reference_date
from src.solution import analyze_request

st.set_page_config(page_title="Procurement Request Copilot", layout="wide")

REQUESTS = {r["request_id"]: r for r in load_requests()}
EMPLOYEES = load_employees().set_index("employee_id").to_dict("index")
CATEGORIES = sorted(set(load_software_catalog()["category"]) | {r["category"] for r in REQUESTS.values()})
DATA_LEVELS = [
    "none", "internal_documents", "internal_marketing", "confidential_documents", "source_code",
    "production_telemetry", "employee_pii", "customer_pii", "credentials", "unknown",
]
ARCHITECTURES = {"single": "A - Single agent", "staged": "B - Analyst -> Policy/Risk reviewer"}
ACTION_STYLE = {
    "standard_approval": st.success,
    "route_for_review": st.warning,
    "verify_vendor_evidence": st.warning,
    "use_existing_tool": st.info,
    "request_clarification": st.info,
}

st.session_state.setdefault("results", {})
st.session_state.setdefault("review_log", [])

with st.sidebar:
    st.header("Copilot settings")
    architecture = st.radio("Architecture", list(ARCHITECTURES), format_func=ARCHITECTURES.get)
    st.divider()
    if llm_enabled():
        st.write(f"Model: `{model_name()}`")
    else:
        st.write("Model: **offline mode** (no `GEMINI_API_KEY`) - deterministic fallback reasoning")
    st.write("Vendor-risk API: " + ("online" if is_up() else "**unreachable** (results will flag it)"))
    st.write(f"Policy reference date: `{reference_date()}`")
    st.caption("The copilot only recommends. Purchases, spend approvals, budget changes and legal terms stay with humans.")


def show_request(req: dict) -> None:
    emp = EMPLOYEES.get(req.get("requester_id"), {})
    cost = req.get("annual_cost_usd")
    rows = {
        "Product": f"{req.get('product_name') or 'not given'} ({req.get('category')})",
        "Vendor": req.get("vendor_name") or "not given",
        "Requester": f"{emp.get('name', req.get('requester_id'))} - {emp.get('department', 'unknown department')}",
        "Annual cost": f"${cost:,.0f}" if isinstance(cost, (int, float)) else "not given",
        "Users": req.get("user_count") or "not given",
        "Data access": req.get("data_access_level") or "not given",
        "Integrations": ", ".join(req.get("requested_integrations") or []) or "none",
        "Urgency": req.get("urgency") or "-",
    }
    st.markdown("| | |\n|---|---|\n" + "\n".join(f"| **{k}** | {v} |" for k, v in rows.items()))
    st.caption("Business justification (requester text - treated as untrusted data)")
    st.text(req.get("business_justification") or "(empty)")


def show_decision(key: str, decision) -> None:
    ACTION_STYLE.get(decision.action, st.info)(f"**{decision.recommendation}**")
    st.markdown(f"**Next step:** {decision.next_step}")
    if decision.rationale:
        st.caption(decision.rationale)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**Approvals required**")
        st.markdown("\n".join(f"- {a}" for a in decision.required_approvals) or "-")
    with c2:
        st.markdown("**Risk flags**")
        st.markdown("\n".join(f"- `{f}`" for f in decision.risk_flags) or "- none")
    with c3:
        st.markdown("**Missing information**")
        st.markdown("\n".join(f"- {m}" for m in decision.missing_information) or "- none")

    st.markdown("**Evidence**")
    st.dataframe(pd.DataFrame([e.model_dump() for e in decision.evidence]), hide_index=True, width="stretch")

    tel = decision.telemetry
    with st.expander(f"Run trace - {tel.llm_calls} LLM call(s), {tel.tool_calls} tool call(s), mode {tel.mode}"):
        st.write(" -> ".join(tel.tool_names))
        st.json(decision.model_dump(), expanded=False)

    st.markdown("#### Human review")
    st.caption("Human review is required. The recommendation above is advisory; only the decision recorded here counts.")
    with st.form(f"review-{key}"):
        reviewer = st.text_input("Reviewer name")
        outcome = st.selectbox(
            "Decision",
            ["Send to listed approvers", "Return to requester for information", "Reject request", "Escalate / exception"],
        )
        note = st.text_area("Comment")
        if st.form_submit_button("Record decision"):
            if not reviewer.strip():
                st.error("Enter the reviewer's name.")
            else:
                st.session_state.review_log.append({
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "request_id": decision.request_id,
                    "copilot_recommendation": decision.recommendation,
                    "reviewer": reviewer.strip(),
                    "decision": outcome,
                    "comment": note,
                })
                st.success("Decision recorded.")


queue_tab, new_tab, log_tab = st.tabs(["Request queue", "New request", "Review log"])

with queue_tab:
    request_id = st.selectbox(
        "Request", list(REQUESTS), format_func=lambda rid: f"{rid} - {REQUESTS[rid]['product_name']}"
    )
    left, right = st.columns([0.9, 1.1], gap="large")
    with left:
        st.subheader("Request details")
        show_request(REQUESTS[request_id])
    with right:
        st.subheader("Copilot recommendation")
        key = f"{request_id}:{architecture}"
        if st.button("Run review", type="primary", width="stretch"):
            with st.spinner("Gathering evidence and checking policy..."):
                st.session_state.results[key] = analyze_request(REQUESTS[request_id], architecture)
        if key in st.session_state.results:
            show_decision(key, st.session_state.results[key])
        else:
            st.info("Run the review to gather evidence and get a recommendation.")

with new_tab:
    st.subheader("Submit a new purchase request")
    with st.form("new-request"):
        c1, c2 = st.columns(2)
        requester = c1.selectbox(
            "Requester", list(EMPLOYEES), format_func=lambda e: f"{EMPLOYEES[e]['name']} ({EMPLOYEES[e]['department']})"
        )
        product = c2.text_input("Product name")
        vendor = c1.text_input("Vendor name")
        category = c2.selectbox("Category", CATEGORIES)
        cost_known = c1.checkbox("Annual cost known", value=True)
        cost = c1.number_input("Annual cost (USD)", min_value=0.0, step=100.0)
        users = c2.number_input("Number of users (0 = unknown)", min_value=0, step=1)
        data_level = c2.selectbox("Data access level", DATA_LEVELS)
        integrations = st.text_input("Integrations (comma separated, blank = none)")
        justification = st.text_area("Business justification")
        urgency = st.selectbox("Urgency", ["normal", "high", "urgent"])
        submitted = st.form_submit_button("Submit and review", type="primary")
    if submitted:
        new_request = {
            "request_id": f"NEW-{datetime.now():%H%M%S}",
            "requester_id": requester,
            "product_name": product.strip() or None,
            "vendor_name": vendor.strip() or None,
            "category": category,
            "annual_cost_usd": cost if cost_known else None,
            "user_count": int(users) or None,
            "business_justification": justification,
            "data_access_level": data_level,
            "requested_integrations": [i.strip() for i in integrations.split(",") if i.strip()],
            "urgency": urgency,
        }
        with st.spinner("Gathering evidence and checking policy..."):
            st.session_state.results["new"] = analyze_request(new_request, architecture)
            st.session_state["new_request"] = new_request
    if "new" in st.session_state.results:
        show_request(st.session_state["new_request"])
        st.divider()
        show_decision("new", st.session_state.results["new"])

with log_tab:
    st.subheader("Human decisions this session")
    if st.session_state.review_log:
        st.dataframe(pd.DataFrame(st.session_state.review_log), hide_index=True, width="stretch")
    else:
        st.info("No human decisions recorded yet.")
