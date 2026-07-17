"""Store lifecycle: provisioning atomicity, cache invalidation, rename,
archive/restore, delete/deprovision, and reconciliation.

These exercise the real request path (admin session + the /manage endpoints)
plus the module-level helpers that back cross-worker cache correctness. Each
test provisions its own throwaway store so the session-shared ``acme`` / ``beta``
stores other suites depend on are never touched.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import main


# ── Helpers ─────────────────────────────────────────────────────────────


def _slug(prefix: str = "lc") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


async def _registry(engine):
    reg = await main._platform_db(engine)
    return reg["store_registry"]


async def _status(engine, slug: str) -> str | None:
    doc = await (await _registry(engine)).find_one({"slug": slug})
    return None if doc is None else doc.get("status")


async def _physical_names(engine) -> set[str]:
    return set(await engine.connection_manager.mongo_db.list_collection_names())


# ── Provisioning atomicity (#10) ─────────────────────────────────────────


async def test_provision_marks_store_ready(app):
    engine = app.state.engine
    slug = _slug()
    await main.provision_store(engine, slug, "Ready Co")

    assert await _status(engine, slug) == main.STORE_STATUS_READY
    # The registry row exists exactly once and the cache is warm.
    assert await (await _registry(engine)).count_documents({"slug": slug}) == 1
    assert slug in main.KNOWN_STORES


async def test_reconcile_finishes_stuck_provisioning(app):
    engine = app.state.engine
    slug = _slug("stuck")
    reg = await _registry(engine)
    # Simulate a crash mid-provision: a lone "provisioning" row, no collections,
    # last touched long enough ago to be considered stuck.
    old = datetime.now(timezone.utc) - timedelta(minutes=main.PROVISION_STUCK_MINUTES + 5)
    await reg.insert_one(
        {"slug": slug, "name": "Stuck Co", "status": main.STORE_STATUS_PROVISIONING,
         "created_at": old, "updated_at": old}
    )

    result = await main.reconcile_stores(engine)

    assert slug in result["retried"], result
    assert await _status(engine, slug) == main.STORE_STATUS_READY
    assert f"{slug}_stores" in await _physical_names(engine)
    assert slug in main.KNOWN_STORES


async def test_reconcile_detects_and_drops_orphans(app):
    engine = app.state.engine
    slug = _slug("orphan")
    raw_db = engine.connection_manager.mongo_db
    # An orphan: a "{slug}_stores" collection with no registry row (e.g. a
    # crash before the first registry write, or a partial delete).
    await raw_db[f"{slug}_stores"].insert_one({"name": "Orphan"})
    await raw_db[f"{slug}_items"].insert_one({"name": "Orphan item"})

    found = await main.reconcile_stores(engine)
    assert slug in found["orphans"], found

    dropped = await main.reconcile_stores(engine, drop_orphans=True)
    assert any(name.startswith(f"{slug}_") for name in dropped["dropped"]), dropped
    names = await _physical_names(engine)
    assert not any(n.startswith(f"{slug}_") for n in names)


async def test_reconcile_recovers_stranded_deleting(app):
    engine = app.state.engine
    slug = _slug("stuckdel")
    # Simulate a delete that crashed mid-drop: a "deleting" row (old enough to
    # be past the stuck cutoff) with its collections still present.
    await main.provision_store(engine, slug, "Half-deleted")
    old = datetime.now(timezone.utc) - timedelta(minutes=main.PROVISION_STUCK_MINUTES + 5)
    reg = await _registry(engine)
    await reg.update_one(
        {"slug": slug}, {"$set": {"status": main.STORE_STATUS_DELETING, "updated_at": old}}
    )

    result = await main.reconcile_stores(engine)

    assert slug in result["deleted"], result
    assert await reg.find_one({"slug": slug}) is None
    assert not any(n.startswith(f"{slug}_") for n in await _physical_names(engine))


async def test_reconcile_leaves_fresh_deleting_alone(app):
    """A just-started delete on a peer worker must not be disturbed."""
    engine = app.state.engine
    slug = _slug("freshdel")
    await main.provision_store(engine, slug, "Mid-delete")
    reg = await _registry(engine)
    # "deleting" but touched just now → within the cutoff → left alone.
    await reg.update_one(
        {"slug": slug},
        {"$set": {"status": main.STORE_STATUS_DELETING, "updated_at": datetime.now(timezone.utc)}},
    )

    result = await main.reconcile_stores(engine)
    assert slug not in result["deleted"], result
    assert await reg.find_one({"slug": slug}) is not None


async def test_delete_drops_stores_collection_last(app):
    """Orphan detection keys on {slug}_stores, so it must be dropped last."""
    engine = app.state.engine
    slug = _slug("droporder")
    await main.provision_store(engine, slug, "Order")
    dropped = await main._drop_store_collections(engine, slug)
    assert dropped, dropped
    assert dropped[-1] == f"{slug}_stores", dropped


# ── Cache invalidation (#9) ──────────────────────────────────────────────


async def test_refresh_reflects_direct_registry_edits(app):
    engine = app.state.engine
    slug = _slug("cache")
    reg = await _registry(engine)

    # A ready row shows up after refresh…
    await reg.insert_one(
        {"slug": slug, "name": "Cache Co", "status": main.STORE_STATUS_READY,
         "created_at": datetime.now(timezone.utc)}
    )
    known = await main.refresh_known_stores()
    assert slug in known

    # …and flipping it to archived removes it on the next refresh.
    await reg.update_one({"slug": slug}, {"$set": {"status": main.STORE_STATUS_ARCHIVED}})
    known = await main.refresh_known_stores()
    assert slug not in known


async def test_status_less_rows_are_routable(app):
    """Back-compat: registry rows written before ``status`` existed still route."""
    engine = app.state.engine
    slug = _slug("legacy")
    reg = await _registry(engine)
    await reg.insert_one({"slug": slug, "name": "Legacy", "created_at": datetime.now(timezone.utc)})

    assert slug in await main.refresh_known_stores()
    assert await main._store_is_registered(slug) is True


# ── Rename (#5) ──────────────────────────────────────────────────────────


async def test_rename_updates_registry_and_storefront(admin_client, app):
    engine = app.state.engine
    slug = _slug("rename")
    await main.provision_store(engine, slug, "Old Name")

    res = await admin_client.patch(f"/manage/stores/{slug}", json={"name": "Fresh Name"})
    assert res.status_code == 200, res.text

    doc = await (await _registry(engine)).find_one({"slug": slug})
    assert doc["name"] == "Fresh Name"
    store_db = await engine.get_scoped_db(slug)
    singleton = await store_db["stores"].find_one({})
    assert singleton["name"] == "Fresh Name"


async def test_rename_requires_name(admin_client, app):
    slug = _slug("rename")
    await main.provision_store(app.state.engine, slug, "Keep")
    res = await admin_client.patch(f"/manage/stores/{slug}", json={"name": "  "})
    assert res.status_code == 422, res.text


async def test_rename_unknown_store_404s(admin_client):
    res = await admin_client.patch("/manage/stores/nope-not-real", json={"name": "X"})
    assert res.status_code == 404, res.text


# ── Archive / restore (#5) ───────────────────────────────────────────────


async def test_archive_hides_storefront_and_restore_reenables(admin_client, anon_client, app):
    engine = app.state.engine
    slug = _slug("arch")
    await main.provision_store(engine, slug, "Archivable")

    # Routable while ready.
    assert (await anon_client.get(f"/{slug}/")).status_code == 200

    arch = await admin_client.post(f"/manage/stores/{slug}/archive")
    assert arch.status_code == 200, arch.text
    assert await _status(engine, slug) == main.STORE_STATUS_ARCHIVED
    assert slug not in main.KNOWN_STORES
    # Archived → 404 (cache cleared AND the status-aware fallback rejects it).
    assert (await anon_client.get(f"/{slug}/")).status_code == 404

    restore = await admin_client.post(f"/manage/stores/{slug}/restore")
    assert restore.status_code == 200, restore.text
    assert await _status(engine, slug) == main.STORE_STATUS_READY
    assert (await anon_client.get(f"/{slug}/")).status_code == 200


async def test_archive_rejects_platform(admin_client):
    res = await admin_client.post(f"/manage/stores/{main.PLATFORM_SLUG}/archive")
    assert res.status_code == 400, res.text


# ── Delete / deprovision (#5) ────────────────────────────────────────────


async def test_delete_drops_collections_and_clears_cache(admin_client, anon_client, app):
    engine = app.state.engine
    slug = _slug("del")
    await main.provision_store(engine, slug, "Deletable")
    # Give it a little content so there are several {slug}_* collections.
    await admin_client.post(f"/{slug}/api/items", json={"name": "X", "item_code": "DEL-1"})

    before = await _physical_names(engine)
    assert any(n.startswith(f"{slug}_") for n in before)

    res = await admin_client.request(
        "DELETE", f"/manage/stores/{slug}", json={"confirm": slug}
    )
    assert res.status_code == 200, res.text
    assert res.json()["dropped"], res.text

    after = await _physical_names(engine)
    assert not any(n.startswith(f"{slug}_") for n in after)
    assert await (await _registry(engine)).find_one({"slug": slug}) is None
    assert slug not in main.KNOWN_STORES
    assert (await anon_client.get(f"/{slug}/")).status_code == 404


async def test_delete_requires_matching_confirm(admin_client, app):
    engine = app.state.engine
    slug = _slug("del")
    await main.provision_store(engine, slug, "Guarded")

    missing = await admin_client.request("DELETE", f"/manage/stores/{slug}", json={})
    assert missing.status_code == 422, missing.text
    wrong = await admin_client.request(
        "DELETE", f"/manage/stores/{slug}", json={"confirm": "not-the-slug"}
    )
    assert wrong.status_code == 422, wrong.text
    # Still present and routable.
    assert await (await _registry(engine)).find_one({"slug": slug}) is not None


async def test_delete_rejects_platform(admin_client):
    res = await admin_client.request(
        "DELETE", f"/manage/stores/{main.PLATFORM_SLUG}", json={"confirm": main.PLATFORM_SLUG}
    )
    assert res.status_code == 400, res.text


# ── Audit trail ──────────────────────────────────────────────────────────


async def _audit_events(engine, slug: str) -> list[str]:
    reg = await main._platform_db(engine)
    return [d.get("event") async for d in reg["audit_log"].find({"slug": slug})]


async def test_lifecycle_writes_audit_trail(admin_client, app):
    engine = app.state.engine
    slug = _slug("audit")

    # create → archive → restore → delete, all via the admin endpoints.
    await admin_client.post("/manage/stores", json={"slug": slug, "name": "Audited"})
    await admin_client.patch(f"/manage/stores/{slug}", json={"name": "Audited v2"})
    await admin_client.post(f"/manage/stores/{slug}/archive")
    await admin_client.post(f"/manage/stores/{slug}/restore")
    await admin_client.request("DELETE", f"/manage/stores/{slug}", json={"confirm": slug})

    events = await _audit_events(engine, slug)
    for expected in (
        "store_created",
        "store_renamed",
        "store_archived",
        "store_restored",
        "store_deleted",
    ):
        assert expected in events, (expected, events)

    # The trail records the acting admin, and survives the store's deletion
    # (it lives in the platform scope, not the dropped {slug}_* collections).
    reg = await main._platform_db(engine)
    created = await reg["audit_log"].find_one({"slug": slug, "event": "store_created"})
    assert created is not None
    assert "@" in str(created.get("actor", "")), created


async def test_delete_unknown_store_404s(admin_client):
    res = await admin_client.request(
        "DELETE", "/manage/stores/nope-not-real", json={"confirm": "nope-not-real"}
    )
    assert res.status_code == 404, res.text


# ── Auth gating ──────────────────────────────────────────────────────────


async def test_lifecycle_endpoints_require_admin(anon_client, app):
    slug = _slug("auth")
    await main.provision_store(app.state.engine, slug, "Gated")

    rename = await anon_client.patch(f"/manage/stores/{slug}", json={"name": "X"})
    assert rename.status_code in (401, 403), rename.text
    archive = await anon_client.post(f"/manage/stores/{slug}/archive")
    assert archive.status_code in (401, 403), archive.text
    delete = await anon_client.request("DELETE", f"/manage/stores/{slug}", json={"confirm": slug})
    assert delete.status_code in (401, 403), delete.text
    reconcile = await anon_client.post("/manage/reconcile", json={})
    assert reconcile.status_code in (401, 403), reconcile.text
    # The store survived every rejected call.
    assert await main._store_is_registered(slug) is True
