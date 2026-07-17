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
os.environ.setdefault("SIGNUP_RATELIMIT_PER_MIN", "100000")
os.environ.setdefault("SIGNUP_RATELIMIT_PER_HOUR", "100000")
# Notifications off by default in tests (no outbound HTTP).
os.environ.pop("RESEND_API_KEY", None)

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from asgi_lifespan import LifespanManager  # noqa: E402

ADMIN_EMAIL = os.environ["ADMIN_EMAIL"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]

# Non-admin namespace members used across the RBAC/namespace suites.
OWNER_EMAIL = "owner@acme.test"
OWNER_PASSWORD = "owner-password-123"
VIEWER_EMAIL = "viewer@acme.test"
VIEWER_PASSWORD = "viewer-password-123"
OUTSIDER_EMAIL = "outsider@nowhere.test"
OUTSIDER_PASSWORD = "outsider-password-123"


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
    """Provision the session's namespaces, members, and non-admin users.

    Layout (per-user namespaces at /{handle}/{store}):
      * ``acme/shop`` + ``acme/wholesale`` — one owner (OWNER_EMAIL) owns both.
      * ``globex/shop`` — a separate namespace (owner OWNER of globex).
    A ``viewer`` of ``acme`` and an unaffiliated ``outsider`` user are seeded
    so the RBAC suites can log in as each role.
    """
    import main
    from mdb_engine.auth.users import create_app_user

    engine = app.state.engine
    await main.provision_store(engine, "acme", "shop", "Acme Shop", owner_email=OWNER_EMAIL)
    await main.provision_store(engine, "acme", "wholesale", "Acme Wholesale", owner_email=OWNER_EMAIL)
    await main.provision_store(engine, "globex", "shop", "Globex Shop", owner_email="owner@globex.test")

    pdb = await main._platform_db(engine)
    # Non-admin users (role="member"); memberships drive per-namespace authz.
    for email, password in (
        (OWNER_EMAIL, OWNER_PASSWORD),
        (VIEWER_EMAIL, VIEWER_PASSWORD),
        (OUTSIDER_EMAIL, OUTSIDER_PASSWORD),
    ):
        await create_app_user(pdb, email, password, role="member")
    members = pdb["namespace_members"]
    await main.rbac.add_member(members, "acme", OWNER_EMAIL, "owner")
    await main.rbac.add_member(members, "acme", VIEWER_EMAIL, "viewer")
    await main.rbac.add_member(members, "globex", "owner@globex.test", "owner")
    return {
        "acme_shop": ("acme", "shop"),
        "acme_wholesale": ("acme", "wholesale"),
        "globex_shop": ("globex", "shop"),
    }


def _make_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


async def _login_client(app, email: str, password: str) -> httpx.AsyncClient:
    client = _make_client(app)
    res = await client.post("/auth/login", json={"email": email, "password": password})
    assert res.status_code < 300, f"login failed for {email}: {res.status_code} {res.text}"
    return client


@pytest_asyncio.fixture(scope="session")
async def admin_client(app):
    """A client authenticated as the platform superuser (logs in once)."""
    client = await _login_client(app, ADMIN_EMAIL, ADMIN_PASSWORD)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture(scope="session")
async def owner_client(app, stores):
    """A client authenticated as the owner of the ``acme`` namespace."""
    client = await _login_client(app, OWNER_EMAIL, OWNER_PASSWORD)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture(scope="session")
async def viewer_client(app, stores):
    """A client authenticated as a read-only viewer of the ``acme`` namespace."""
    client = await _login_client(app, VIEWER_EMAIL, VIEWER_PASSWORD)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture(scope="session")
async def outsider_client(app, stores):
    """A logged-in user who is a member of no namespace."""
    client = await _login_client(app, OUTSIDER_EMAIL, OUTSIDER_PASSWORD)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture()
async def anon_client(app):
    """An unauthenticated client (fresh cookie jar, no login)."""
    async with _make_client(app) as client:
        yield client
