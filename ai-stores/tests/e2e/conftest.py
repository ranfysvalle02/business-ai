"""End-to-end (Playwright) harness for AI Stores.

Unlike the fast in-process suite, these tests drive a *real browser* against a
*real uvicorn server* so the actual templates + JavaScript (signup, quick
store, the AI chat widget) are exercised end to end.

The server is booted once per session as a subprocess against the same local
MongoDB Atlas Local the unit suite uses, in a throwaway database that is dropped
on teardown. The whole directory is excluded from the default ``pytest`` run
(see ``pytest.ini``'s ``--ignore``) so the fast suite never needs Playwright.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[2]  # .../ai-stores
E2E_DB = f"ai_stores_e2e_{uuid.uuid4().hex[:8]}"
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/?directConnection=true")
ADMIN_EMAIL = "admin@e2e.local"
ADMIN_PASSWORD = "e2e-admin-password-123"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_json(url: str, timeout: float = 2.0) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — localhost only
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:  # readiness may 503 while warming up
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return exc.code, {}
    except (urllib.error.URLError, OSError):
        # Server not accepting connections yet (still booting) — signal "retry".
        return 0, {}


@pytest.fixture(scope="session")
def live_server():
    """Boot uvicorn on a free port against a throwaway DB; yield its base URL."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "MONGODB_URI": MONGODB_URI,
        "MDB_DB_NAME": E2E_DB,
        "MDB_JWT_SECRET": "e2e-only-jwt-secret-at-least-32-characters-long",
        "MDB_ENGINE_MASTER_KEY": "",  # secrets manager off for a clean platform scope
        "ADMIN_EMAIL": ADMIN_EMAIL,
        "ADMIN_PASSWORD": ADMIN_PASSWORD,
        "SIGNUP_RATELIMIT_PER_MIN": "100000",
        "SIGNUP_RATELIMIT_PER_HOUR": "100000",
        "AI_RATELIMIT_PER_MIN": "100000",
        "LOG_LEVEL": "WARNING",
        "HOST": "127.0.0.1",
        "PORT": str(port),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(APP_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            raise RuntimeError(f"uvicorn exited early (code {proc.returncode}):\n{out}")
        status, _ = _http_json(base_url + "/healthz")
        if status == 200:
            break
        time.sleep(0.4)
    else:
        proc.terminate()
        raise RuntimeError("uvicorn did not become healthy within 60s")

    try:
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            from pymongo import MongoClient

            client = MongoClient(MONGODB_URI)
            client.drop_database(E2E_DB)
            client.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


@pytest.fixture(scope="session")
def ai_configured(live_server) -> bool:
    """Whether the live server has a Gemini key (drives the AI-chat skip)."""
    _, data = _http_json(live_server + "/readyz")
    return bool(data.get("ai_configured"))
