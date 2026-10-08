"""Thin wrapper around Gemini's OpenAI-compatible chat endpoint.

Any OpenAI-compatible provider works by changing LLM_BASE_URL / LLM_MODEL.
Without an API key (or with COPILOT_OFFLINE=1) the copilot runs in offline
mode and the agents fall back to deterministic heuristics.
"""
from __future__ import annotations

import json
import os
import re

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODEL = "gemini-2.5-flash"


class LLMError(RuntimeError):
    pass


def _api_key() -> str | None:
    return os.getenv("GEMINI_API_KEY") or os.getenv("LLM_API_KEY")


def llm_enabled() -> bool:
    return bool(_api_key()) and os.getenv("COPILOT_OFFLINE", "").lower() not in {"1", "true", "yes"}


def model_name() -> str:
    return os.getenv("LLM_MODEL", DEFAULT_MODEL)


class LLMClient:
    def __init__(self) -> None:
        from openai import OpenAI

        self.model = model_name()
        self.client = OpenAI(
            api_key=_api_key(),
            base_url=os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL),
            timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "45")),
            max_retries=0,  # never retry quota (429) errors; let compare.py handle them explicitly
        )

    def chat(self, messages: list[dict], tools: list[dict] | None = None):
        kwargs = {"model": self.model, "messages": messages, "temperature": 0}
        if tools:
            kwargs["tools"] = tools
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as exc:  # network, auth, quota and provider errors all degrade the same way
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        if not response.choices:
            raise LLMError("Empty response from model")
        return response.choices[0].message


def parse_json(text: str | None) -> dict:
    """Extract the first JSON object from a model reply (tolerates ``` fences)."""
    if not text:
        raise LLMError("Model returned no content")
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise LLMError("Model reply did not contain a JSON object")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"Invalid JSON from model: {exc}") from exc
