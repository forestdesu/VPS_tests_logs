"""
Фикстура `api_get` — обёртка над requests.get,
которая сохраняет каждый запрос и ответ в api_responses.json.
"""
import json
import time
from pathlib import Path

import pytest
import requests


BASE_URL = "http://2.26.10.175:8000"
TIMEOUT = 10

JSON_FILE = Path(__file__).resolve().parent / "api_responses.json"

_log: list[dict] = []


def pytest_sessionstart(session):
    _log.clear()


def pytest_sessionfinish(session, exitstatus):
    JSON_FILE.write_text(
        json.dumps(_log, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


@pytest.fixture
def api_get():
    def _get(path: str, **kwargs) -> requests.Response:
        url = f"{BASE_URL}{path}"
        start = time.perf_counter()
        try:
            r = requests.get(url, timeout=TIMEOUT, **kwargs)
            elapsed = time.perf_counter() - start
        except Exception as e:
            _log.append({
                "url": url,
                "status": None,
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                "error": str(e),
                "body": None,
            })
            raise

        try:
            body = r.json()
        except ValueError:
            body = r.text

        _log.append({
            "url": url,
            "status": r.status_code,
            "elapsed_ms": round(elapsed * 1000, 2),
            "error": None,
            "body": body,
        })
        return r

    return _get