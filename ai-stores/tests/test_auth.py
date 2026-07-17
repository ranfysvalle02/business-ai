"""Authentication + routing gates.

Admin surfaces reject anonymous callers; the superuser session works across
every namespace; unknown handles and unknown stores 404.
"""
from __future__ import annotations


async def test_admin_collection_requires_auth(anon_client, stores):
    # inquiries read is member-only (auth.roles = ["admin", "viewer"]).
    res = await anon_client.get("/acme/shop/api/inquiries")
    assert res.status_code in (401, 403), res.text


async def test_admin_write_requires_auth(anon_client, stores):
    res = await anon_client.post(
        "/acme/shop/api/items",
        json={"name": "Nope", "item_code": "NOPE-1"},
    )
    assert res.status_code in (401, 403), res.text


async def test_superuser_session_works_across_namespaces(admin_client, stores):
    for path in ("/acme/shop", "/acme/wholesale", "/globex/shop"):
        res = await admin_client.get(f"{path}/api/inquiries")
        assert res.status_code == 200, f"{path}: {res.status_code} {res.text}"


async def test_unknown_handle_returns_404(anon_client):
    res = await anon_client.get("/definitely-not-a-handle/")
    assert res.status_code == 404


async def test_unknown_store_in_known_handle_returns_404(anon_client, stores):
    res = await anon_client.get("/acme/definitely-not-a-store/")
    assert res.status_code == 404


async def test_namespace_landing_is_public(anon_client, stores):
    res = await anon_client.get("/acme/")
    assert res.status_code == 200
    # Lists the handle's stores.
    assert "shop" in res.text


async def test_manage_console_gated_for_anonymous(anon_client):
    # /manage renders (200) but shows the sign-in form, not the console.
    res = await anon_client.get("/manage")
    assert res.status_code == 200
    # Creating a store must be rejected without a session.
    created = await anon_client.post(
        "/manage/stores", json={"handle": "sneaky", "slug": "shop", "name": "Sneaky"}
    )
    assert created.status_code in (401, 403), created.text
