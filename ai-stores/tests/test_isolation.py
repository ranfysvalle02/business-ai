"""Cross-tenant isolation — the one class of bug that ruins a multi-tenant app.

Data written under one store must never surface under another. We exercise the
real request path (auto-CRUD + the public inquiry endpoint) for items,
inquiries and sections.
"""
from __future__ import annotations

import uuid



async def _create_item(client, slug: str, item_code: str, name: str):
    return await client.post(
        f"/{slug}/api/items",
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
    res = await _create_item(admin_client, "acme", code, f"Acme {code}")
    assert res.status_code in (200, 201), res.text

    acme_list = await admin_client.get("/acme/api/items")
    beta_list = await admin_client.get("/beta/api/items")
    assert acme_list.status_code == 200
    assert beta_list.status_code == 200
    assert code in acme_list.text
    assert code not in beta_list.text


async def test_inquiries_do_not_leak_across_stores(admin_client, anon_client, stores):
    marker = f"iso-lead-{uuid.uuid4().hex[:8]}"
    # Public (unauthenticated) submission into acme only.
    res = await anon_client.post(
        "/acme/api/submit-inquiry",
        json={
            "customer_name": marker,
            "customer_contact": "test@example.com",
            "message": "Isolation probe",
        },
    )
    assert res.status_code == 201, res.text

    acme_inq = await admin_client.get("/acme/api/inquiries")
    beta_inq = await admin_client.get("/beta/api/inquiries")
    assert acme_inq.status_code == 200
    assert beta_inq.status_code == 200
    assert marker in acme_inq.text
    assert marker not in beta_inq.text


async def test_sections_do_not_leak_across_stores(admin_client, stores):
    key = f"iso-{uuid.uuid4().hex[:8]}"
    res = await admin_client.post(
        "/acme/api/sections",
        json={"key": key, "type": "richtext", "title": "Isolation section", "order": 99},
    )
    assert res.status_code in (200, 201), res.text

    acme_sections = await admin_client.get("/acme/api/sections")
    beta_sections = await admin_client.get("/beta/api/sections")
    assert key in acme_sections.text
    assert key not in beta_sections.text
