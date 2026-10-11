from __future__ import annotations

import csv
import json
import os

import pytest

from evals import compare
from src import single_agent, staged_agents
from src.llm import LLMError, Provider

FINAL = json.dumps({"need_summary": "x", "overlap_assessment": "expansion",
                    "overlap_reason": "Extends an existing licence.", "rationale": "Follows the policy engine."})


class Message:
    tool_calls = None
    content = FINAL


class CountingLLM:
    """Answers immediately; raises a quota error once `fail_after` calls have been made."""

    calls = 0
    fail_after: int | None = None

    def __init__(self):
        self.last_used = "groq:test-model"

    def chat(self, messages, tools=None):
        if CountingLLM.fail_after is not None and CountingLLM.calls >= CountingLLM.fail_after:
            raise LLMError("RateLimitError: Error code: 429 - quota exceeded", 429)
        CountingLLM.calls += 1
        return Message()


@pytest.fixture
def live_eval(vendor_api, monkeypatch, tmp_path):
    CountingLLM.calls, CountingLLM.fail_after = 0, None
    monkeypatch.setenv("COPILOT_OFFLINE", "")

    def enabled() -> bool:
        return os.environ.get("COPILOT_OFFLINE") != "1"

    for module in (single_agent, staged_agents):
        monkeypatch.setattr(module, "LLMClient", CountingLLM)
        monkeypatch.setattr(module, "llm_enabled", enabled)
    monkeypatch.setattr(compare, "llm_enabled", enabled)
    monkeypatch.setattr(compare, "configured_providers", lambda: [Provider("groq", "k", "u", "test-model")])
    monkeypatch.setattr(compare, "RESULTS_DIR", tmp_path)
    for name in ("LLM_PROVIDER", "LLM_RATE_LIMIT_WAIT", "LLM_MIN_CALL_INTERVAL"):
        monkeypatch.setenv(name, "")
    return tmp_path


def run(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["compare.py", "--provider", "groq", *args])
    compare.main()


def test_interrupted_live_run_keeps_completed_cases_and_resumes(live_eval, monkeypatch, capsys):
    CountingLLM.fail_after = 5  # single agent uses one call per case here
    with pytest.raises(SystemExit):
        run(monkeypatch)
    out = capsys.readouterr().out
    assert "QUOTA EXHAUSTED" in out and "--resume" in out

    with (live_eval / compare.LIVE_PROGRESS).open(newline="", encoding="utf-8") as f:
        saved = list(csv.DictReader(f))
    assert [r["case_id"] for r in saved] == ["PUB-01", "PUB-02", "PUB-03", "PUB-04", "PUB-05"]
    assert {r["mode"] for r in saved} == {"llm"}
    assert not (live_eval / "summary_live.md").exists()

    CountingLLM.calls, CountingLLM.fail_after = 0, None
    run(monkeypatch, "--resume")
    out = capsys.readouterr().out
    assert out.count("already completed, skipped") == 5
    summary = (live_eval / "summary_live.md").read_text(encoding="utf-8")
    assert "Mode: **llm (groq:test-model)**" in summary
    assert not (live_eval / "summary_offline.md").exists()
    with (live_eval / "comparison_live.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 30
    # 10 remaining single cases + 15 staged cases x 2 calls; the API-down pass runs offline
    assert CountingLLM.calls == 10 + 30


def test_resume_refuses_results_from_another_model(live_eval, monkeypatch):
    path = live_eval / compare.LIVE_PROGRESS
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=compare.COLUMNS)
        writer.writeheader()
        writer.writerow({c: "" for c in compare.COLUMNS} | {"case_id": "PUB-01", "architecture": "single",
                                                            "llm_providers": "gemini:gemini-3.8-flash"})
    with pytest.raises(SystemExit) as err:
        run(monkeypatch, "--resume")
    assert "gemini:gemini-3.8-flash" in str(err.value)


def test_fresh_live_run_does_not_reuse_old_progress(live_eval, monkeypatch):
    CountingLLM.fail_after = 2
    with pytest.raises(SystemExit):
        run(monkeypatch)
    CountingLLM.calls, CountingLLM.fail_after = 0, 1
    with pytest.raises(SystemExit):
        run(monkeypatch)
    with (live_eval / compare.LIVE_PROGRESS).open(newline="", encoding="utf-8") as f:
        assert [r["case_id"] for r in csv.DictReader(f)] == ["PUB-01"]


def test_offline_run_writes_only_offline_files(vendor_api, monkeypatch, tmp_path):
    monkeypatch.setattr(compare, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["compare.py"])
    compare.main()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["comparison_offline.csv", "summary_offline.md"]
    assert "offline" in (tmp_path / "summary_offline.md").read_text(encoding="utf-8").splitlines()[2]
