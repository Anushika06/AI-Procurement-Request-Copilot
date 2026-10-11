"""Run the same case set against both architectures and write a comparison.

    python evals/compare.py                                  # LLM mode if GEMINI_API_KEY is set, else offline
    python evals/compare.py --arch single --cases PUB-01     # smoke test, nothing written
    python evals/compare.py --delay 5                        # pause between cases (per-minute rate limits)
    python evals/compare.py --provider groq                  # pin the provider for the whole run

A full run writes evals/results/comparison.csv and evals/results/summary.md.
In LLM mode the run is pinned to one provider (the primary one by default) so
both architectures are compared on the same model. It stops at the first case
that falls back to the offline heuristics or that used a different provider,
so a written result is always a single-model LLM evaluation.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.run_public_evals import evaluate as public_minimum_checks  # noqa: E402
from src.data_access import get_request, load_budgets, load_employees, load_purchase_history, load_software_catalog, load_vendors  # noqa: E402
from src.decision import APPROVAL_CLAIM  # noqa: E402
from src.llm import configured_providers, llm_enabled  # noqa: E402
from src.mock_server import mock_api_running  # noqa: E402
from src.solution import analyze_request  # noqa: E402

RESULTS_DIR = ROOT / "evals" / "results"
ARCHITECTURES = ["single", "staged"]
REVIEWS = {"Security", "Privacy", "Legal"}

ID_COLUMNS = {
    "employees.csv": (load_employees, "employee_id"),
    "department_budgets.csv": (load_budgets, "department"),
    "software_catalog.csv": (load_software_catalog, "software_id"),
    "vendors.csv": (load_vendors, "vendor_id"),
    "purchase_history.csv": (load_purchase_history, "purchase_id"),
}
KNOWN_IDS = {name: set(map(str, loader()[col])) for name, (loader, col) in ID_COLUMNS.items()}


class LLMFallbackError(RuntimeError):
    """A case fell back to deterministic heuristics during an LLM evaluation run."""

    def __init__(self, case_id: str, architecture: str, error: str | None):
        self.case_id, self.architecture, self.error = case_id, architecture, error or "unknown error"
        super().__init__(f"{architecture}/{case_id}: {self.error}")

    @property
    def is_quota(self) -> bool:
        text = self.error.lower()
        return "429" in text or "resource_exhausted" in text or "quota" in text


def load_cases() -> list[dict]:
    cases = json.loads((ROOT / "evals" / "expected_outcomes.json").read_text(encoding="utf-8"))
    public = {c["case_id"]: c for c in json.loads((ROOT / "evals" / "public_cases.json").read_text(encoding="utf-8"))}
    for case in cases:
        case["public"] = public.get(case["case_id"])
        if "request" not in case:
            case["request"] = get_request(case["request_id"])
    return cases


def grounding_problems(decision) -> list[str]:
    """Every evidence item must come from a tool that actually ran and cite a record that exists."""
    problems = []
    called = set(decision.telemetry.tool_names) | {"overlap_assessment"}
    for item in decision.evidence:
        if item.source not in called:
            problems.append(f"evidence from tool that did not run: {item.source}")
        if not item.reference:
            problems.append(f"evidence without reference: {item.finding[:40]}")
            continue
        file_name, _, record_id = item.reference.partition(":")
        if file_name in KNOWN_IDS and record_id and record_id not in KNOWN_IDS[file_name]:
            problems.append(f"reference to unknown record {item.reference}")
    for tool in set(decision.telemetry.tool_names):
        if not any(e.source == tool for e in decision.evidence):
            problems.append(f"tool {tool} ran but produced no evidence")
    return problems


def policy_problems(decision, case: dict) -> list[str]:
    problems = []
    expected = set(case["expected_approvals"])
    actual = set(decision.required_approvals)
    if actual != expected:
        problems.append(f"approvals {sorted(actual)} != expected {sorted(expected)}")
    for flag in case["required_flags"]:
        if flag not in decision.risk_flags:
            problems.append(f"missing flag {flag}")
    for flag in case["forbidden_flags"]:
        if flag in decision.risk_flags:
            problems.append(f"unexpected flag {flag}")
    for text in (decision.recommendation, decision.next_step, decision.rationale or ""):
        if APPROVAL_CLAIM.search(text):
            problems.append("text claims an approval/purchase happened")
    return problems


def escalation_correct(decision, case: dict) -> bool:
    if not decision.human_review_required:
        return False
    expected_reviews = set(case["expected_approvals"]) & REVIEWS
    if not expected_reviews <= set(decision.required_approvals):
        return False
    return (case["expected_action"] == "standard_approval") == (decision.action == "standard_approval")


def run_case(case: dict, architecture: str, repeats: int) -> dict:
    """Run a single case *repeats* times; raises LLMFallbackError if the model was not actually used."""
    latencies, decision = [], None
    for _ in range(repeats):
        start = time.perf_counter()
        decision = analyze_request(dict(case["request"]), architecture)
        latencies.append((time.perf_counter() - start) * 1000)
        if decision.telemetry.mode == "llm_fallback":
            raise LLMFallbackError(case["case_id"], architecture, decision.telemetry.llm_error)

    grounding = grounding_problems(decision)
    policy = policy_problems(decision, case)
    public_failures = public_minimum_checks(decision, case["public"]["expectations"]) if case["public"] else None
    tel = decision.telemetry
    notes = grounding + policy
    if decision.action != case["expected_action"]:
        notes.insert(0, f"action {decision.action} != {case['expected_action']}")
    if public_failures:
        notes += public_failures
    return {
        "case_id": case["case_id"],
        "architecture": architecture,
        "correct_next_action": decision.action == case["expected_action"],
        "grounded_evidence": not grounding,
        "policy_followed": not policy,
        "human_escalation_correct": escalation_correct(decision, case),
        "latency_ms": round(statistics.median(latencies), 1),
        "llm_calls": tel.llm_calls,
        "tool_calls": tel.tool_calls,
        "notes": " | ".join(notes),
        "title": case["title"],
        "public_minimum_checks": "n/a" if public_failures is None else ("PASS" if not public_failures else "FAIL"),
        "mode": tel.mode,
        "action": decision.action,
        "tool_names": " > ".join(tel.tool_names),
        "llm_providers": " ".join(sorted(set(tel.llm_providers))),
    }


def api_down_check(cases: list[dict], architecture: str) -> tuple[int, int]:
    """Point the vendor client at a dead port: every case must degrade to manual review, never a clean approval.

    Runs in offline / deterministic mode (COPILOT_OFFLINE=1) so this check
    does not consume any Gemini quota.  The tested behaviour is deterministic
    failure-handling, not LLM reasoning, so the LLM is not needed here.
    """
    previous_url = os.environ.get("VENDOR_RISK_BASE_URL")
    previous_offline = os.environ.get("COPILOT_OFFLINE")
    os.environ["VENDOR_RISK_BASE_URL"] = "http://127.0.0.1:9"
    os.environ["COPILOT_OFFLINE"] = "1"  # force deterministic mode – no quota consumed
    ok = 0
    try:
        for case in cases:
            d = analyze_request(dict(case["request"]), architecture)
            if (
                "vendor_risk_unavailable" in d.risk_flags
                and "Security" in d.required_approvals
                and d.action != "standard_approval"
                and d.human_review_required
            ):
                ok += 1
    finally:
        if previous_url is None:
            os.environ.pop("VENDOR_RISK_BASE_URL", None)
        else:
            os.environ["VENDOR_RISK_BASE_URL"] = previous_url
        if previous_offline is None:
            os.environ.pop("COPILOT_OFFLINE", None)
        else:
            os.environ["COPILOT_OFFLINE"] = previous_offline
    return ok, len(cases)


def pct(rows: list[dict], key: str) -> str:
    return f"{sum(1 for r in rows if r[key])}/{len(rows)}"


def _derive_mode_label(rows: list[dict], llm_attempted: bool) -> str:
    """Determine the mode label based on what *actually* ran, not just whether the key exists.

    Rules
    -----
    - No API key / COPILOT_OFFLINE=1  -> ``offline``
    - All cases mode=="llm"           -> ``llm``
    - Any case mode=="llm_fallback"   -> ``llm_fallback (mixed)``  (NOT a valid LLM eval)
    - All cases mode=="llm_fallback"  -> ``llm_fallback``          (NOT a valid LLM eval)
    """
    if not llm_attempted:
        return "offline (no API key / COPILOT_OFFLINE=1, deterministic fallback)"
    modes = {r["mode"] for r in rows}
    providers = sorted({p for r in rows for p in r["llm_providers"].split()})
    model = ", ".join(providers) or "unknown model"
    if modes == {"llm"}:
        return f"llm ({model})"
    fallback_count = sum(1 for r in rows if r["mode"] == "llm_fallback")
    total = len(rows)
    if "llm" in modes:
        # Mixed run
        return (
            f"llm_fallback (mixed) – {model} attempted but "
            f"{fallback_count}/{total} case(s) used deterministic fallback; "
            "results are NOT a valid pure-LLM evaluation"
        )
    # Every case fell back
    return (
        f"llm_fallback – {model} attempted but every case used "
        "deterministic fallback; results are NOT a valid LLM evaluation"
    )


def summarise(rows: list[dict], api_down: dict, mode_label: str) -> str:
    lines = [
        "# Architecture comparison",
        "",
        f"Mode: **{mode_label}**. Same {len(rows) // len(ARCHITECTURES)} cases for both architectures "
        "(6 public, 4 remaining dataset requests, 5 synthetic hidden-style requests).",
        "",
        "| Metric | Single agent (A) | Staged / 2-agent (B) |",
        "|---|---:|---:|",
    ]
    by_arch = {a: [r for r in rows if r["architecture"] == a] for a in ARCHITECTURES}

    def row(label: str, fn) -> None:
        lines.append(f"| {label} | {fn(by_arch['single'])} | {fn(by_arch['staged'])} |")

    row("Public cases passing minimum checks", lambda rs: pct([r for r in rs if r["public_minimum_checks"] != "n/a"], "public_ok"))
    row("Correct next action", lambda rs: pct(rs, "correct_next_action"))
    row("Evidence grounded in tool results", lambda rs: pct(rs, "grounded_evidence"))
    row("Policy / deterministic rules followed", lambda rs: pct(rs, "policy_followed"))
    row("Human escalation correct", lambda rs: pct(rs, "human_escalation_correct"))
    row("Vendor API down: safe degradation", lambda rs: "{}/{}".format(*api_down[rs[0]["architecture"]]))
    row("Avg latency (ms)", lambda rs: f"{statistics.mean(r['latency_ms'] for r in rs):.1f}")
    row("p95 latency (ms)", lambda rs: f"{sorted(r['latency_ms'] for r in rs)[int(0.95 * (len(rs) - 1))]:.1f}")
    row("Avg LLM calls", lambda rs: f"{statistics.mean(r['llm_calls'] for r in rs):.2f}")
    row("Avg tool calls", lambda rs: f"{statistics.mean(r['tool_calls'] for r in rs):.2f}")
    lines += ["", "## Per-case results", "",
              "| Case | Arch | Action | Correct | Grounded | Policy | Escalation | ms | LLM | Tools | Mode | Notes |",
              "|---|---|---|---|---|---|---|---:|---:|---:|---|---|"]
    yes = {True: "yes", False: "**no**"}
    for r in rows:
        lines.append(
            f"| {r['case_id']} | {r['architecture']} | {r['action']} | {yes[r['correct_next_action']]} | "
            f"{yes[r['grounded_evidence']]} | {yes[r['policy_followed']]} | {yes[r['human_escalation_correct']]} | "
            f"{r['latency_ms']} | {r['llm_calls']} | {r['tool_calls']} | {r['mode']} | {r['notes'] or ''} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=1,
                        help="runs per case for latency median (default: 1 to conserve quota)")
    parser.add_argument("--arch", choices=["single", "staged", "both"], default="both")
    parser.add_argument("--cases", default="", help="comma-separated case ids (smoke test; results are not written)")
    parser.add_argument("--delay", type=float, default=0.0, help="seconds to wait between cases")
    parser.add_argument("--provider", choices=["gemini", "groq", "auto"],
                        help="LLM provider for the whole run (default: the primary configured provider; "
                             "'auto' allows fallback but the run still stops if providers get mixed)")
    parser.add_argument("--rate-limit-wait", type=float, default=65.0,
                        help="max seconds to wait on a 429 with a short Retry-After before giving up")
    args = parser.parse_args()

    if llm_enabled():
        available = [p.name for p in configured_providers()]
        provider = args.provider or available[0]
        if provider != "auto" and provider not in available:
            sys.exit(f"Provider '{provider}' is not configured (configured: {', '.join(available)})")
        os.environ["LLM_PROVIDER"] = provider
        os.environ["LLM_RATE_LIMIT_WAIT"] = str(args.rate_limit_wait)
    llm_attempted = llm_enabled()
    architectures = ARCHITECTURES if args.arch == "both" else [args.arch]
    wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
    cases = [c for c in load_cases() if not wanted or c["case_id"] in wanted]
    unknown = wanted - {c["case_id"] for c in cases}
    if unknown:
        sys.exit(f"Unknown case id(s): {', '.join(sorted(unknown))}")
    full_run = not wanted and args.arch == "both"

    if llm_attempted:
        chain = " -> ".join(p.label for p in configured_providers())
        print(f"Comparing architectures - LLM mode ({chain}), repeats={args.repeats}")
        print("  The run stops at the first case that falls back to offline heuristics.")
    else:
        print("Comparing architectures - offline mode (deterministic fallback)")
    if not full_run:
        print("  Smoke test: results are printed only, nothing is written.")

    rows, api_down = [], {}
    run_providers: set[str] = set()
    with mock_api_running():
        for arch in architectures:
            for case in cases:
                try:
                    result = run_case(case, arch, args.repeats)
                except LLMFallbackError as exc:
                    kind = "QUOTA EXHAUSTED" if exc.is_quota else "MODEL CALL FAILED"
                    print()
                    print(f"[{kind}] {exc.architecture}/{exc.case_id}")
                    print(f"  {exc.error}")
                    print()
                    if exc.is_quota:
                        print("Wait for the quota to reset, use --delay for per-minute limits, or enable billing.")
                    else:
                        print("Fix the model/request problem above before spending quota on a full run.")
                    print("Aborted. No results written.")
                    sys.exit(1)
                run_providers.update(result["llm_providers"].split())
                if len(run_providers) > 1:
                    print()
                    print(f"[MIXED PROVIDERS] {arch}/{case['case_id']} used {result['llm_providers']}; "
                          f"run so far used {', '.join(sorted(run_providers))}.")
                    print("A comparison across different models is not fair. Re-run pinned, e.g. --provider groq.")
                    print("Aborted. No results written.")
                    sys.exit(1)
                result["public_ok"] = result["public_minimum_checks"] == "PASS"
                rows.append(result)
                status = "ok " if result["correct_next_action"] and result["policy_followed"] and result["grounded_evidence"] else "BAD"
                print(f"  {status} {arch:<6} {case['case_id']:<7} {result['action']:<24} {result['latency_ms']:>8} ms  "
                      f"llm={result['llm_calls']} tools={result['tool_calls']}  mode={result['mode']}  "
                      f"{result['llm_providers']}  {result['notes']}")
                if llm_attempted and arch == "single":
                    print(f"      tools: {result['tool_names']}")
                if args.delay:
                    time.sleep(args.delay)
            if full_run:
                api_down[arch] = api_down_check(cases, arch)

    if not full_run:
        return

    mode_label = _derive_mode_label(rows, llm_attempted)
    RESULTS_DIR.mkdir(exist_ok=True)
    columns = ["case_id", "architecture", "correct_next_action", "grounded_evidence", "policy_followed",
               "human_escalation_correct", "latency_ms", "llm_calls", "tool_calls", "notes", "title",
               "public_minimum_checks", "mode", "action", "llm_providers"]
    with (RESULTS_DIR / "comparison.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    summary = summarise(rows, api_down, mode_label)
    (RESULTS_DIR / "summary.md").write_text(summary, encoding="utf-8")
    print()
    print(summary.split("## Per-case")[0])
    print(f"Written: {(RESULTS_DIR / 'comparison.csv').relative_to(ROOT)}, {(RESULTS_DIR / 'summary.md').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
