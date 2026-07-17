"""Authentication + routing gates.

Admin surfaces reject anonymous callers; a single shared admin session works
across every store; unknown stores 404.
"""
from __future__ import annotations


async def test_admin_collection_requires_auth(anon_client, stores):
    # inquiries read is admin-only (auth.roles = ["admin"]).
    res = await anon_client.get("/acme/api/inquiries")
    assert res.status_code in (401, 403), res.text


async def test_admin_write_requires_auth(anon_client, stores):
    res = await anon_client.post(
        "/acme/api/items",
        json={"name": "Nope", "item_code": "NOPE-1"},
    )
    assert res.status_code in (401, 403), res.text


async def test_shared_session_works_across_stores(admin_client, stores):
    for slug in ("acme", "beta"):
        res = await admin_client.get(f"/{slug}/api/inquiries")
        assert res.status_code == 200, f"{slug}: {res.status_code} {res.text}"


async def test_unknown_store_returns_404(anon_client):
    res = await anon_client.get("/definitely-not-a-store/")
    assert res.status_code == 404


async def test_manage_console_gated_for_anonymous(anon_client):
    # /manage renders (200) but shows the sign-in form, not the store picker.
    res = await anon_client.get("/manage")
    assert res.status_code == 200
    # Creating a store must be rejected without an admin session.
    created = await anon_client.post("/manage/stores", json={"slug": "sneaky", "name": "Sneaky"})
    assert created.status_code in (401, 403), created.text
