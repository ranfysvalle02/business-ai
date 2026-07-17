"""Store provisioning: idempotency + per-store unique constraints."""
from __future__ import annotations

import uuid

import main


async def _item_code_count(slug: str, code: str) -> int:
    db = await main.app.state.engine.get_scoped_db(slug)
    return await db["items"].count_documents({"item_code": code})


async def test_provision_is_idempotent(app):
    slug = f"idemp{uuid.uuid4().hex[:8]}"
    engine = app.state.engine
    template_items = len(main._STORE_TEMPLATE.get("items", []))
    template_sections = len(main._STORE_TEMPLATE.get("sections", []))

    await main.provision_store(engine, slug, "Idemp One")
    db = await engine.get_scoped_db(slug)
    first_items = await db["items"].count_documents({})
    first_sections = await db["sections"].count_documents({})
    first_stores = await db["stores"].count_documents({})

    # Run again — additive seeding must not duplicate anything.
    await main.provision_store(engine, slug, "Idemp One")
    assert await db["items"].count_documents({}) == first_items == template_items
    assert await db["sections"].count_documents({}) == first_sections == template_sections
    assert await db["stores"].count_documents({}) == first_stores == 1

    # Exactly one registry row, and the in-memory cache is warm.
    reg = await main._platform_db(engine)
    assert await reg["store_registry"].count_documents({"slug": slug}) == 1
    assert slug in main.KNOWN_STORES


async def test_item_code_unique_within_store_but_not_across(admin_client, stores):
    code = f"DUP-{uuid.uuid4().hex[:6]}"

    first = await admin_client.post("/acme/api/items", json={"name": "Dup A", "item_code": code})
    assert first.status_code in (200, 201), first.text

    dup = await admin_client.post("/acme/api/items", json={"name": "Dup A2", "item_code": code})
    assert dup.status_code != 201, "duplicate item_code should be rejected within a store"

    # Same code in a *different* store is fine — collections are per-store.
    other = await admin_client.post("/beta/api/items", json={"name": "Dup B", "item_code": code})
    assert other.status_code in (200, 201), other.text

    # Verify at the data layer: exactly one in each store.
    assert await _item_code_count("acme", code) == 1
    assert await _item_code_count("beta", code) == 1
