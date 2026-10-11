from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RunTelemetryCounter:
    """Optional helper. Use it or replace it with your own observability."""
    llm_calls: int = 0
    tool_calls: int = 0
    tool_names: list[str] = field(default_factory=list)
    llm_providers: list[str] = field(default_factory=list)

    def record_llm_call(self, provider: str | None = None) -> None:
        self.llm_calls += 1
        if provider:
            self.llm_providers.append(provider)

    def record_tool_call(self, name: str) -> None:
        self.tool_calls += 1
        self.tool_names.append(name)
