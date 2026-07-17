"""Store lifecycle: provisioning atomicity, cache invalidation, rename,
archive/restore, delete/deprovision, and reconciliation — now per-namespace.

These exercise the real request path (superuser session + the /manage
endpoints) plus the module-level helpers that back cross-worker cache
correctness. Each test provisions its own throwaway handle so the
session-shared ``acme`` / ``globex`` namespaces other suites depend on are
never touched.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import main


# ── Helpers ─────────────────────────────────────────────────────────────


def _handle(prefix: str = "lc") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


async def _registry(engine):
    reg = await main._platform_db(engine)
    return reg["store_registry"]


async def _status(engine, handle: str, store: str) -> str | None:
    doc = await (await _registry(engine)).find_one({"handle": handle, "store": store})
    return None if doc is None else doc.get("status")


async def _physical_names(engine) -> set[str]:
    return set(await engine.connection_manager.mongo_db.list_collection_names())


# ── Provisioning atomicity ───────────────────────────────────────────────


async def test_provision_marks_store_ready(app):
    engine = app.state.engine
    handle, store = _handle(), "shop"
    scope = main.scope_id(handle, store)
    await main.provision_store(engine, handle, store, "Ready Co")

    assert await _status(engine, handle, store) == main.STORE_STATUS_READY
    assert await (await _registry(engine)).count_documents({"handle": handle, "store": store}) == 1
    assert scope in main.KNOWN_STORES
    assert handle in main.KNOWN_HANDLES


async def test_reconcile_finishes_stuck_provisioning(app):
    engine = app.state.engine
    handle, store = _handle("stuck"), "shop"
    scope = main.scope_id(handle, store)
    reg = await _registry(engine)
    old = datetime.now(timezone.utc) - timedelta(minutes=main.PROVISION_STUCK_MINUTES + 5)
    await reg.insert_one(
        {"handle": handle, "store": store, "scope": scope, "name": "Stuck Co",
         "status": main.STORE_STATUS_PROVISIONING, "created_at": old, "updated_at": old}
    )

    result = await main.reconcile_stores(engine)

    assert scope in result["retried"], result
    assert await _status(engine, handle, store) == main.STORE_STATUS_READY
    assert f"{scope}_stores" in await _physical_names(engine)
    assert scope in main.KNOWN_STORES


async def test_reconcile_detects_and_drops_orphans(app):
    engine = app.state.engine
    handle, store = _handle("orphan"), "shop"
    scope = main.scope_id(handle, store)
    raw_db = engine.connection_manager.mongo_db
    # An orphan: a "{scope}_stores" collection with no registry row.
    await raw_db[f"{scope}_stores"].insert_one({"name": "Orphan"})
    await raw_db[f"{scope}_items"].insert_one({"name": "Orphan item"})

    found = await main.reconcile_stores(engine)
    assert scope in found["orphans"], found

    dropped = await main.reconcile_stores(engine, drop_orphans=True)
    assert any(name.startswith(f"{scope}_") for name in dropped["dropped"]), dropped
    names = await _physical_names(engine)
    assert not any(n.startswith(f"{scope}_") for n in names)


async def test_reconcile_recovers_stranded_deleting(app):
    engine = app.state.engine
    handle, store = _handle("stuckdel"), "shop"
    scope = main.scope_id(handle, store)
    await main.provision_store(engine, handle, store, "Half-deleted")
    old = datetime.now(timezone.utc) - timedelta(minutes=main.PROVISION_STUCK_MINUTES + 5)
    reg = await _registry(engine)
    await reg.update_one(
        {"handle": handle, "store": store},
        {"$set": {"status": main.STORE_STATUS_DELETING, "updated_at": old}},
    )

    result = await main.reconcile_stores(engine)

    assert scope in result["deleted"], result
    assert await reg.find_one({"handle": handle, "store": store}) is None
    assert not any(n.startswith(f"{scope}_") for n in await _physical_names(engine))


async def test_reconcile_leaves_fresh_deleting_alone(app):
    """A just-started delete on a peer worker must not be disturbed."""
    engine = app.state.engine
    handle, store = _handle("freshdel"), "shop"
    scope = main.scope_id(handle, store)
    await main.provision_store(engine, handle, store, "Mid-delete")
    reg = await _registry(engine)
    await reg.update_one(
        {"handle": handle, "store": store},
        {"$set": {"status": main.STORE_STATUS_DELETING, "updated_at": datetime.now(timezone.utc)}},
    )

    result = await main.reconcile_stores(engine)
    assert scope not in result["deleted"], result
    assert await reg.find_one({"handle": handle, "store": store}) is not None


async def test_delete_drops_stores_collection_last(app):
    """Orphan detection keys on {scope}_stores, so it must be dropped last."""
    engine = app.state.engine
    handle, store = _handle("droporder"), "shop"
    scope = main.scope_id(handle, store)
    await main.provision_store(engine, handle, store, "Order")
    dropped = await main._drop_store_collections(engine, scope)
    assert dropped, dropped
    assert dropped[-1] == f"{scope}_stores", dropped


# ── Cache invalidation ───────────────────────────────────────────────────


async def test_refresh_reflects_direct_registry_edits(app):
    engine = app.state.engine
    handle, store = _handle("cache"), "shop"
    scope = main.scope_id(handle, store)
    reg = await _registry(engine)

    await reg.insert_one(
        {"handle": handle, "store": store, "scope": scope, "name": "Cache Co",
         "status": main.STORE_STATUS_READY, "created_at": datetime.now(timezone.utc)}
    )
    known = await main.refresh_known_stores()
    assert scope in known

    await reg.update_one(
        {"handle": handle, "store": store}, {"$set": {"status": main.STORE_STATUS_ARCHIVED}}
    )
    known = await main.refresh_known_stores()
    assert scope not in known


async def test_status_less_rows_are_routable(app):
    """Back-compat: registry rows written before ``status`` existed still route."""
    engine = app.state.engine
    handle, store = _handle("legacy"), "shop"
    scope = main.scope_id(handle, store)
    reg = await _registry(engine)
    await reg.insert_one(
        {"handle": handle, "store": store, "scope": scope, "name": "Legacy",
         "created_at": datetime.now(timezone.utc)}
    )

    assert scope in await main.refresh_known_stores()
    assert await main._store_is_registered(handle, store) is True


# ── Rename ────────────────────────────────────────────────────────────────


async def test_rename_updates_registry_and_storefront(admin_client, app):
    engine = app.state.engine
    handle, store = _handle("rename"), "shop"
    await main.provision_store(engine, handle, store, "Old Name")

    res = await admin_client.patch(f"/manage/stores/{handle}/{store}", json={"name": "Fresh Name"})
    assert res.status_code == 200, res.text

    doc = await (await _registry(engine)).find_one({"handle": handle, "store": store})
    assert doc["name"] == "Fresh Name"
    store_db = await engine.get_scoped_db(main.scope_id(handle, store))
    singleton = await store_db["stores"].find_one({})
    assert singleton["name"] == "Fresh Name"


async def test_rename_requires_name(admin_client, app):
    handle, store = _handle("rename"), "shop"
    await main.provision_store(app.state.engine, handle, store, "Keep")
    res = await admin_client.patch(f"/manage/stores/{handle}/{store}", json={"name": "  "})
    assert res.status_code == 422, res.text


async def test_rename_unknown_store_404s(admin_client):
    res = await admin_client.patch("/manage/stores/nope/not-real", json={"name": "X"})
    assert res.status_code == 404, res.text


# ── Archive / restore ──────────────────────────────────────────────────────


async def test_archive_hides_storefront_and_restore_reenables(admin_client, anon_client, app):
    engine = app.state.engine
    handle, store = _handle("arch"), "shop"
    scope = main.scope_id(handle, store)
    await main.provision_store(engine, handle, store, "Archivable")

    assert (await anon_client.get(f"/{handle}/{store}/")).status_code == 200

    arch = await admin_client.post(f"/manage/stores/{handle}/{store}/archive")
    assert arch.status_code == 200, arch.text
    assert await _status(engine, handle, store) == main.STORE_STATUS_ARCHIVED
    assert scope not in main.KNOWN_STORES
    assert (await anon_client.get(f"/{handle}/{store}/")).status_code == 404

    restore = await admin_client.post(f"/manage/stores/{handle}/{store}/restore")
    assert restore.status_code == 200, restore.text
    assert await _status(engine, handle, store) == main.STORE_STATUS_READY
    assert (await anon_client.get(f"/{handle}/{store}/")).status_code == 200


async def test_archive_rejects_platform(admin_client):
    res = await admin_client.post(f"/manage/stores/{main.PLATFORM_SLUG}/shop/archive")
    assert res.status_code == 400, res.text


# ── Delete / deprovision ────────────────────────────────────────────────────


async def test_delete_drops_collections_and_clears_cache(admin_client, anon_client, app):
    engine = app.state.engine
    handle, store = _handle("del"), "shop"
    scope = main.scope_id(handle, store)
    await main.provision_store(engine, handle, store, "Deletable")
    await admin_client.post(f"/{handle}/{store}/api/items", json={"name": "X", "item_code": "DEL-1"})

    before = await _physical_names(engine)
    assert any(n.startswith(f"{scope}_") for n in before)

    res = await admin_client.request(
        "DELETE", f"/manage/stores/{handle}/{store}", json={"confirm": store}
    )
    assert res.status_code == 200, res.text
    assert res.json()["dropped"], res.text

    after = await _physical_names(engine)
    assert not any(n.startswith(f"{scope}_") for n in after)
    assert await (await _registry(engine)).find_one({"handle": handle, "store": store}) is None
    assert scope not in main.KNOWN_STORES
    assert (await anon_client.get(f"/{handle}/{store}/")).status_code == 404


async def test_delete_requires_matching_confirm(admin_client, app):
    engine = app.state.engine
    handle, store = _handle("del"), "shop"
    await main.provision_store(engine, handle, store, "Guarded")

    missing = await admin_client.request("DELETE", f"/manage/stores/{handle}/{store}", json={})
    assert missing.status_code == 422, missing.text
    wrong = await admin_client.request(
        "DELETE", f"/manage/stores/{handle}/{store}", json={"confirm": "not-the-store"}
    )
    assert wrong.status_code == 422, wrong.text
    assert await (await _registry(engine)).find_one({"handle": handle, "store": store}) is not None


async def test_delete_rejects_platform(admin_client):
    res = await admin_client.request(
        "DELETE", f"/manage/stores/{main.PLATFORM_SLUG}/shop", json={"confirm": "shop"}
    )
    assert res.status_code == 400, res.text


async def test_delete_last_store_cascades_namespace_members(admin_client, app):
    """Deleting a handle's last store cleans up its namespace_members."""
    engine = app.state.engine
    handle, store = _handle("cascade"), "shop"
    await main.provision_store(engine, handle, store, "Cascade")
    pdb = await main._platform_db(engine)
    await main.rbac.add_member(pdb["namespace_members"], handle, "someone@x.test", "editor")
    assert await pdb["namespace_members"].count_documents({"handle": handle}) == 1

    res = await admin_client.request(
        "DELETE", f"/manage/stores/{handle}/{store}", json={"confirm": store}
    )
    assert res.status_code == 200, res.text
    assert await pdb["namespace_members"].count_documents({"handle": handle}) == 0
    assert handle not in main.KNOWN_HANDLES


async def test_delete_keeps_members_when_sibling_store_remains(admin_client, app):
    """Deleting one store of a multi-store handle keeps the handle's members."""
    engine = app.state.engine
    handle = _handle("multi")
    await main.provision_store(engine, handle, "shop", "Shop")
    await main.provision_store(engine, handle, "wholesale", "Wholesale")
    pdb = await main._platform_db(engine)
    await main.rbac.add_member(pdb["namespace_members"], handle, "keep@x.test", "owner")

    res = await admin_client.request(
        "DELETE", f"/manage/stores/{handle}/shop", json={"confirm": "shop"}
    )
    assert res.status_code == 200, res.text
    # Sibling store + membership survive.
    assert await pdb["namespace_members"].count_documents({"handle": handle}) == 1
    assert handle in main.KNOWN_HANDLES


# ── Audit trail ────────────────────────────────────────────────────────────


async def _audit_events(engine, handle: str, store: str) -> list[str]:
    reg = await main._platform_db(engine)
    return [d.get("event") async for d in reg["audit_log"].find({"handle": handle, "store": store})]


async def test_lifecycle_writes_audit_trail(admin_client, app):
    engine = app.state.engine
    handle, store = _handle("audit"), "shop"

    await admin_client.post("/manage/stores", json={"handle": handle, "slug": store, "name": "Audited"})
    await admin_client.patch(f"/manage/stores/{handle}/{store}", json={"name": "Audited v2"})
    await admin_client.post(f"/manage/stores/{handle}/{store}/archive")
    await admin_client.post(f"/manage/stores/{handle}/{store}/restore")
    await admin_client.request("DELETE", f"/manage/stores/{handle}/{store}", json={"confirm": store})

    events = await _audit_events(engine, handle, store)
    for expected in (
        "store_created",
        "store_renamed",
        "store_archived",
        "store_restored",
        "store_deleted",
    ):
        assert expected in events, (expected, events)

    reg = await main._platform_db(engine)
    created = await reg["audit_log"].find_one({"handle": handle, "store": store, "event": "store_created"})
    assert created is not None
    assert "@" in str(created.get("actor", "")), created


async def test_delete_unknown_store_404s(admin_client):
    res = await admin_client.request(
        "DELETE", "/manage/stores/nope/not-real", json={"confirm": "not-real"}
    )
    assert res.status_code == 404, res.text


# ── Auth gating ──────────────────────────────────────────────────────────


async def test_lifecycle_endpoints_require_admin(anon_client, app):
    handle, store = _handle("authz"), "shop"
    await main.provision_store(app.state.engine, handle, store, "Gated")

    rename = await anon_client.patch(f"/manage/stores/{handle}/{store}", json={"name": "X"})
    assert rename.status_code in (401, 403), rename.text
    archive = await anon_client.post(f"/manage/stores/{handle}/{store}/archive")
    assert archive.status_code in (401, 403), archive.text
    delete = await anon_client.request(
        "DELETE", f"/manage/stores/{handle}/{store}", json={"confirm": store}
    )
    assert delete.status_code in (401, 403), delete.text
    reconcile = await anon_client.post("/manage/reconcile", json={})
    assert reconcile.status_code in (401, 403), reconcile.text
    assert await main._store_is_registered(handle, store) is True
