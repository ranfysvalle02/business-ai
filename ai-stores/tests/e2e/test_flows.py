"""End-to-end browser flows: signup, quick store, and the AI chat editor.

Run with:  pytest tests/e2e   (needs `python -m playwright install chromium`)
These are excluded from the default suite (see pytest.ini).
"""
from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _unique(prefix: str = "acme") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _signup(page: Page, base_url: str) -> tuple[str, str, str, str]:
    """Complete the signup form and return (handle, store, email, password)."""
    handle = _unique()
    email = f"{handle}@e2e.test"
    password = "e2e-password-123"
    store = "shop"

    page.goto(f"{base_url}/signup")
    page.fill("#email", email)
    page.fill("#password", password)
    # The email->handle autofill may set a different handle; force ours.
    page.fill("#handle", handle)
    page.fill("#store_name", "Acme Coffee")
    page.fill("#slug", store)
    page.click("#signup-submit")
    return handle, store, email, password


def test_signup_lands_in_store_admin(live_server: str, page: Page):
    """A brand-new owner signs up and is dropped straight into their store admin."""
    handle, store, _email, _pw = _signup(page, live_server)
    page.wait_for_url(f"{live_server}/{handle}/{store}/admin/**", timeout=15000)
    assert f"/{handle}/{store}/admin" in page.url


def test_quick_store_from_console(live_server: str, page: Page):
    """After signup, the one-click Quick store button provisions a second store."""
    handle, _store, _email, _pw = _signup(page, live_server)
    page.wait_for_url(f"{live_server}/{handle}/**", timeout=15000)

    # Session cookie is set; open the console and use the Quick store button.
    page.goto(f"{live_server}/manage")
    quick = page.locator("#quick-store-btn")
    expect(quick).to_be_visible()
    quick.click()

    # It navigates to the new store's admin (admin_url), a different store.
    page.wait_for_url(f"{live_server}/{handle}/**/admin/**", timeout=15000)
    assert f"/{handle}/" in page.url and "/admin" in page.url


def test_ai_chat_proposes_and_applies(live_server: str, page: Page, ai_configured: bool):
    """The AI widget turns a request into a diff the owner can Apply."""
    if not ai_configured:
        pytest.skip("GEMINI_API_KEY not configured on the live server")

    handle, store, _email, _pw = _signup(page, live_server)
    page.wait_for_url(f"{live_server}/{handle}/{store}/admin/**", timeout=15000)

    # Open the floating assistant and ask for an unambiguous copy change.
    page.locator(".aic-launcher").click()
    chat = page.locator(".aic-input")
    expect(chat).to_be_visible()
    chat.fill('Change the tagline to "Handmade with care"')
    page.locator(".aic-send").click()

    # A diff card should appear; applying it shows the "Applied." confirmation.
    diff = page.locator(".aic-diff")
    expect(diff).to_be_visible(timeout=45000)
    page.locator(".aic-btn-apply").click()
    expect(page.locator(".aic-diff-warn")).to_contain_text("Applied", timeout=30000)
