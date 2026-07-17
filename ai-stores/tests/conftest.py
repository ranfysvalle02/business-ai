"""Shared pytest fixtures for the AI Stores test suite.

The suite drives the *real* ASGI app in-process (httpx ``ASGITransport`` +
``asgi-lifespan``) so the engine lifespan and ``_bootstrap_stores`` actually
run — the same code path production uses. Everything runs against a live
MongoDB Atlas Local in a throwaway database that is dropped on teardown.

Design notes:
  * Test env is set **before** ``main`` is imported. ``main`` calls
    ``load_dotenv()`` which never overrides already-set vars, so these win.
  * ``MDB_ENGINE_MASTER_KEY`` is blanked so the secrets manager stays off and
    the platform scope needs no app-token dance.
  * One session-scoped event loop + lifespan so the shared admin logs in
    exactly once (the engine rate-limits ``/auth/login`` to 5 / 15 min).
"""
from __future__ import annotations

import asyncio
import os
import uuid

# ── Test environment (must be set before importing main) ────────────────
_TEST_DB = f"ai_stores_test_{uuid.uuid4().hex[:8]}"
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/?directConnection=true")
os.environ["MDB_DB_NAME"] = _TEST_DB
os.environ.setdefault("MDB_JWT_SECRET", "test-only-jwt-secret-at-least-32-characters-long")
os.environ["MDB_ENGINE_MASTER_KEY"] = ""  # disable secrets manager in tests
os.environ.setdefault("ADMIN_EMAIL", "admin@test.local")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password-123")
# Relax abuse throttles so functional tests never trip them; the dedicated
# abuse test overrides app.state to assert throttling deterministically.
os.environ.setdefault("INQUIRY_RATELIMIT_PER_MIN", "100000")
os.environ.setdefault("INQUIRY_RATELIMIT_PER_HOUR", "100000")
os.environ.setdefault("AI_RATELIMIT_PER_MIN", "100000")
os.environ.setdefault("UPLOAD_RATELIMIT_PER_MIN", "100000")
# Notifications off by default in tests (no outbound HTTP).
os.environ.pop("RESEND_API_KEY", None)

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from asgi_lifespan import LifespanManager  # noqa: E402

ADMIN_EMAIL = os.environ["ADMIN_EMAIL"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]


@pytest.fixture(scope="session")
def event_loop():
    """One event loop for the whole session so lifespan + login persist."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def app():
    """The live app with its lifespan running (engine connected, stores bootstrapped)."""
    import main

    async with LifespanManager(main.app, startup_timeout=60, shutdown_timeout=60):
        try:
            yield main.app
        finally:
            # Drop the throwaway DB so nothing leaks between runs.
            try:
                engine = main.app.state.engine
                client = engine.connection_manager.mongo_db.client
                await client.drop_database(_TEST_DB)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass


@pytest_asyncio.fixture(scope="session")
async def stores(app):
    """Provision two isolated stores (acme, beta) once for the session."""
    import main

    await main.provision_store(app.state.engine, "acme", "Acme Co")
    await main.provision_store(app.state.engine, "beta", "Beta LLC")
    return ["acme", "beta"]


def _make_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


@pytest_asyncio.fixture(scope="session")
async def admin_client(app):
    """A client authenticated as the shared admin (logs in exactly once)."""
    async with _make_client(app) as client:
        res = await client.post(
            "/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        assert res.status_code < 300, f"admin login failed: {res.status_code} {res.text}"
        yield client


@pytest_asyncio.fixture()
async def anon_client(app):
    """An unauthenticated client (fresh cookie jar, no login)."""
    async with _make_client(app) as client:
        yield client
