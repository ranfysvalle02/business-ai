"""Observability surfaces: liveness, readiness, and the superuser status page.

These back the operational story (load-balancer probes + an at-a-glance
platform snapshot) and enforce that ``/manage/status`` stays superuser-only.
"""
from __future__ import annotations


async def test_healthz_is_public_and_liveness_only(anon_client):
    res = await anon_client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


async def test_healthz_echoes_request_id_header(anon_client):
    res = await anon_client.get("/healthz")
    # RequestIDMiddleware always stamps a correlation id on the response.
    assert res.headers.get("x-request-id")


async def test_readyz_reports_mongo_reachable(anon_client, stores):
    res = await anon_client.get("/readyz")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["mongo"] is True
    assert data["stores"] >= 3  # at least the session's provisioned stores
    assert "ai_configured" in data


async def test_status_requires_superuser(anon_client, outsider_client):
    assert (await anon_client.get("/manage/status")).status_code == 401
    assert (await outsider_client.get("/manage/status")).status_code == 401


async def test_status_snapshot_for_superuser(admin_client, stores):
    res = await admin_client.get("/manage/status")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["mongo"] is True
    assert data["stores"]["total"] >= 3
    assert data["namespaces"] >= 2  # acme + globex
    assert "by_status" in data["stores"]
    assert "quotas" in data
    assert "ai" in data and "configured" in data["ai"]
