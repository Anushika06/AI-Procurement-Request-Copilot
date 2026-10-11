"""Run the same case set against both architectures and write a comparison.

    python evals/compare.py                                  # LLM mode if GEMINI_API_KEY is set, else offline
    python evals/compare.py --arch single --cases PUB-01     # smoke test, nothing written
    python evals/compare.py --delay 5                        # pause between cases (per-minute rate limits)
    python evals/compare.py --provider groq --rpm 3          # pin the provider, space out calls
    python evals/compare.py --provider groq --resume         # continue an interrupted live run

A full offline run writes evals/results/summary_offline.md and comparison_offline.csv.
A full LLM run writes summary_live.md and comparison_live.csv, so live results never
replace the offline baseline. LLM runs are pinned to one provider (the primary one by
default) so both architectures are compared on the same model, and stop at the first
case that falls back to the offline heuristics or that used a different provider.
Each completed live case is saved to live_progress.csv as it finishes, so an
interrupted run (e.g. quota exhausted) can be continued with --resume.
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
LIVE_PROGRESS = "live_progress.csv"
LIVE_PREVIOUS = "live_progress_previous.csv"
COLUMNS = ["case_id", "architecture", "correct_next_action", "grounded_evidence", "policy_followed",
           "human_escalation_correct", "latency_ms", "llm_calls", "tool_calls", "notes", "title",
           "public_minimum_checks", "mode", "action", "llm_providers"]
BOOL_COLUMNS = ("correct_next_action", "grounded_evidence", "policy_followed", "human_escalation_correct")
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


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_progress(provider_label: str) -> dict[tuple[str, str], dict]:
    """Completed live rows from an earlier run on the same provider:model."""
    path = RESULTS_DIR / LIVE_PROGRESS
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    other = {r["llm_providers"] for r in rows} - {provider_label}
    if other:
        sys.exit(f"{_rel(path)} holds results from {', '.join(sorted(other))}, not {provider_label}. "
                 "Resume with the same provider, or start a fresh run without --resume.")
    done = {}
    for r in rows:
        for col in BOOL_COLUMNS:
            r[col] = r[col] == "True"
        r["latency_ms"] = float(r["latency_ms"])
        r["llm_calls"], r["tool_calls"] = int(r["llm_calls"]), int(r["tool_calls"])
        r["public_ok"] = r["public_minimum_checks"] == "PASS"
        done[(r["architecture"], r["case_id"])] = r
    return done


def save_progress_row(row: dict, fresh: bool) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / LIVE_PROGRESS
    with path.open("w" if fresh else "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        if fresh:
            writer.writeheader()
        writer.writerow(row)


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
                        help="max seconds to wait on a 429 with a short retry delay before giving up")
    parser.add_argument("--rpm", type=float, default=0.0,
                        help="max LLM calls per minute (spaces calls evenly; 0 = no limit)")
    parser.add_argument("--resume", action="store_true",
                        help="skip cases already completed in evals/results/live_progress.csv (same provider)")
    args = parser.parse_args()

    if llm_enabled():
        available = [p.name for p in configured_providers()]
        provider = args.provider or available[0]
        if provider != "auto" and provider not in available:
            sys.exit(f"Provider '{provider}' is not configured (configured: {', '.join(available)})")
        os.environ["LLM_PROVIDER"] = provider
        os.environ["LLM_RATE_LIMIT_WAIT"] = str(args.rate_limit_wait)
        if args.rpm > 0:
            os.environ["LLM_MIN_CALL_INTERVAL"] = str(60.0 / args.rpm)
    llm_attempted = llm_enabled()
    architectures = ARCHITECTURES if args.arch == "both" else [args.arch]
    wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
    cases = [c for c in load_cases() if not wanted or c["case_id"] in wanted]
    unknown = wanted - {c["case_id"] for c in cases}
    if unknown:
        sys.exit(f"Unknown case id(s): {', '.join(sorted(unknown))}")
    full_run = not wanted and args.arch == "both"
    track_progress = llm_attempted and full_run

    done: dict[tuple[str, str], dict] = {}
    if args.resume:
        if not track_progress or os.environ.get("LLM_PROVIDER") == "auto":
            sys.exit("--resume applies to full LLM runs pinned to one provider.")
        done = load_progress(configured_providers()[0].label)
        print(f"Resuming: {len(done)} completed case(s) loaded from {LIVE_PROGRESS}.")
    elif track_progress and (RESULTS_DIR / LIVE_PROGRESS).exists():
        # A fresh run must never be resumed onto rows from an earlier run (older code or prompts),
        # even if it fails before its first case completes. Keep the old file aside instead.
        previous = RESULTS_DIR / LIVE_PREVIOUS
        (RESULTS_DIR / LIVE_PROGRESS).replace(previous)
        print(f"Fresh run: earlier progress moved to {_rel(previous)}.")

    if llm_attempted:
        chain = " -> ".join(p.label for p in configured_providers())
        print(f"Comparing architectures - LLM mode ({chain}), repeats={args.repeats}")
        print("  The run stops at the first case that falls back to offline heuristics.")
    else:
        print("Comparing architectures - offline mode (deterministic fallback)")
    if not full_run:
        print("  Smoke test: results are printed only, nothing is written.")

    rows, api_down = [], {}
    run_providers: set[str] = {r["llm_providers"] for r in done.values()}
    fresh_progress = not args.resume

    def stop(message: str) -> None:
        print(message)
        if track_progress and rows:
            print(f"{len(rows)} completed case(s) are saved in {_rel(RESULTS_DIR / LIVE_PROGRESS)}; "
                  "re-run with the same --provider and --resume to continue.")
        print("Aborted. No summary written.")
        sys.exit(1)

    with mock_api_running():
        for arch in architectures:
            for case in cases:
                if (arch, case["case_id"]) in done:
                    rows.append(done[(arch, case["case_id"])])
                    print(f"  --- {arch:<6} {case['case_id']:<7} already completed, skipped")
                    continue
                try:
                    result = run_case(case, arch, args.repeats)
                except LLMFallbackError as exc:
                    kind = "QUOTA EXHAUSTED" if exc.is_quota else "MODEL CALL FAILED"
                    print()
                    print(f"[{kind}] {exc.architecture}/{exc.case_id}")
                    print(f"  {exc.error}")
                    print()
                    if exc.is_quota:
                        stop("Wait for the quota to reset, lower --rpm / raise --delay for per-minute limits, "
                             "or enable billing.")
                    stop("Fix the model/request problem above before spending quota on a full run.")
                run_providers.update(result["llm_providers"].split())
                if len(run_providers) > 1:
                    print()
                    print(f"[MIXED PROVIDERS] {arch}/{case['case_id']} used {result['llm_providers']}; "
                          f"run so far used {', '.join(sorted(run_providers))}.")
                    stop("A comparison across different models is not fair. Re-run pinned, e.g. --provider groq.")
                result["public_ok"] = result["public_minimum_checks"] == "PASS"
                rows.append(result)
                if track_progress:
                    save_progress_row(result, fresh_progress)
                    fresh_progress = False
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
    suffix = "live" if llm_attempted else "offline"
    csv_path, md_path = RESULTS_DIR / f"comparison_{suffix}.csv", RESULTS_DIR / f"summary_{suffix}.md"
    RESULTS_DIR.mkdir(exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    summary = summarise(rows, api_down, mode_label)
    md_path.write_text(summary, encoding="utf-8")
    print()
    print(summary.split("## Per-case")[0])
    print(f"Written: {_rel(csv_path)}, {_rel(md_path)}")


if __name__ == "__main__":
    main()
