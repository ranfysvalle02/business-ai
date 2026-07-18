"""Store provisioning: idempotency + per-store unique constraints."""
from __future__ import annotations

import uuid

import main


async def _item_code_count(scope: str, code: str) -> int:
    db = await main.app.state.engine.get_scoped_db(scope)
    return await db["items"].count_documents({"item_code": code})


async def test_provision_is_idempotent(app):
    handle = f"idemp{uuid.uuid4().hex[:8]}"
    store = "shop"
    scope = main.scope_id(handle, store)
    engine = app.state.engine
    template_items = len(main._STORE_TEMPLATE.get("items", []))
    template_sections = len(main._STORE_TEMPLATE.get("sections", []))

    await main.provision_store(engine, handle, store, "Idemp One")
    db = await engine.get_scoped_db(scope)
    first_items = await db["items"].count_documents({})
    first_sections = await db["sections"].count_documents({})
    first_stores = await db["stores"].count_documents({})

    # Run again — additive seeding must not duplicate anything.
    await main.provision_store(engine, handle, store, "Idemp One")
    assert await db["items"].count_documents({}) == first_items == template_items
    assert await db["sections"].count_documents({}) == first_sections == template_sections
    assert await db["stores"].count_documents({}) == first_stores == 1

    # Exactly one registry row, and the in-memory cache is warm.
    reg = await main._platform_db(engine)
    assert await reg["store_registry"].count_documents({"handle": handle, "store": store}) == 1
    assert scope in main.KNOWN_STORES
    assert handle in main.KNOWN_HANDLES


async def test_business_type_template_selects_starter_content(app):
    engine = app.state.engine

    # A restaurant store seeds the restaurant template (schema.org Restaurant,
    # a "Menu" section) and records the template on the registry row.
    r_handle = f"resto{uuid.uuid4().hex[:8]}"
    r_scope = main.scope_id(r_handle, "shop")
    await main.provision_store(engine, r_handle, "shop", "Chez Test", template="restaurant")
    rdb = await engine.get_scoped_db(r_scope)
    rstore = await rdb["stores"].find_one({}) or {}
    assert rstore.get("business_type") == "restaurant"
    assert rstore.get("schema_type") == "Restaurant"
    assert await rdb["sections"].count_documents({"anchor": "menu"}) == 1

    reg = await main._platform_db(engine)
    row = await reg["store_registry"].find_one({"handle": r_handle, "store": "shop"})
    assert row.get("template") == "restaurant"

    # A blank/unknown type falls back to the default (retail) template.
    d_handle = f"deflt{uuid.uuid4().hex[:8]}"
    d_scope = main.scope_id(d_handle, "shop")
    await main.provision_store(engine, d_handle, "shop", "Default Co", template="does-not-exist")
    ddb = await engine.get_scoped_db(d_scope)
    dstore = await ddb["stores"].find_one({}) or {}
    assert dstore.get("business_type") == "retail"

    # "retail" is always offered in the create/signup UIs.
    assert "retail" in main.STORE_TEMPLATES
    assert "restaurant" in main.STORE_TEMPLATES


async def test_item_code_unique_within_store_but_not_across(admin_client, stores):
    code = f"DUP-{uuid.uuid4().hex[:6]}"

    first = await admin_client.post("/acme/shop/api/items", json={"name": "Dup A", "item_code": code})
    assert first.status_code in (200, 201), first.text

    dup = await admin_client.post("/acme/shop/api/items", json={"name": "Dup A2", "item_code": code})
    assert dup.status_code != 201, "duplicate item_code should be rejected within a store"

    # Same code in a *different* store (even under the same handle) is fine —
    # collections are per store scope.
    other = await admin_client.post("/acme/wholesale/api/items", json={"name": "Dup B", "item_code": code})
    assert other.status_code in (200, 201), other.text

    # Verify at the data layer: exactly one in each store scope.
    assert await _item_code_count(main.scope_id("acme", "shop"), code) == 1
    assert await _item_code_count(main.scope_id("acme", "wholesale"), code) == 1
