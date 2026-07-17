"""Security-posture regression guards.

We deliberately keep the engine's ``csrf_protection: false`` (see the rationale
in [SCALE.md]): the admin session cookie is CSRF-mitigated by ``SameSite=Lax`` +
``HttpOnly`` (and the absence of CORS), and the only public write path
(``/api/submit-inquiry``) needs no CSRF by design. Because that mitigation lives
in ``mdb-engine`` rather than our code, this test locks the guarantee in — a
future engine upgrade that silently weakened the cookie flags fails CI here
instead of shipping.
"""
from __future__ import annotations

import os


def _session_set_cookie(response) -> str:
    """Return the ``Set-Cookie`` header value for the app session cookie.

    The engine names it ``{session_cookie_name}_{slug}`` → ``ais_session_ai-stores``;
    a substring match on ``ais_session`` is resilient to that suffix.
    """
    for raw in response.headers.get_list("set-cookie"):
        if "ais_session" in raw:
            return raw
    return ""


async def test_login_cookie_is_httponly_and_samesite_lax(anon_client):
    res = await anon_client.post(
        "/auth/login",
        json={
            "email": os.environ["ADMIN_EMAIL"],
            "password": os.environ["ADMIN_PASSWORD"],
        },
    )
    assert res.status_code < 300, f"admin login failed: {res.status_code} {res.text}"

    cookie = _session_set_cookie(res)
    assert cookie, f"no session Set-Cookie found in {res.headers.get_list('set-cookie')}"

    lowered = cookie.lower()
    # HttpOnly → the session token is unreadable from JS (XSS can't exfiltrate it).
    assert "httponly" in lowered, cookie
    # SameSite=Lax → browsers withhold the cookie on cross-site state-changing
    # requests. This is the CSRF mitigation we rely on with csrf_protection off,
    # so it must never silently regress.
    assert "samesite=lax" in lowered, cookie
