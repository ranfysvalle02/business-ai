"""Per-user namespace routing: /{handle}/{store} resolution + one owner,
many stores, with cross-namespace data isolation intact.
"""
from __future__ import annotations

import uuid


# ── Two-segment routing ──────────────────────────────────────────────────


async def test_store_home_resolves(anon_client, stores):
    res = await anon_client.get("/acme/shop/")
    assert res.status_code == 200, res.text


async def test_unknown_handle_404s(anon_client, stores):
    res = await anon_client.get("/nope-handle/shop/")
    assert res.status_code == 404


async def test_unknown_store_in_known_handle_404s(anon_client, stores):
    res = await anon_client.get("/acme/nope-store/")
    assert res.status_code == 404


async def test_namespace_landing_lists_stores(anon_client, stores):
    res = await anon_client.get("/acme/")
    assert res.status_code == 200
    # Both acme stores appear on the landing.
    assert "/acme/shop" in res.text
    assert "/acme/wholesale" in res.text


async def test_bare_handle_without_slash_still_lands(anon_client, stores):
    # Trailing-slash normalisation → the landing renders either way.
    res = await anon_client.get("/acme", follow_redirects=True)
    assert res.status_code == 200


# ── One owner, many stores ────────────────────────────────────────────────


async def test_owner_can_edit_all_stores_under_their_handle(owner_client, stores):
    code = f"NS-{uuid.uuid4().hex[:8]}"
    a = await owner_client.post("/acme/shop/api/items", json={"name": "A", "item_code": code + "-a"})
    b = await owner_client.post("/acme/wholesale/api/items", json={"name": "B", "item_code": code + "-b"})
    assert a.status_code in (200, 201), a.text
    assert b.status_code in (200, 201), b.text


# ── Cross-namespace isolation (writes blocked, viewing allowed) ────────────


async def test_owner_cannot_write_other_namespace(owner_client, stores):
    res = await owner_client.post(
        "/globex/shop/api/items", json={"name": "Nope", "item_code": "X-1"}
    )
    assert res.status_code in (401, 403), res.text


async def test_owner_cannot_open_other_namespace_admin(owner_client, stores):
    res = await owner_client.get("/globex/shop/admin/dashboard")
    assert res.status_code in (401, 403), res.text


async def test_owner_can_still_view_other_namespace_storefront(owner_client, stores):
    res = await owner_client.get("/globex/shop/")
    assert res.status_code == 200, res.text


async def test_data_isolated_between_namespaces(admin_client, stores):
    code = f"NSISO-{uuid.uuid4().hex[:8]}"
    await admin_client.post("/acme/shop/api/items", json={"name": "iso", "item_code": code})
    globex = await admin_client.get("/globex/shop/api/items")
    assert code not in globex.text


# ── Quick store: one-click "hello world" onboarding ───────────────────────


async def test_quick_store_owner_gets_ready_store(owner_client, stores):
    res = await owner_client.post("/manage/stores/quick", json={"handle": "acme"})
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["handle"] == "acme"
    assert data["store"]  # auto-addressed
    # The starter store is immediately live and editable by the owner.
    assert (await owner_client.get(data["url"])).status_code == 200
    assert (await owner_client.get(data["admin_url"])).status_code == 200


async def test_quick_store_auto_increments_address(admin_client, stores):
    handle = f"quick-{uuid.uuid4().hex[:8]}"
    first = await admin_client.post("/manage/stores/quick", json={"handle": handle})
    second = await admin_client.post("/manage/stores/quick", json={"handle": handle})
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["store"] == "store"
    assert second.json()["store"] == "store-2"


async def test_quick_store_forbidden_for_viewer(viewer_client, stores):
    res = await viewer_client.post("/manage/stores/quick", json={"handle": "acme"})
    assert res.status_code == 403, res.text


async def test_quick_store_outsider_without_handle_is_guided(outsider_client, stores):
    res = await outsider_client.post("/manage/stores/quick", json={})
    assert res.status_code == 422, res.text


async def test_quick_store_requires_auth(anon_client, stores):
    res = await anon_client.post("/manage/stores/quick", json={"handle": "acme"})
    assert res.status_code in (401, 403), res.text
