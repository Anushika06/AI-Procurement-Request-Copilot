from __future__ import annotations

import pytest

from evals import compare
from src import single_agent, staged_agents
from src.llm import LLMError


@pytest.mark.parametrize("architecture, module", [("single", single_agent), ("staged", staged_agents)])
def test_eval_run_stops_on_any_model_fallback(vendor_api, monkeypatch, architecture, module):
    class FailingLLM:
        def chat(self, messages, tools=None):
            raise LLMError("ServiceUnavailableError: Error code: 503 - high demand", 503)

    monkeypatch.setattr(module, "LLMClient", FailingLLM)
    monkeypatch.setattr(module, "llm_enabled", lambda: True)
    case = next(c for c in compare.load_cases() if c["case_id"] == "PUB-01")

    with pytest.raises(compare.LLMFallbackError) as err:
        compare.run_case(case, architecture, repeats=1)
    assert "503" in err.value.error
    assert not err.value.is_quota


def test_offline_case_is_scored(vendor_api):
    case = next(c for c in compare.load_cases() if c["case_id"] == "PUB-05")
    row = compare.run_case(case, "single", repeats=1)
    assert row["mode"] == "offline"
    assert row["correct_next_action"] and row["policy_followed"] and row["grounded_evidence"]
