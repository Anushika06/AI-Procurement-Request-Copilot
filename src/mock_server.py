"""Start the mock vendor-risk API for scripts that need it (evals, tests)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]


def base_url() -> str:
    return os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/")


def is_up(url: str | None = None) -> bool:
    try:
        return requests.get(f"{url or base_url()}/health", timeout=0.5).ok
    except requests.RequestException:
        return False


@contextmanager
def mock_api_running(timeout_seconds: float = 15.0):
    """Reuse an already running mock API, otherwise start one for the duration of the block."""
    if is_up():
        yield base_url()
        return
    parsed = urlparse(base_url())
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mock_api.app:app", "--host", parsed.hostname or "127.0.0.1",
         "--port", str(parsed.port or 8001), "--log-level", "warning"],
        cwd=ROOT,
    )
    try:
        deadline = time.monotonic() + timeout_seconds
        while not is_up():
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"Could not start the mock vendor-risk API at {base_url()}")
            time.sleep(0.2)
        yield base_url()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
