"""Cross-tenant isolation — the one class of bug that ruins a multi-tenant app.

Data written under one store must never surface under another, whether the
stores share a handle (``acme/shop`` vs ``acme/wholesale``) or live in
different namespaces (``acme/shop`` vs ``globex/shop``). We exercise the real
request path (auto-CRUD + the public inquiry endpoint).
"""
from __future__ import annotations

import uuid


async def _create_item(client, path: str, item_code: str, name: str):
    return await client.post(
        f"{path}/api/items",
        json={
            "name": name,
            "item_code": item_code,
            "price": 10.0,
            "category": "Test",
            "status": "Available",
        },
    )


async def test_items_do_not_leak_across_stores(admin_client, stores):
    code = f"ISO-{uuid.uuid4().hex[:8]}"
    res = await _create_item(admin_client, "/acme/shop", code, f"Acme {code}")
    assert res.status_code in (200, 201), res.text

    shop_list = await admin_client.get("/acme/shop/api/items")
    wholesale_list = await admin_client.get("/acme/wholesale/api/items")
    globex_list = await admin_client.get("/globex/shop/api/items")
    assert code in shop_list.text
    # Same handle, different store → isolated.
    assert code not in wholesale_list.text
    # Different namespace → isolated.
    assert code not in globex_list.text


async def test_inquiries_do_not_leak_across_stores(admin_client, anon_client, stores):
    marker = f"iso-lead-{uuid.uuid4().hex[:8]}"
    # Public (unauthenticated) submission into acme/shop only.
    res = await anon_client.post(
        "/acme/shop/api/submit-inquiry",
        json={
            "customer_name": marker,
            "customer_contact": "test@example.com",
            "message": "Isolation probe",
        },
    )
    assert res.status_code == 201, res.text

    shop_inq = await admin_client.get("/acme/shop/api/inquiries")
    globex_inq = await admin_client.get("/globex/shop/api/inquiries")
    assert shop_inq.status_code == 200
    assert globex_inq.status_code == 200
    assert marker in shop_inq.text
    assert marker not in globex_inq.text


async def test_sections_do_not_leak_across_stores(admin_client, stores):
    key = f"iso-{uuid.uuid4().hex[:8]}"
    res = await admin_client.post(
        "/acme/shop/api/sections",
        json={"key": key, "type": "richtext", "title": "Isolation section", "order": 99},
    )
    assert res.status_code in (200, 201), res.text

    shop_sections = await admin_client.get("/acme/shop/api/sections")
    globex_sections = await admin_client.get("/globex/shop/api/sections")
    assert key in shop_sections.text
    assert key not in globex_sections.text
