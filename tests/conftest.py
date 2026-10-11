from __future__ import annotations

import os

import pytest

os.environ["COPILOT_OFFLINE"] = "1"
os.environ["VENDOR_RISK_BASE_URL"] = "http://127.0.0.1:8011"
# Empty values keep a developer's .env (loaded by src/__init__) out of the tests.
for name in (
    "GEMINI_API_KEY", "LLM_API_KEY", "GROQ_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "GROQ_MODEL",
    "LLM_BASE_URL", "GROQ_BASE_URL", "LLM_REASONING_EFFORT", "GROQ_REASONING_EFFORT",
    "LLM_RATE_LIMIT_WAIT", "LLM_TEMPERATURE",
):
    os.environ[name] = ""


@pytest.fixture(scope="session")
def vendor_api():
    from src.mock_server import mock_api_running

    with mock_api_running() as url:
        yield url


@pytest.fixture(autouse=True)
def fresh_provider_state():
    from src.llm import reset_provider_state

    reset_provider_state()
    yield
    reset_provider_state()
