from __future__ import annotations

import os

import pytest

os.environ["COPILOT_OFFLINE"] = "1"
os.environ["VENDOR_RISK_BASE_URL"] = "http://127.0.0.1:8011"


@pytest.fixture(scope="session")
def vendor_api():
    from src.mock_server import mock_api_running

    with mock_api_running() as url:
        yield url
