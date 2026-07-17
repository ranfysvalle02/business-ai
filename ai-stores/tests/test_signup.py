"""Public self-serve signup: claim a handle + first store, become its owner."""
from __future__ import annotations

import uuid

import httpx

import main


def _handle(prefix: str = "su") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


async def test_signup_page_renders(anon_client):
    res = await anon_client.get("/signup")
    assert res.status_code == 200
    assert "Claim your handle" in res.text


async def test_signup_creates_namespace_and_owner(anon_client, app):
    engine = app.state.engine
    handle = _handle()
    email = f"{handle}@x.test"
    password = "signup-password-123"

    res = await anon_client.post(
        "/signup",
        json={"email": email, "password": password, "handle": handle, "slug": "shop", "store_name": "My Shop"},
    )
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["url"] == f"/{handle}/shop/"

    # The store routes publicly.
    assert (await anon_client.get(f"/{handle}/shop/")).status_code == 200

    # The user is a non-admin member carrying its handle, and owns the namespace.
    pdb = await main._platform_db(engine)
    user = await pdb["users"].find_one({"email": email})
    assert user is not None
    assert user.get("role") == "member"
    assert user.get("handle") == handle
    role = await main.rbac.get_namespace_role(pdb["namespace_members"], handle, email)
    assert role == "owner"


async def test_signup_then_login_can_edit_own_store(anon_client, app):
    handle = _handle("edit")
    email = f"{handle}@x.test"
    password = "signup-password-123"
    res = await anon_client.post(
        "/signup", json={"email": email, "password": password, "handle": handle, "slug": "shop"}
    )
    assert res.status_code == 201, res.text

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        login = await c.post("/auth/login", json={"email": email, "password": password})
        assert login.status_code < 300, login.text
        write = await c.post(
            f"/{handle}/shop/api/items", json={"name": "mine", "item_code": "S-1"}
        )
        assert write.status_code in (200, 201), write.text


async def test_signup_duplicate_handle_409(anon_client, app):
    handle = _handle("dup")
    r1 = await anon_client.post(
        "/signup", json={"email": f"{handle}-1@x.test", "password": "signup-password-123", "handle": handle, "slug": "shop"}
    )
    assert r1.status_code == 201, r1.text
    r2 = await anon_client.post(
        "/signup", json={"email": f"{handle}-2@x.test", "password": "signup-password-123", "handle": handle, "slug": "other"}
    )
    assert r2.status_code == 409, r2.text


async def test_signup_duplicate_email_409(anon_client, app):
    email = f"dupmail{uuid.uuid4().hex[:6]}@x.test"
    r1 = await anon_client.post(
        "/signup", json={"email": email, "password": "signup-password-123", "handle": _handle(), "slug": "shop"}
    )
    assert r1.status_code == 201, r1.text
    r2 = await anon_client.post(
        "/signup", json={"email": email, "password": "signup-password-123", "handle": _handle(), "slug": "shop"}
    )
    assert r2.status_code == 409, r2.text


async def test_signup_rejects_reserved_handle(anon_client):
    res = await anon_client.post(
        "/signup", json={"email": "x@x.test", "password": "signup-password-123", "handle": "admin", "slug": "shop"}
    )
    assert res.status_code == 422, res.text


async def test_signup_rejects_short_password(anon_client):
    res = await anon_client.post(
        "/signup",
        json={"email": "shortpw@x.test", "password": "short", "handle": _handle(), "slug": "shop"},
    )
    assert res.status_code == 422, res.text
