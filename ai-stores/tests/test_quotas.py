"""Per-store quota enforcement.

Caps are read from module globals at request time, so tests set them with
``monkeypatch`` (``0`` = unlimited). We assert both the CRUD write path
(``QuotaMiddleware``) and the AI-apply path (which bypasses CRUD) return 409 at
the cap.
"""
from __future__ import annotations

import uuid

import pytest_asyncio


@pytest_asyncio.fixture()
async def quota_store(app):
    """A throwaway store with a couple of items to count against."""
    import main

    engine = app.state.engine
    handle, store = "quota", f"s{uuid.uuid4().hex[:6]}"
    await main.provision_store(engine, handle, store, "Quota Shop", owner_email="admin@test.local")
    return handle, store


async def test_crud_item_create_blocked_at_cap(app, admin_client, quota_store, monkeypatch):
    import main

    handle, store = quota_store
    path = f"/{handle}/{store}"

    # Seed one item with quotas disabled so the collection is non-empty.
    monkeypatch.setattr(main, "MAX_ITEMS_PER_STORE", 0)
    created = await admin_client.post(
        f"{path}/api/items", json={"name": "Seed", "item_code": f"seed-{uuid.uuid4().hex[:6]}"}
    )
    assert created.status_code in (200, 201), created.text

    scope = main.scope_id(handle, store)
    count = await main._store_doc_count(app.state.engine, scope, "items")
    assert count >= 1

    # Pin the cap at the current count → the next create is over the limit.
    monkeypatch.setattr(main, "MAX_ITEMS_PER_STORE", count)
    blocked = await admin_client.post(
        f"{path}/api/items", json={"name": "TooMany", "item_code": f"x-{uuid.uuid4().hex[:6]}"}
    )
    assert blocked.status_code == 409, blocked.text


async def test_crud_section_create_blocked_at_cap(app, admin_client, quota_store, monkeypatch):
    import main

    handle, store = quota_store
    path = f"/{handle}/{store}"

    scope = main.scope_id(handle, store)
    count = await main._store_doc_count(app.state.engine, scope, "sections")
    # A freshly provisioned store seeds sections; ensure there's at least one.
    if count == 0:
        monkeypatch.setattr(main, "MAX_SECTIONS_PER_STORE", 0)
        await admin_client.post(
            f"{path}/api/sections",
            json={"key": f"k{uuid.uuid4().hex[:6]}", "type": "richtext", "title": "S", "order": 1},
        )
        count = await main._store_doc_count(app.state.engine, scope, "sections")

    monkeypatch.setattr(main, "MAX_SECTIONS_PER_STORE", count)
    blocked = await admin_client.post(
        f"{path}/api/sections",
        json={"key": f"k{uuid.uuid4().hex[:6]}", "type": "richtext", "title": "Nope", "order": 99},
    )
    assert blocked.status_code == 409, blocked.text


async def test_ai_apply_respects_item_cap(app, admin_client, quota_store, monkeypatch):
    import main

    handle, store = quota_store
    path = f"/{handle}/{store}"

    # Make sure at least one item exists, then pin the cap at the count.
    monkeypatch.setattr(main, "MAX_ITEMS_PER_STORE", 0)
    await admin_client.post(
        f"{path}/api/items", json={"name": "Seed2", "item_code": f"seed2-{uuid.uuid4().hex[:6]}"}
    )
    scope = main.scope_id(handle, store)
    count = await main._store_doc_count(app.state.engine, scope, "items")

    monkeypatch.setattr(main, "MAX_ITEMS_PER_STORE", count)
    res = await admin_client.post(
        f"{path}/admin/ai/apply",
        json={"ops": [{"tool": "create_item", "args": {"name": f"AI {uuid.uuid4().hex[:6]}"}}]},
    )
    assert res.status_code == 409, res.text


async def test_zero_cap_is_unlimited(app, admin_client, quota_store, monkeypatch):
    import main

    handle, store = quota_store
    path = f"/{handle}/{store}"
    monkeypatch.setattr(main, "MAX_ITEMS_PER_STORE", 0)
    for _ in range(3):
        res = await admin_client.post(
            f"{path}/api/items", json={"name": "Unlimited", "item_code": f"u-{uuid.uuid4().hex[:8]}"}
        )
        assert res.status_code in (200, 201), res.text
