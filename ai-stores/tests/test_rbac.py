"""Layered, per-namespace RBAC.

Covers the effective-role overlay end to end: superuser reach, viewer
read-only, non-member/public behaviour, team invite→accept, and the
last-owner guardrail. Public storefront reads must always stay public.
"""
from __future__ import annotations

import uuid

import httpx

import main


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ── Superuser ─────────────────────────────────────────────────────────────


async def test_superuser_sees_all_namespaces_in_console(admin_client, stores):
    res = await admin_client.get("/manage")
    assert res.status_code == 200
    assert "/acme" in res.text
    assert "/globex" in res.text


async def test_superuser_can_write_any_namespace(admin_client, stores):
    a = await admin_client.post("/acme/shop/api/items", json={"name": "s", "item_code": _code("SU-A")})
    g = await admin_client.post("/globex/shop/api/items", json={"name": "s", "item_code": _code("SU-G")})
    assert a.status_code in (200, 201), a.text
    assert g.status_code in (200, 201), g.text


# ── Viewer (read-only) ──────────────────────────────────────────────────


async def test_viewer_can_read_but_not_write(viewer_client, stores):
    read = await viewer_client.get("/acme/shop/api/inquiries")
    assert read.status_code == 200, read.text
    write = await viewer_client.post(
        "/acme/shop/api/items", json={"name": "no", "item_code": _code("V")}
    )
    assert write.status_code == 403, write.text


async def test_viewer_can_open_admin_readonly(viewer_client, stores):
    res = await viewer_client.get("/acme/shop/admin/dashboard")
    assert res.status_code == 200, res.text


async def test_viewer_cannot_use_ai_editor(viewer_client, stores):
    res = await viewer_client.post(
        "/acme/shop/admin/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert res.status_code == 403, res.text


# ── Public / non-member (viewing always allowed) ──────────────────────────


async def test_anon_public_read_and_inquiry(anon_client, stores):
    assert (await anon_client.get("/globex/shop/")).status_code == 200
    r = await anon_client.post(
        "/globex/shop/api/submit-inquiry",
        json={"customer_name": "a", "customer_contact": "a@a.test"},
    )
    assert r.status_code == 201, r.text


async def test_logged_in_nonmember_can_view_and_inquire(outsider_client, stores):
    assert (await outsider_client.get("/globex/shop/")).status_code == 200
    inquire = await outsider_client.post(
        "/globex/shop/api/submit-inquiry",
        json={"customer_name": "x", "customer_contact": "x@x.test"},
    )
    assert inquire.status_code == 201, inquire.text


async def test_logged_in_nonmember_cannot_write_or_admin(outsider_client, stores):
    w = await outsider_client.post(
        "/globex/shop/api/items", json={"name": "no", "item_code": _code("O")}
    )
    assert w.status_code in (401, 403), w.text
    a = await outsider_client.get("/globex/shop/admin/dashboard")
    assert a.status_code in (401, 403), a.text


# ── Team invites (issue → accept → membership) ────────────────────────────


async def test_invite_issue_accept_grants_membership(owner_client, app, stores):
    from mdb_engine.auth.users import create_app_user

    engine = app.state.engine
    email = f"invitee-{uuid.uuid4().hex[:6]}@x.test"
    password = "invitee-password-123"
    pdb = await main._platform_db(engine)
    await create_app_user(pdb, email, password, role="member")

    inv = await owner_client.post(
        "/acme/shop/admin/team/invite", json={"email": email, "role": "editor"}
    )
    assert inv.status_code == 200, inv.text
    token = inv.json()["token"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        login = await c.post("/auth/login", json={"email": email, "password": password})
        assert login.status_code < 300, login.text
        # Cannot write before accepting.
        pre = await c.post("/acme/shop/api/items", json={"name": "pre", "item_code": _code("PRE")})
        assert pre.status_code in (401, 403), pre.text
        # Accept the invite (on the reserved /manage surface, not /admin).
        acc = await c.post("/manage/invite/accept", json={"token": token})
        assert acc.status_code == 200, acc.text
        # Now the editor can write.
        post = await c.post("/acme/shop/api/items", json={"name": "ok", "item_code": _code("OK")})
        assert post.status_code in (200, 201), post.text


async def test_invite_wrong_email_rejected(owner_client, app, stores):
    from mdb_engine.auth.users import create_app_user

    engine = app.state.engine
    invited = f"target-{uuid.uuid4().hex[:6]}@x.test"
    other = f"other-{uuid.uuid4().hex[:6]}@x.test"
    password = "some-password-123"
    pdb = await main._platform_db(engine)
    await create_app_user(pdb, other, password, role="member")

    inv = await owner_client.post(
        "/acme/shop/admin/team/invite", json={"email": invited, "role": "viewer"}
    )
    token = inv.json()["token"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        await c.post("/auth/login", json={"email": other, "password": password})
        res = await c.post("/manage/invite/accept", json={"token": token})
        assert res.status_code == 403, res.text


async def test_non_owner_cannot_invite(viewer_client, stores):
    res = await viewer_client.post(
        "/acme/shop/admin/team/invite", json={"email": "x@x.test", "role": "viewer"}
    )
    assert res.status_code in (401, 403), res.text


# ── Last-owner guardrail ──────────────────────────────────────────────────


async def test_last_owner_cannot_be_demoted_or_removed(admin_client, app, stores):
    engine = app.state.engine
    handle = f"guard{uuid.uuid4().hex[:6]}"
    await main.provision_store(engine, handle, "shop", "Guard", owner_email="o1@x.test")
    pdb = await main._platform_db(engine)
    await main.rbac.add_member(pdb["namespace_members"], handle, "o1@x.test", "owner")

    demote = await admin_client.patch(
        f"/{handle}/shop/admin/team/members", json={"email": "o1@x.test", "role": "editor"}
    )
    assert demote.status_code == 409, demote.text
    remove = await admin_client.request(
        "DELETE", f"/{handle}/shop/admin/team/members", json={"email": "o1@x.test"}
    )
    assert remove.status_code == 409, remove.text

    # With a second owner, removing the first is allowed.
    await main.rbac.add_member(pdb["namespace_members"], handle, "o2@x.test", "owner")
    ok = await admin_client.request(
        "DELETE", f"/{handle}/shop/admin/team/members", json={"email": "o1@x.test"}
    )
    assert ok.status_code == 200, ok.text
