"""
AI Stores — layered, per-namespace role-based access control.

The engine (``mdb-engine``) resolves *who you are* globally (one shared
``users`` pool, one session cookie). This module layers *what you can do*
**per namespace** on top of that, without forking the engine's auth:

    * A user's membership of a handle lives in the platform-scope
      ``namespace_members`` collection as ``{handle, email, role}``.
    * ``effective_engine_roles`` maps a namespace role (or platform-superuser
      status) to the engine role list the request should carry
      (``request.state.user_roles``). ``owner``/``editor`` → ``["admin"]`` so
      the engine's ``write_roles: ["admin"]`` checks pass; ``viewer`` →
      ``["viewer"]`` (read-only); non-members → ``[]``.

Ownership, team membership, and store creation are all namespace-level: an
owner/editor/viewer of a handle holds that role across every store under it.
This keeps enforcement a single, per-request effective-role rewrite.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Per-namespace roles, most to least privileged.
ROLE_OWNER = "owner"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"
NAMESPACE_ROLES = (ROLE_OWNER, ROLE_EDITOR, ROLE_VIEWER)

# Roles that may edit store content (mapped to the engine's write-capable
# ``admin`` role). ``viewer`` is intentionally excluded — read-only.
WRITE_ROLES = frozenset({ROLE_OWNER, ROLE_EDITOR})
# Only owners may manage the team and create/delete stores in the namespace.
OWNER_ROLES = frozenset({ROLE_OWNER})


def normalize_email(email: Any) -> str:
    """Lowercase + strip an email for stable membership keys."""
    return str(email or "").strip().lower()


def is_platform_superuser(user: dict[str, Any] | None) -> bool:
    """Whether the (global) user is the platform admin — access to every namespace."""
    return bool(user) and str((user or {}).get("role") or "").lower() == "admin"


async def get_namespace_role(members_col, handle: str, email: str) -> str | None:
    """Return the user's role for ``handle`` (``owner``/``editor``/``viewer``) or ``None``.

    ``members_col`` is the platform-scope ``namespace_members`` collection
    (e.g. ``await _platform_db(engine))["namespace_members"]``).
    """
    email = normalize_email(email)
    if not handle or not email:
        return None
    doc = await members_col.find_one({"handle": handle, "email": email})
    role = (doc or {}).get("role")
    return role if role in NAMESPACE_ROLES else None


def effective_engine_roles(namespace_role: str | None, *, is_superuser: bool) -> list[str]:
    """Map a namespace role (+ superuser flag) to engine ``user_roles``.

    * platform superuser or owner/editor → ``["admin"]`` (write-capable).
    * viewer → ``["viewer"]`` (read-only; write checks 403).
    * anyone else (non-member) → ``[]``.
    """
    if is_superuser or namespace_role in WRITE_ROLES:
        return ["admin"]
    if namespace_role == ROLE_VIEWER:
        return [ROLE_VIEWER]
    return []


def grants_admin(user_roles: list[str] | None) -> bool:
    """Whether the effective engine roles include write-capable ``admin``."""
    return "admin" in (user_roles or [])


async def add_member(
    members_col, handle: str, email: str, role: str, invited_by: str | None = None
) -> dict[str, Any]:
    """Upsert a namespace membership. Returns the stored ``{handle, email, role}``."""
    email = normalize_email(email)
    now = datetime.now(timezone.utc)
    await members_col.update_one(
        {"handle": handle, "email": email},
        {
            "$set": {"role": role, "updated_at": now},
            "$setOnInsert": {
                "handle": handle,
                "email": email,
                "invited_by": normalize_email(invited_by) or None,
                "created_at": now,
            },
        },
        upsert=True,
    )
    return {"handle": handle, "email": email, "role": role}


async def list_members(members_col, handle: str) -> list[dict[str, Any]]:
    """All memberships for a handle, oldest first."""
    out: list[dict[str, Any]] = []
    async for doc in members_col.find({"handle": handle}).sort("created_at", 1):
        out.append(
            {
                "handle": doc.get("handle"),
                "email": doc.get("email"),
                "role": doc.get("role"),
                "invited_by": doc.get("invited_by"),
            }
        )
    return out


async def count_owners(members_col, handle: str) -> int:
    """Number of ``owner`` memberships for a handle (last-owner guardrail)."""
    return await members_col.count_documents({"handle": handle, "role": ROLE_OWNER})
