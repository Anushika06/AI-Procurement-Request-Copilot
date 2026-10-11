"""LLM access through OpenAI-compatible chat endpoints.

Gemini is the primary provider and Groq is an optional fallback. Both are
called with the `openai` SDK. LLM_PROVIDER pins one provider (no fallback),
which is what evaluation runs use so both architectures see the same model.
Without any key (or with COPILOT_OFFLINE=1) the agents use their
deterministic fallback instead.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"

TRANSIENT_STATUS = {500, 502, 503, 504}
RETRY_DELAYS_SECONDS = (2.0, 5.0)
QUOTA_COOLDOWN_SECONDS = 600.0
TRANSIENT_COOLDOWN_SECONDS = 30.0

# Providers that recently failed with a quota/availability error are skipped
# until this monotonic time, so an exhausted quota is not hit on every call.
_cooldown_until: dict[str, float] = {}
_last_call_at: dict[str, float] = {}
DAILY_QUOTA_COOLDOWN_SECONDS = 3600.0


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None,
                 can_fall_back: bool = False):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.can_fall_back = can_fall_back

    @property
    def is_quota(self) -> bool:
        text = str(self).lower()
        return self.status == 429 or "resource_exhausted" in text or "quota" in text


@dataclass(frozen=True)
class Provider:
    name: str
    api_key: str
    base_url: str
    model: str
    reasoning_effort: str | None = None

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model}"


def _offline() -> bool:
    return os.getenv("COPILOT_OFFLINE", "").lower() in {"1", "true", "yes"}


def configured_providers() -> list[Provider]:
    """Providers in the order they are tried, honouring LLM_PROVIDER=auto|gemini|groq."""
    providers = []
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("LLM_API_KEY")
    if gemini_key:
        providers.append(Provider(
            "gemini", gemini_key, os.getenv("LLM_BASE_URL") or GEMINI_BASE_URL,
            os.getenv("LLM_MODEL") or DEFAULT_MODEL, os.getenv("LLM_REASONING_EFFORT") or None,
        ))
    groq_key = os.getenv("GROQ_API_KEY")
    if groq_key:
        providers.append(Provider(
            "groq", groq_key, os.getenv("GROQ_BASE_URL") or GROQ_BASE_URL,
            os.getenv("GROQ_MODEL") or DEFAULT_GROQ_MODEL, os.getenv("GROQ_REASONING_EFFORT") or None,
        ))
    choice = (os.getenv("LLM_PROVIDER") or "auto").strip().lower()
    if choice != "auto":
        providers = [p for p in providers if p.name == choice]
    return providers


def llm_enabled() -> bool:
    return bool(configured_providers()) and not _offline()


def model_name() -> str:
    """Primary model id (the first provider that will be tried)."""
    providers = configured_providers()
    return providers[0].model if providers else (os.getenv("LLM_MODEL") or DEFAULT_MODEL)


def provider_chain() -> str:
    providers = configured_providers()
    return " -> ".join(p.label for p in providers) if providers else "none"


def reset_provider_state() -> None:
    _cooldown_until.clear()
    _last_call_at.clear()


def _error_text(exc) -> str:
    try:
        return exc.response.text or str(exc)
    except Exception:
        return str(exc)


def _retry_after(exc) -> float | None:
    """Seconds the provider asks us to wait: Groq sends a Retry-After header,
    Gemini puts RetryInfo.retryDelay ("43s") or "retry in 43.2s" in the body."""
    try:
        value = exc.response.headers.get("retry-after")
        if value is not None:
            return float(value)
    except (AttributeError, TypeError, ValueError):
        pass
    text = _error_text(exc)
    for pattern in (r'"retryDelay"\s*:\s*"([\d.]+)s"', r"retry in ([\d.]+)\s*s"):
        match = re.search(pattern, text, re.I)
        if match:
            return float(match.group(1))
    return None


def _is_daily_quota(exc) -> bool:
    text = _error_text(exc).lower()
    return "perday" in text or "per day" in text or "requests per day" in text


def _throttle(provider_name: str) -> None:
    """Optional client-side spacing between calls (LLM_MIN_CALL_INTERVAL seconds),
    used by evaluation runs to stay under per-minute request/token limits."""
    interval = float(os.getenv("LLM_MIN_CALL_INTERVAL") or 0)
    if interval > 0:
        wait = _last_call_at.get(provider_name, 0.0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    _last_call_at[provider_name] = time.monotonic()


def _messages_for(provider: Provider, messages: list[dict]) -> list[dict]:
    """Gemini gets the history as returned (keeps thought signatures); other
    providers get only standard OpenAI fields, since strict endpoints reject
    unknown ones such as extra_content or reasoning."""
    if provider.name == "gemini":
        return messages
    cleaned = []
    for m in messages:
        if m.get("role") == "assistant":
            out = {"role": "assistant"}
            if m.get("content") is not None:
                out["content"] = m["content"]
            if m.get("tool_calls"):
                out["tool_calls"] = [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["function"]["name"], "arguments": c["function"].get("arguments") or "{}"}}
                    for c in m["tool_calls"]
                ]
            cleaned.append(out)
        elif m.get("role") == "tool":
            cleaned.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            cleaned.append({"role": m["role"], "content": m["content"]})
    return cleaned


class LLMClient:
    def __init__(self, http_client=None) -> None:
        self.providers = configured_providers()
        if not self.providers:
            raise LLMError("No LLM provider configured")
        self.http_client = http_client
        self.model = self.providers[0].model
        self.last_used: str | None = None
        self._clients: dict[str, object] = {}
        # Seconds the client may wait on a 429 that carries a short Retry-After
        # (per-minute limits). 0 in the app; evaluation runs raise it.
        self.max_rate_wait = float(os.getenv("LLM_RATE_LIMIT_WAIT") or 0)

    def _client(self, provider: Provider):
        if provider.name not in self._clients:
            from openai import OpenAI

            # SDK retries are off: retry policy is handled in _call.
            self._clients[provider.name] = OpenAI(
                api_key=provider.api_key,
                base_url=provider.base_url,
                timeout=float(os.getenv("LLM_TIMEOUT_SECONDS") or 60),
                max_retries=0,
                http_client=self.http_client,
            )
        return self._clients[provider.name]

    def chat(self, messages: list[dict], tools: list[dict] | None = None):
        now = time.monotonic()
        available = [p for p in self.providers if _cooldown_until.get(p.name, 0.0) <= now]
        if not available:
            raise LLMError("All LLM providers are cooling down after quota/availability errors", status=429)

        failures: list[str] = []
        last: LLMError | None = None
        for provider in available:
            try:
                message = self._call(provider, messages, tools)
            except LLMError as exc:
                if not exc.can_fall_back:
                    raise
                cooldown = exc.retry_after or (QUOTA_COOLDOWN_SECONDS if exc.is_quota else TRANSIENT_COOLDOWN_SECONDS)
                _cooldown_until[provider.name] = time.monotonic() + cooldown
                failures.append(f"{provider.label}: {exc}")
                last = exc
                continue
            self.last_used = provider.label
            return message
        raise LLMError(" | ".join(failures), status=last.status if last else None)

    def _call(self, provider: Provider, messages: list[dict], tools: list[dict] | None):
        from openai import APIConnectionError, APIStatusError, APITimeoutError

        kwargs = {
            "model": provider.model,
            "messages": _messages_for(provider, messages),
            "temperature": float(os.getenv("LLM_TEMPERATURE") or 0),
        }
        if tools:
            if provider.name == "groq":
                # gpt-oss models on Groq sometimes answer with a call to an undeclared "json" tool,
                # which Groq rejects with HTTP 400 (tool_use_failed). Declaring it lets the reply
                # through; it is converted back into plain content below and never executed.
                kwargs["tools"] = list(tools) + [{
                    "type": "function",
                    "function": {"name": "json", "description": "Return the final JSON output."}
                }]
            else:
                kwargs["tools"] = tools
        if provider.reasoning_effort:
            kwargs["reasoning_effort"] = provider.reasoning_effort

        transient_attempt = rate_waits = 0
        while True:
            try:
                _throttle(provider.name)
                response = self._client(provider).chat.completions.create(**kwargs)
                break
            except APIStatusError as exc:
                status = exc.status_code
                if status == 429:
                    daily = _is_daily_quota(exc)
                    wait = None if daily else _retry_after(exc)
                    if wait is not None and wait <= self.max_rate_wait and rate_waits < 3:
                        rate_waits += 1
                        time.sleep(wait)
                        continue
                    cooldown = DAILY_QUOTA_COOLDOWN_SECONDS if daily else wait
                    raise LLMError(f"{type(exc).__name__}: {exc}", 429, cooldown, can_fall_back=True) from exc
                if status in TRANSIENT_STATUS:
                    if transient_attempt < len(RETRY_DELAYS_SECONDS):
                        time.sleep(RETRY_DELAYS_SECONDS[transient_attempt])
                        transient_attempt += 1
                        continue
                    raise LLMError(f"{type(exc).__name__}: {exc}", status, can_fall_back=True) from exc
                # 400/401/403/404 point at a request or configuration problem; don't hide it behind a fallback.
                raise LLMError(f"{type(exc).__name__}: {exc}", status) from exc
            except (APIConnectionError, APITimeoutError) as exc:
                if transient_attempt < len(RETRY_DELAYS_SECONDS):
                    time.sleep(RETRY_DELAYS_SECONDS[transient_attempt])
                    transient_attempt += 1
                    continue
                raise LLMError(f"{type(exc).__name__}: {exc}", can_fall_back=True) from exc
            except Exception as exc:
                raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        if not response.choices:
            raise LLMError("Empty response from model")
        message = response.choices[0].message

        # Normalise the declared "json" pseudo-tool back into a plain reply so the
        # agent loop behaves the same on every provider.
        if provider.name == "groq" and message.tool_calls:
            real = [c for c in message.tool_calls if c.function.name != "json"]
            json_calls = [c for c in message.tool_calls if c.function.name == "json"]
            if json_calls and not real:
                from openai.types.chat import ChatCompletionMessage
                message = ChatCompletionMessage(
                    role="assistant",
                    content=json_calls[0].function.arguments or "",
                    tool_calls=None,
                )
            elif json_calls and real:
                from openai.types.chat import ChatCompletionMessage
                message = ChatCompletionMessage(
                    role="assistant",
                    content=None,
                    tool_calls=real,
                )

        return message


def assistant_turn(message) -> dict:
    """Assistant message to send back on the next turn.

    The message is replayed as returned (not rebuilt) so provider-specific
    fields survive, e.g. Gemini's thought signatures in
    tool_calls[].extra_content, which Gemini 3 models require when tool
    calling spans several turns.
    """
    turn = message.model_dump(exclude_none=True)
    turn["role"] = "assistant"
    return turn


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
