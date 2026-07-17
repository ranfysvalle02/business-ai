"""Abuse protection: honeypot silent-drop + per-IP/store rate limiting."""
from __future__ import annotations

import uuid


async def test_honeypot_silently_drops(admin_client, anon_client, stores):
    marker = f"honey-{uuid.uuid4().hex[:8]}"
    res = await anon_client.post(
        "/acme/api/submit-inquiry",
        json={
            "customer_name": marker,
            "customer_contact": "bot@example.com",
            "message": "I am a bot",
            "company_website": "http://spam.example",  # bait field
        },
    )
    # The bot gets a convincing 201, but nothing is stored.
    assert res.status_code == 201, res.text
    inq = await admin_client.get("/acme/api/inquiries")
    assert marker not in inq.text


async def test_inquiry_rate_limit_returns_429(app, anon_client, stores):
    import main

    # Override just the inquiry limit to a tiny, long window for determinism,
    # scoped to a dedicated store so no other test's counts interfere.
    slug = f"rl{uuid.uuid4().hex[:6]}"
    await main.provision_store(app.state.engine, slug, "Rate Limited")

    from mdb_engine.auth.rate_limiter import RateLimit

    original = app.state.rate_limits.get("inquiry")
    app.state.rate_limits["inquiry"] = [RateLimit(max_attempts=3, window_seconds=3600)]
    try:
        statuses = []
        for i in range(4):
            r = await anon_client.post(
                f"/{slug}/api/submit-inquiry",
                json={"customer_name": f"rl-{i}", "customer_contact": "x@example.com"},
            )
            statuses.append(r.status_code)
        assert statuses[:3] == [201, 201, 201], statuses
        assert statuses[3] == 429, statuses
    finally:
        if original is not None:
            app.state.rate_limits["inquiry"] = original
