from __future__ import annotations

from src.contracts import Architecture, ProcurementDecision
from src.data_access import get_request
from src.single_agent import run_single_agent
from src.staged_agents import run_staged


def analyze_request(request: dict, architecture: Architecture = "single") -> ProcurementDecision:
    """Run either architecture on a request dict (also used by the UI for new requests)."""
    if architecture == "single":
        return run_single_agent(request)
    if architecture == "staged":
        return run_staged(request)
    raise ValueError(f"Unknown architecture: {architecture}")


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter used by the evaluation harness."""
    return analyze_request(get_request(request_id), architecture)
