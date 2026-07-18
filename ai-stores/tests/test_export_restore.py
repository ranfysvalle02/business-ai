"""Per-store export / restore — the pragmatic blast-radius mitigation.

Exercises the JSON round-trip (owner-gated), the auto-provision-on-import
behaviour, and the non-empty-target guard (``409`` unless ``?overwrite=1``).
"""
from __future__ import annotations

import uuid

_JSON = {"Content-Type": "application/json"}


async def _seed_item(client, path: str, code: str):
    return await client.post(
        f"{path}/api/items",
        json={"name": f"Export {code}", "item_code": code, "price": 5.0, "status": "Available"},
    )


async def test_export_import_round_trip(owner_client, stores):
    code = f"EXP-{uuid.uuid4().hex[:8]}"
    assert (await _seed_item(owner_client, "/acme/shop", code)).status_code in (200, 201)

    exported = await owner_client.get("/manage/stores/acme/shop/export")
    assert exported.status_code == 200, exported.text
    assert code in exported.text
    assert exported.headers["content-type"].startswith("application/json")

    target = f"restored{uuid.uuid4().hex[:6]}"
    restored = await owner_client.post(
        f"/manage/stores/acme/{target}/import", content=exported.text, headers=_JSON
    )
    assert restored.status_code == 200, restored.text

    listing = await owner_client.get(f"/acme/{target}/api/items")
    assert listing.status_code == 200
    assert code in listing.text


async def test_import_guards_nonempty_target(owner_client, stores):
    exported = (await owner_client.get("/manage/stores/acme/shop/export")).text
    target = f"guard{uuid.uuid4().hex[:6]}"

    # First import auto-provisions the (empty) target → OK.
    first = await owner_client.post(
        f"/manage/stores/acme/{target}/import", content=exported, headers=_JSON
    )
    assert first.status_code == 200, first.text

    # Re-importing into the now-populated store is refused without overwrite.
    second = await owner_client.post(
        f"/manage/stores/acme/{target}/import", content=exported, headers=_JSON
    )
    assert second.status_code == 409

    # ?overwrite=1 replaces it.
    third = await owner_client.post(
        f"/manage/stores/acme/{target}/import?overwrite=1", content=exported, headers=_JSON
    )
    assert third.status_code == 200, third.text


async def test_export_is_owner_gated(anon_client, viewer_client, stores):
    assert (await anon_client.get("/manage/stores/acme/shop/export")).status_code == 401
    # A viewer of acme is not an owner → forbidden.
    assert (await viewer_client.get("/manage/stores/acme/shop/export")).status_code == 403
