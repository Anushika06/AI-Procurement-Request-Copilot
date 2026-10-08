"""Architecture A: one tool-calling agent."""
from __future__ import annotations

import json

from src.contracts import ProcurementDecision
from src.decision import build_decision, heuristic_judgement
from src.llm import LLMClient, LLMError, llm_enabled, parse_json
from src.prompts import request_block, single_agent_system
from src.telemetry import RunTelemetryCounter
from src.tools import TOOL_SCHEMAS, ToolBox

MAX_TURNS = 8


def run_single_agent(request: dict) -> ProcurementDecision:
    telemetry = RunTelemetryCounter()
    toolbox = ToolBox(request, telemetry)

    if not llm_enabled():
        toolbox.gather_all()
        return build_decision(request, toolbox, heuristic_judgement(request, toolbox), telemetry, "offline")

    try:
        judgement = _agent_loop(request, toolbox, telemetry)
        mode = "llm"
    except LLMError as exc:
        judgement, mode = None, "llm_fallback"
        error = str(exc)

    # Code guard: the deterministic checks run even if the agent skipped tools.
    toolbox.gather_all()
    if judgement is None:
        judgement = heuristic_judgement(request, toolbox)
        judgement["rationale"] = f"Model unavailable ({error[:120]}); deterministic fallback used."
    return build_decision(request, toolbox, judgement, telemetry, mode)


def _agent_loop(request: dict, toolbox: ToolBox, telemetry: RunTelemetryCounter) -> dict:
    llm = LLMClient()
    messages = [
        {"role": "system", "content": single_agent_system()},
        {"role": "user", "content": f"Review this purchase request.\n{request_block(request)}"},
    ]
    for _ in range(MAX_TURNS):
        message = llm.chat(messages, tools=TOOL_SCHEMAS)
        telemetry.record_llm_call()
        if not message.tool_calls:
            return parse_json(message.content)
        messages.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                for c in message.tool_calls
            ],
        })
        for call in message.tool_calls:
            result = toolbox.call(call.function.name)
            content = toolbox.for_llm(call.function.name) if "error" not in result else json.dumps(result)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
    raise LLMError(f"Agent did not finish within {MAX_TURNS} turns")
