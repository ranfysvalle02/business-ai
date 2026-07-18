"""
AI Stores — a multi-tenant shell over ``mdb-engine`` with per-user namespaces.

One app instance serves **many stores** from a single entrypoint. A user
claims a **handle** (their namespace) and owns every store under it at
``stores.com/{handle}/{store}``. Each store is a per-request database *scope*
``{handle}__{store}``: the engine prefixes every collection with it
(``{handle}__{store}_items``) and tags documents with an ``app_id``, so stores
are fully isolated while sharing one deployment and one Mongo database.

Identity stays **global** (one ``mdb-engine`` ``users`` pool + session cookie);
authorization is layered **per namespace** on top — owner/editor/viewer roles
in ``namespace_members`` are mapped to the engine's effective ``user_roles`` by
``StoreRoleOverlayMiddleware`` (see ``rbac.py``). The seeded ``ADMIN_EMAIL`` is
the platform superuser with access to every namespace.

Everything about the domain (collections, auto-CRUD, auth, SSR routes,
indexes, admin plane, reconciler, trash sweeper) is declared in
``manifest.json`` and wired by ``mdb_engine.quickstart``.

This module keeps only what does not belong in the manifest:

    * ``StoreScopeMiddleware`` + a ``get_scoped_db`` override that resolve
      ``/{handle}/{store}/...`` to the right scope per request.
    * ``StoreRoleOverlayMiddleware`` that applies per-namespace RBAC without
      forking the engine's global auth.
    * Runtime store provisioning (``provision_store``) and a ``store_registry``
      held in the platform scope, plus the role-aware ``/manage`` console and
      ``/{handle}/`` landing — no redeploy needed to add a store.
    * Namespace team management (owner-gated invites) and public self-serve
      ``/signup`` to claim a handle and open a first store.
    * Static asset mount for ``/static`` (PWA icons, service worker, css/js).
    * SSR route mounting (reads the manifest ``ssr`` block).
    * A public ``POST /api/submit-inquiry`` endpoint so unauthenticated
      visitors can submit leads without opening the ``inquiries`` collection
      to anonymous writes.
    * Cloudinary-backed ``POST /admin/upload-image`` / ``/admin/upload-video``
      endpoints gated to a store's owner/editor (or the platform superuser).
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import cloudinary
import cloudinary.uploader
from bson import json_util
from cloudinary.utils import cloudinary_url
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

from mdb_engine import get_current_user, quickstart
from mdb_engine.auth.jwt import decode_jwt_token, encode_jwt_token
from mdb_engine.auth.rate_limiter import RateLimit, create_rate_limit_store
from mdb_engine.auth.users import create_app_user
from mdb_engine.dependencies import get_scoped_db, get_user_roles
from mdb_engine.env import get_jwt_secret
from mdb_engine.indexes import run_index_creation_for_collection
from mdb_engine.routing._ssr import mount_ssr_routes

# Load .env before importing local modules so their module-level configuration
# (notably ai_editor's GEMINI_* settings) picks up values from .env on local and
# `uvicorn`-driven runs, not just when the vars are exported (e.g. via compose).
load_dotenv()

import ai_editor  # noqa: E402
import notifications  # noqa: E402
import rbac  # noqa: E402

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ai-stores")

BASE_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = BASE_DIR / "manifest.json"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

APP_NAME = "AI Stores"

_cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "")
_cloud_key = os.getenv("CLOUDINARY_API_KEY", "")
_cloud_secret = os.getenv("CLOUDINARY_API_SECRET", "")
if _cloud_name and _cloud_key and _cloud_secret:
    cloudinary.config(cloud_name=_cloud_name, api_key=_cloud_key, api_secret=_cloud_secret)
    logger.info("Cloudinary configured")
else:
    logger.warning("Cloudinary not configured — /admin/upload-* will return 503")

_manifest_data = json.loads(MANIFEST_PATH.read_text())

STORE_TEMPLATE_PATH = BASE_DIR / "store_template.json"
# Default starter content copied into each new store scope (name/slug overridden
# per store). Used for "retail" and as the fallback for any unknown/blank type.
_STORE_TEMPLATE: dict[str, list[dict[str, Any]]] = json.loads(STORE_TEMPLATE_PATH.read_text())

# Alternate starter templates by business type. Each file mirrors the shape of
# ``store_template.json`` (stores/sections/items/specials/slideshow) with content
# tuned for that vertical. Add a vertical by dropping a ``store_template.<key>.json``
# here and listing the key in ``STORE_TEMPLATES`` (which drives the create/signup UIs).
_ALT_TEMPLATE_FILES: dict[str, Path] = {
    "restaurant": BASE_DIR / "store_template.restaurant.json",
}
# Business types offered in the create-store + signup UIs. "retail" is the default
# template above; the rest map to ``_ALT_TEMPLATE_FILES``.
STORE_TEMPLATES: tuple[str, ...] = ("retail", *sorted(_ALT_TEMPLATE_FILES))


def _load_store_template(business_type: str | None) -> dict[str, list[dict[str, Any]]]:
    """Return a fresh copy of the starter template for ``business_type``.

    Falls back to the default (retail) template for a blank/unknown type or an
    unreadable file, so provisioning is never blocked by a bad template key.
    """
    key = (business_type or "").strip().lower()
    path = _ALT_TEMPLATE_FILES.get(key)
    if path is not None:
        try:
            return json.loads(path.read_text())
        except Exception as exc:  # noqa: BLE001 — fall back to the default template
            logger.warning(f"store template '{key}' unreadable, using default: {exc}")
    return copy.deepcopy(_STORE_TEMPLATE)

# ── Multi-tenant runtime configuration ─────────────────────────────────
#
# The platform scope holds cross-store data: the single global admin ``users``
# pool (seeded by the engine from ``auth.users``) and the ``store_registry``.
# Each store scope holds its own ``stores`` singleton, ``items``, ``sections``,
# ``specials``, ``slideshow`` and ``inquiries``.
PLATFORM_SLUG: str = _manifest_data["slug"]

# First path segments that are global — never a namespace, never scoped.
RESERVED_SEGMENTS = frozenset(
    {"static", "__mdb", "health", "healthz", "readyz", "favicon.ico", "robots.txt", "auth", "manage", "signup"}
)
# Slugs that would collide with a global route's first segment (handles), or
# with a store's own sub-paths (stores), or with the platform scope's own
# ``{PLATFORM_SLUG}_*`` collections. Reused for both handles and store slugs.
BANNED_SLUGS = RESERVED_SEGMENTS | frozenset(
    {"admin", "api", "item", "contact", "sitemap.xml", "sitemap", "www", "invite", PLATFORM_SLUG}
)
# 3–40 chars, lowercase alnum + hyphen, no leading/trailing hyphen. Because a
# validated slug never contains ``_``, the ``__`` scope join below is always an
# unambiguous, reversible separator between a handle and a store slug.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")

# Physical engine scope for a store = ``{handle}__{store}`` (double-underscore
# join). One Mongo database holds every store; the engine prefixes each
# collection with this scope (``{handle}__{store}_items``).
_SCOPE_SEP = "__"


def scope_id(handle: str, store: str) -> str:
    """Composite engine scope for a (handle, store) pair."""
    return f"{handle}{_SCOPE_SEP}{store}"


def split_scope(scope: str) -> tuple[str, str] | None:
    """Reverse ``scope_id`` → ``(handle, store)``; ``None`` if not a store scope."""
    parts = scope.split(_SCOPE_SEP)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]

# ── Store lifecycle status ─────────────────────────────────────────────
#
# Each ``store_registry`` doc carries a ``status`` that is the single source
# of truth for what routes. A missing status is treated as ``ready`` so rows
# created before this field existed keep working.
STORE_STATUS_PROVISIONING = "provisioning"
STORE_STATUS_READY = "ready"
STORE_STATUS_ARCHIVED = "archived"
STORE_STATUS_FAILED = "failed"
STORE_STATUS_DELETING = "deleting"

# Routing caches, rebuilt from the registry at startup, on every lifecycle
# mutation, on a short TTL, and on registry change-stream events so
# multi-worker deployments converge. On a cache miss the middleware still
# falls back to a status-aware registry lookup.
#   * ``KNOWN_HANDLES`` — every handle that owns at least one routable store.
#   * ``KNOWN_STORES``  — composite ``{handle}__{store}`` scope ids (status=ready).
KNOWN_HANDLES: set[str] = set()
KNOWN_STORES: set[str] = set()

def _not_found_html(title: str, message: str) -> str:
    return (
        f"<!doctype html><meta charset='utf-8'><title>{title}</title>"
        "<div style=\"font-family:system-ui;max-width:32rem;margin:12vh auto;"
        "text-align:center;background:#0b1120;color:#e2e8f0;padding:2rem 1.5rem;"
        f"border-radius:12px\"><h1 style='font-size:1.5rem;margin:0 0 .5rem'>"
        f"{title}</h1><p style='color:#94a3b8;margin:0'>{message}</p></div>"
    )


_STORE_404_HTML = _not_found_html(
    "Store not found", "No store is published at this address."
)
_HANDLE_404_HTML = _not_found_html(
    "Namespace not found", "No one has claimed this namespace."
)

app = quickstart(
    slug=_manifest_data["slug"],
    name=_manifest_data.get("name", APP_NAME),
    manifest=MANIFEST_PATH,
    title=_manifest_data.get("name", APP_NAME),
    description=_manifest_data.get("description", ""),
)

_ssr_cfg = _manifest_data.get("ssr", {})
if _ssr_cfg.get("enabled") and TEMPLATES_DIR.is_dir():
    mount_ssr_routes(
        app,
        TEMPLATES_DIR,
        _ssr_cfg,
        collections_config=_manifest_data.get("collections", {}),
    )

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── Multi-tenant request scoping ───────────────────────────────────────


async def _platform_db(engine):
    """Scoped DB for the platform scope, supplying the app token when needed.

    When ``MDB_ENGINE_MASTER_KEY`` is set the engine stores a secret for the
    primary (platform) slug, so ``get_scoped_db`` requires that slug's token.
    Store scopes never store a secret, so they need no token. This helper
    centralises the platform token lookup (cached by the engine after startup).
    """
    token = engine.get_app_token(PLATFORM_SLUG)
    if token is None:
        try:
            token = await engine.auto_retrieve_app_token(PLATFORM_SLUG)
        except Exception:  # noqa: BLE001 — secrets manager off / no secret
            token = None
    return await engine.get_scoped_db(PLATFORM_SLUG, app_token=token)


def _routable_status_query() -> dict[str, Any]:
    """Registry filter for stores that should be publicly routable.

    A missing ``status`` counts as ready so rows created before the field
    existed keep routing.
    """
    return {"$or": [{"status": STORE_STATUS_READY}, {"status": {"$exists": False}}]}


async def refresh_known_stores() -> set[str]:
    """Rebuild ``KNOWN_HANDLES`` + ``KNOWN_STORES`` from the registry (ready only).

    Reassigns the module-level sets atomically so in-flight requests always
    read a complete snapshot. Returns the new store-scope set (handy in tests).
    Never raises — a refresh error leaves the previous caches in place.
    """
    global KNOWN_HANDLES, KNOWN_STORES
    try:
        reg = await _platform_db(app.state.engine)
        handles: set[str] = set()
        stores: set[str] = set()
        async for doc in reg["store_registry"].find(
            _routable_status_query(), {"handle": 1, "store": 1}
        ):
            handle, store = doc.get("handle"), doc.get("store")
            if handle and store:
                handles.add(handle)
                stores.add(scope_id(handle, store))
        KNOWN_HANDLES = handles
        KNOWN_STORES = stores
        return stores
    except Exception as exc:  # noqa: BLE001 — never crash on a refresh error
        logger.warning(f"store cache refresh skipped: {exc}")
        return KNOWN_STORES


async def _store_is_registered(handle: str, store: str) -> bool:
    """Status-aware registry lookup for a (handle, store) on a cache miss.

    Only ``ready`` (or legacy status-less) stores route; ``archived``,
    ``deleting``, ``provisioning`` and ``failed`` stores return ``False`` so
    they 404 across every worker.
    """
    try:
        reg = await _platform_db(app.state.engine)
        found = await reg["store_registry"].find_one(
            {"handle": handle, "store": store, **_routable_status_query()}
        )
        return found is not None
    except Exception:  # noqa: BLE001 — never fail a request on a lookup error
        return False


async def _handle_is_registered(handle: str) -> bool:
    """Whether a handle owns at least one routable store (cache-miss fallback)."""
    try:
        reg = await _platform_db(app.state.engine)
        found = await reg["store_registry"].find_one(
            {"handle": handle, **_routable_status_query()}
        )
        return found is not None
    except Exception:  # noqa: BLE001 — never fail a request on a lookup error
        return False


class StoreScopeMiddleware:
    """Resolve ``/{handle}/{store}/...`` to a per-request database scope.

    Added after ``quickstart`` so it is the outermost middleware — it runs
    before the engine's auth middleware and the router. For a known store it
    sets ``root_path`` to the ``/{handle}/{store}`` prefix: Starlette then
    routes on the remaining path (``get_route_path``) so the engine's existing
    routes match unchanged, while ``request.url`` / ``base_url`` keep the prefix
    so canonical URLs, sitemaps and OG tags stay per-store correct.

    Routing decisions on the first two path segments:
      * reserved 1st segment (``manage``, ``auth``, ``static``, ``signup``, …)
        → platform scope (no rewrite).
      * unknown handle → 404.
      * known handle, no store segment → the ``/{handle}/`` namespace landing,
        served by rewriting the path to the platform route ``/manage/ns/{handle}``.
      * unknown store within a known handle → 404.
      * known ``(handle, store)`` → scope the request to ``{handle}__{store}``.

    Auth is untouched here — global identity is resolved by the engine's auth
    middleware; per-namespace authorization is layered on by
    ``StoreRoleOverlayMiddleware`` (which runs after auth).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        raw_path = scope.get("path") or "/"

        # Bare root → the management console (sign in / sign up from there).
        if raw_path == "/":
            await self._send_redirect(send, "/manage")
            return

        segments = raw_path.lstrip("/").split("/")
        handle = segments[0]

        # Global, un-scoped surfaces (static, auth, manage, signup, health, …).
        if not handle or handle in RESERVED_SEGMENTS:
            await self.app(scope, receive, send)
            return

        # First segment must be a known handle (namespace). Fall back to the
        # registry on a cache miss so every worker converges.
        if handle not in KNOWN_HANDLES:
            if not await _handle_is_registered(handle):
                await self._send_html(send, 404, _HANDLE_404_HTML)
                return
            KNOWN_HANDLES.add(handle)

        store = segments[1] if len(segments) > 1 else ""

        # ``/{handle}`` or ``/{handle}/`` → public namespace landing. Rewrite to
        # a platform route so it is served without a store scope. GET-only.
        if store == "":
            if scope.get("method", "GET").upper() != "GET":
                await self._send_html(send, 404, _HANDLE_404_HTML)
                return
            landing = f"/manage/ns/{handle}"
            scope["path"] = landing
            scope["raw_path"] = landing.encode("utf-8")
            await self.app(scope, receive, send)
            return

        # Second segment must be a known store within this handle.
        composite = scope_id(handle, store)
        if composite not in KNOWN_STORES:
            if not await _store_is_registered(handle, store):
                await self._send_html(send, 404, _STORE_404_HTML)
                return
            KNOWN_STORES.add(composite)

        # Scope this request to the store.
        prefix = f"/{handle}/{store}"
        scope["handle"] = handle
        scope["store"] = store
        scope["store_slug"] = composite
        scope["root_path"] = scope.get("root_path", "") + prefix
        if raw_path[len(prefix):] == "":
            # "/h/s" → "/h/s/" so get_route_path() yields "/" for the home route.
            scope["path"] = raw_path + "/"
            scope["raw_path"] = (raw_path + "/").encode("utf-8")

        await self.app(scope, receive, send)

    @staticmethod
    async def _send_redirect(send, location: str, status: int = 307) -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"location", location.encode("utf-8")), (b"content-length", b"0")],
        })
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    async def _send_html(send, status: int, html: str) -> None:
        body = html.encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


app.add_middleware(StoreScopeMiddleware)


class RequestIDMiddleware:
    """Assign every request an ``X-Request-ID`` and emit one structured log line.

    Pure-ASGI and installed **outermost** (after ``StoreScopeMiddleware`` below,
    so it wraps it), so even the middleware's own 404s get an id + access log.
    Honours an inbound ``X-Request-ID`` (e.g. from a proxy) and echoes it back on
    the response so a client log line can be correlated to a server one.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        inbound = dict(scope.get("headers") or {}).get(b"x-request-id")
        rid = inbound.decode("latin-1")[:64] if inbound else uuid.uuid4().hex[:16]
        scope["request_id"] = rid
        started = time.perf_counter()
        status_code = 0

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message["headers"] = [*message.get("headers", []), (b"x-request-id", rid.encode("latin-1"))]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            logger.info(
                "req id=%s %s %s -> %s %.1fms",
                rid, scope.get("method"), scope.get("path"),
                status_code, (time.perf_counter() - started) * 1000.0,
            )


app.add_middleware(RequestIDMiddleware)


class StoreRoleOverlayMiddleware(BaseHTTPMiddleware):
    """Layer per-namespace authorization onto the engine's global identity.

    Runs **after** the engine's auth middleware (installed as the innermost
    middleware, see below), so ``request.state.user`` is already resolved. For
    a store-scoped request it:

      * **always** rewrites ``request.state.user_roles`` from the caller's
        membership of ``scope["handle"]`` (this alone gates ``/api`` writes,
        since ``public_read`` GETs never consult roles); and
      * hard-blocks **admin-surface** paths (``/admin*`` under the store) for
        non-members by nulling ``request.state.user`` so the engine's SSR
        ``auth: true`` gate and the app's admin endpoints return 401/403.

    Public storefront paths are never restricted: an anonymous or logged-in
    non-member can always browse ``/{handle}/{store}/`` and submit inquiries.
    """

    async def dispatch(self, request: Request, call_next):
        scope = request.scope
        handle = scope.get("handle")
        if not handle:
            # Platform / non-store request (/manage, /auth, /signup, static, …).
            return await call_next(request)

        user = getattr(request.state, "user", None)

        # Anonymous visitors: no admin, and no membership lookup needed. Public
        # reads + the public submit-inquiry endpoint are unaffected.
        if user is None:
            request.state.user_roles = []
            return await call_next(request)

        superuser = rbac.is_platform_superuser(user)
        ns_role: str | None = None
        if not superuser:
            try:
                pdb = await _platform_db(request.app.state.engine)
                ns_role = await rbac.get_namespace_role(
                    pdb["namespace_members"], handle, user.get("email")
                )
            except Exception:  # noqa: BLE001 — a lookup error must never grant access
                ns_role = None

        roles = rbac.effective_engine_roles(ns_role, is_superuser=superuser)
        request.state.user_roles = roles

        # Admin surface = the sub-path after the /{handle}/{store} prefix begins
        # with /admin. Only there do we hard-block a logged-in non-member.
        root = scope.get("root_path", "")
        remainder = (scope.get("path", "") or "")[len(root):] or "/"
        if remainder.startswith("/admin") and not roles:
            request.state.user = None

        return await call_next(request)


class QuotaMiddleware(BaseHTTPMiddleware):
    """Enforce per-store creation caps on the engine's auto-CRUD write path.

    On ``POST /{handle}/{store}/api/{items|sections}`` it counts the store's
    ``{scope}_{collection}`` and returns ``409`` when the (env-configured) cap
    is reached. Caps default to ``0`` (unlimited). AI-driven creates bypass this
    CRUD path, so ``/admin/ai/apply`` enforces the same caps itself.
    """

    async def dispatch(self, request: Request, call_next):
        scope = request.scope
        slug = scope.get("store_slug")
        if slug and request.method == "POST":
            root = scope.get("root_path", "")
            remainder = ((scope.get("path") or "")[len(root):] or "/").rstrip("/")
            collection, cap = "", 0
            if remainder == "/api/items":
                collection, cap = "items", MAX_ITEMS_PER_STORE
            elif remainder == "/api/sections":
                collection, cap = "sections", MAX_SECTIONS_PER_STORE
            if collection and cap:
                try:
                    count = await _store_doc_count(request.app.state.engine, slug, collection)
                except Exception:  # noqa: BLE001 — a counting error must not block writes
                    count = 0
                if count >= cap:
                    return JSONResponse(
                        {"detail": f"{collection} limit reached ({cap}) for this store"},
                        status_code=409,
                    )
        return await call_next(request)


# Append (not add_middleware) so the overlay is the INNERMOST middleware: it
# runs last on ingress, after the engine's AppUserSessionMiddleware has set
# request.state.user, while StoreScopeMiddleware (outermost) has already set
# scope["handle"]. QuotaMiddleware is even further in (closest to the route) so
# it sees the resolved scope. Rebuild the stack now that all are registered.
app.user_middleware.append(Middleware(StoreRoleOverlayMiddleware))
app.user_middleware.append(Middleware(QuotaMiddleware))
app.middleware_stack = app.build_middleware_stack()


# ── Per-request scope override ─────────────────────────────────────────
#
# Every engine data path (SSR pages, auto-CRUD /api/*, sitemap/feeds) and the
# custom endpoints below resolve data through this single dependency, so one
# override re-scopes the whole app. Store requests use their slug; everything
# else (the /manage console and platform pages) uses the platform scope.
async def _scoped_db_for_request(request: Request):
    engine = request.app.state.engine
    slug = request.scope.get("store_slug")
    if slug:
        # Store scopes never hold a secret → no token required.
        return await engine.get_scoped_db(slug)
    # Platform scope may require the primary slug's token (secrets manager on).
    return await _platform_db(engine)


app.dependency_overrides[get_scoped_db] = _scoped_db_for_request


# ── Additive seeding helpers (used when provisioning a store) ───────────
#
# These insert what's *missing* by a stable key and never overwrite
# admin-edited documents, so re-running provisioning is always safe.


async def _seed_singleton(db, collection_name: str, docs: list[dict[str, Any]]) -> None:
    """Insert seed docs only when the collection is completely empty."""
    if not docs:
        return
    collection = db[collection_name]
    if await collection.count_documents({}) > 0:
        return
    prepared = []
    for doc in docs:
        d = doc.copy()
        d.setdefault("created_at", datetime.now(timezone.utc))
        prepared.append(d)
    await collection.insert_many(prepared)
    logger.info(f"Seeded {len(prepared)} document(s) into '{collection_name}'")


async def _seed_by_key(db, collection_name: str, key_field: str, docs: list[dict[str, Any]]) -> None:
    """Insert any seed docs whose ``key_field`` is not already present."""
    if not docs:
        return
    collection = db[collection_name]
    existing = set()
    async for doc in collection.find({}, {key_field: 1}):
        val = doc.get(key_field)
        if val is not None:
            existing.add(val)

    new_docs = []
    for doc in docs:
        key = doc.get(key_field)
        if key is None or key in existing:
            continue
        d = doc.copy()
        d.setdefault("created_at", datetime.now(timezone.utc))
        new_docs.append(d)

    if new_docs:
        await collection.insert_many(new_docs)
        logger.info(f"Seeded {len(new_docs)} new '{collection_name}' row(s) (by {key_field})")


# ── Store provisioning ─────────────────────────────────────────────────


async def _ensure_store_indexes(engine, scope: str) -> None:
    """Create a store scope's managed indexes via the public engine API.

    Driven entirely by the manifest's ``managed_indexes`` (including the unique
    constraints on ``stores.slug_id``, ``sections.key`` and ``items.item_code``),
    applied per store scope with ``run_index_creation_for_collection`` against
    the raw database using scoped ``{scope}_{collection}`` names. No private
    engine internals are touched, so this stays stable across engine upgrades.
    The per-doc ``app_id`` index is auto-ensured by the engine on first access.
    """
    managed = _manifest_data.get("managed_indexes") or {}
    if not managed:
        return
    try:
        raw_db = engine.connection_manager.mongo_db
    except Exception as exc:  # noqa: BLE001 — no raw handle → skip (best-effort)
        logger.warning(f"[{scope}] index creation skipped (no db handle): {exc}")
        return
    for col_name, index_defs in managed.items():
        if not index_defs:
            continue
        try:
            await run_index_creation_for_collection(
                db=raw_db,
                slug=scope,
                collection_name=f"{scope}_{col_name}",
                index_definitions=index_defs,
            )
        except Exception as exc:  # noqa: BLE001 — one bad collection never blocks the rest
            logger.warning(f"[{scope}] index creation for '{col_name}' skipped: {exc}")


def _validate_slug(slug: str) -> str:
    """Normalise + validate a handle or store slug, or raise ``ValueError``.

    Used for both the namespace handle (first path segment) and the store slug
    (second segment). Because a validated slug never contains ``_``, the two
    compose unambiguously into the ``{handle}__{store}`` engine scope.
    """
    slug = (slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError(
            "Address must be 3–40 characters: lowercase letters, numbers and hyphens."
        )
    if slug in BANNED_SLUGS:
        raise ValueError(f"'{slug}' is reserved and can't be used as an address.")
    return slug


async def provision_store(
    engine,
    handle: str,
    store: str,
    name: str = "",
    owner_email: str | None = None,
    template: str = "",
) -> dict[str, str]:
    """Create a store scope end to end: register → indexes → seed → mark ready.

    A store lives under a namespace ``handle`` at ``/{handle}/{store}`` and maps
    to the engine scope ``{handle}__{store}``. ``template`` selects the starter
    content by business type (``retail`` default, or a key in
    ``_ALT_TEMPLATE_FILES`` such as ``restaurant``); it is persisted on the
    registry row so a reconcile-driven retry re-seeds from the same template.
    Idempotent and step-logged: the registry row is written **first** with
    ``status="provisioning"`` so the store is observable before its collections
    exist. If seeding fails mid-way the row stays ``provisioning`` — never
    leaving orphan ``{scope}_*`` collections unaccounted for — and the reconciler
    (or a plain re-run) can finish it safely, since seeding is additive by key
    and index creation is idempotent.
    """
    handle = _validate_slug(handle)
    store = _validate_slug(store)
    scope = scope_id(handle, store)
    name = (name or "").strip() or store
    template_key = (template or "").strip().lower()
    now = datetime.now(timezone.utc)

    reg = await _platform_db(engine)
    set_fields: dict[str, Any] = {
        "name": name,
        "status": STORE_STATUS_PROVISIONING,
        "scope": scope,
        "template": template_key,
        "updated_at": now,
    }
    if owner_email:
        set_fields["owner_email"] = owner_email.strip().lower()
    await reg["store_registry"].update_one(
        {"handle": handle, "store": store},
        {"$set": set_fields, "$setOnInsert": {"handle": handle, "store": store, "created_at": now}},
        upsert=True,
    )

    db = await engine.get_scoped_db(scope)
    await _ensure_store_indexes(engine, scope)

    content = _load_store_template(template_key)
    stores = content.get("stores") or [{}]
    stores[0]["name"] = name
    stores[0]["slug_id"] = store
    await _seed_singleton(db, "stores", stores)
    await _seed_by_key(db, "sections", "key", content.get("sections", []))
    await _seed_by_key(db, "items", "item_code", content.get("items", []))
    await _seed_singleton(db, "specials", content.get("specials", []))
    await _seed_singleton(db, "slideshow", content.get("slideshow", []))

    await reg["store_registry"].update_one(
        {"handle": handle, "store": store},
        {"$set": {"status": STORE_STATUS_READY, "updated_at": datetime.now(timezone.utc)}},
    )
    KNOWN_HANDLES.add(handle)
    KNOWN_STORES.add(scope)
    logger.info(f"Provisioned store '{scope}' ({name}) from '{template_key or 'retail'}' template")
    return {"handle": handle, "store": store, "scope": scope, "name": name}


# ── Registry indexes, deprovisioning & reconciliation ──────────────────


def _registry_collection(engine):
    """Raw Motor handle for the physical ``{PLATFORM_SLUG}_store_registry``."""
    return engine.connection_manager.mongo_db[f"{PLATFORM_SLUG}_store_registry"]


def _members_collection(engine):
    """Raw Motor handle for the physical ``{PLATFORM_SLUG}_namespace_members``."""
    return engine.connection_manager.mongo_db[f"{PLATFORM_SLUG}_namespace_members"]


async def _cleanup_namespace_if_empty(engine, handle: str) -> bool:
    """Drop a handle's memberships once it owns no stores at all.

    Memberships are namespace-level, so they survive deleting *a* store; they
    are only cleaned up when the handle's **last** store is removed. Also drops
    the handle from ``KNOWN_HANDLES``. Returns ``True`` when cleanup ran.
    """
    reg = await _platform_db(engine)
    remaining = await reg["store_registry"].find_one({"handle": handle}, {"_id": 1})
    if remaining is not None:
        return False
    KNOWN_HANDLES.discard(handle)
    with contextlib.suppress(Exception):
        await _members_collection(engine).delete_many({"handle": handle})
    return True


async def _ensure_registry_indexes(engine) -> None:
    """Best-effort indexes on the registry + namespace-members collections."""
    try:
        col = _registry_collection(engine)
    except Exception as exc:  # noqa: BLE001 — no raw handle → skip
        logger.warning(f"registry index creation skipped (no db handle): {exc}")
        return
    try:
        await col.create_index(
            [("handle", 1), ("store", 1)], unique=True, name="store_registry_handle_store_unique"
        )
        await col.create_index("scope", unique=True, sparse=True, name="store_registry_scope_unique")
        await col.create_index("handle", name="store_registry_handle")
        await col.create_index("status", name="store_registry_status")
    except Exception as exc:  # noqa: BLE001 — never block boot on index creation
        logger.warning(f"registry index creation skipped: {exc}")
    try:
        members = _members_collection(engine)
        await members.create_index(
            [("handle", 1), ("email", 1)], unique=True, name="ns_member_handle_email_unique"
        )
        await members.create_index("email", name="ns_member_email")
    except Exception as exc:  # noqa: BLE001 — never block boot on index creation
        logger.warning(f"namespace_members index creation skipped: {exc}")


async def _store_doc_count(engine, scope: str, collection: str) -> int:
    """Count docs in the physical ``{scope}_{collection}`` collection."""
    raw_db = engine.connection_manager.mongo_db
    return await raw_db[f"{scope}_{collection}"].count_documents({})


async def _drop_store_collections(engine, scope: str) -> list[str]:
    """Drop every physical ``{scope}_*`` collection. Returns dropped names.

    The trailing underscore in the prefix makes this exact: validated handles
    and stores never contain ``_``, so ``acme__shop_`` matches ``acme__shop_items``
    but never ``acme__shopping_items`` or the platform's ``{PLATFORM_SLUG}_*``
    collections.

    The ``{scope}_stores`` singleton is dropped **last** on purpose: it is the
    marker the orphan scan keys on, so if the process dies mid-drop the
    leftover collections remain detectable (and re-cleanable) as an orphan.
    """
    raw_db = engine.connection_manager.mongo_db
    prefix = f"{scope}_"
    stores_name = f"{scope}_stores"
    names = [n for n in await raw_db.list_collection_names() if n.startswith(prefix)]
    names.sort(key=lambda n: n == stores_name)  # False (0) before True (1) → stores last
    dropped: list[str] = []
    for full in names:
        try:
            await raw_db.drop_collection(full)
            dropped.append(full)
        except Exception as exc:  # noqa: BLE001 — one bad drop never blocks the rest
            logger.warning(f"Failed to drop collection '{full}': {exc}")
    return dropped


async def _audit_store_event(
    engine,
    event: str,
    handle: str,
    store: str | None = None,
    actor: dict[str, Any] | None = None,
    **extra: Any,
) -> None:
    """Best-effort platform-scope audit entry for a lifecycle/team action.

    Writes to the platform ``audit_log`` so there is an operational trail of
    who did what (created/renamed/archived/restored/deleted, team changes) and
    when. Never raises — an audit write must never fail the operation it records.
    """
    try:
        reg = await _platform_db(engine)
        doc: dict[str, Any] = {
            "event": event,
            "handle": handle,
            "store": store,
            "scope": scope_id(handle, store) if store else handle,
            "actor": (actor or {}).get("email") or (actor or {}).get("role") or "system",
            "timestamp": datetime.now(timezone.utc),
        }
        doc.update(extra)
        await reg["audit_log"].insert_one(doc)
    except Exception as exc:  # noqa: BLE001 — audit is best-effort
        logger.debug(f"audit '{event}' for '{handle}/{store}' skipped: {exc}")


async def _find_orphan_scopes(engine, reg) -> list[str]:
    """Store scopes that own a ``{scope}_stores`` collection but no registry row.

    Every real store owns a ``{scope}_stores`` singleton, so that collection is
    the reliable marker of a store scope. Only composite ``{handle}__{store}``
    scopes are considered, so platform/engine collections (``apps_config``,
    ``{PLATFORM_SLUG}_store_registry``, …) never match.
    """
    raw_db = engine.connection_manager.mongo_db
    suffix = "_stores"
    candidates: set[str] = set()
    for full in await raw_db.list_collection_names():
        if full.endswith(suffix):
            scope = full[: -len(suffix)]
            if scope and scope != PLATFORM_SLUG and split_scope(scope) is not None:
                candidates.add(scope)
    orphans: list[str] = []
    for scope in sorted(candidates):
        handle, store = split_scope(scope)  # type: ignore[misc]
        if await reg["store_registry"].find_one({"handle": handle, "store": store}, {"_id": 1}) is None:
            orphans.append(scope)
    return orphans


def _as_naive_utc(dt: Any) -> datetime | None:
    """Normalise a Mongo datetime to naive UTC for safe comparison.

    PyMongo may return naive or aware datetimes depending on client config;
    normalising both to naive UTC avoids ``offset-naive vs offset-aware``
    comparison errors.
    """
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


async def reconcile_stores(engine, drop_orphans: bool = False) -> dict[str, Any]:
    """Repair lifecycle drift and surface orphaned store collections.

    Three passes, all safe to run repeatedly and guarded by
    ``PROVISION_STUCK_MINUTES`` so an in-flight operation on another worker is
    never disturbed:

    * **Stuck provisioning** — rows left ``provisioning`` past the cutoff are
      retried once via ``provision_store`` (idempotent); a failed retry is
      marked ``failed`` so an operator can act.
    * **Stranded deleting** — rows left ``deleting`` past the cutoff (a delete
      that crashed mid-drop) have their ``{scope}_*`` collections re-dropped and
      the registry row removed, finishing the deprovision.
    * **Orphan collections** — physical ``{scope}_*`` collections whose scope has
      no registry row (a crash before the first registry write, or a delete
      that lost its row before dropping). Reported and returned; pass
      ``drop_orphans=True`` to drop them.

    ``retried``/``failed``/``deleted``/``orphans`` are reported as composite
    ``{handle}__{store}`` scope ids.
    """
    result: dict[str, Any] = {"retried": [], "failed": [], "deleted": [], "orphans": [], "dropped": []}
    reg = await _platform_db(engine)
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_naive - timedelta(minutes=PROVISION_STUCK_MINUTES)

    async for doc in reg["store_registry"].find({"status": STORE_STATUS_PROVISIONING}):
        handle, store = doc.get("handle"), doc.get("store")
        scope = scope_id(handle, store) if handle and store else None
        updated = _as_naive_utc(doc.get("updated_at") or doc.get("created_at"))
        if not scope or (updated is not None and updated > cutoff):
            continue
        try:
            await provision_store(
                engine, handle, store, doc.get("name") or store,
                doc.get("owner_email"), template=doc.get("template") or "",
            )
            result["retried"].append(scope)
            await _audit_store_event(engine, "store_provision_retried", handle, store)
        except Exception as exc:  # noqa: BLE001 — surface as failed, never crash the pass
            logger.warning(f"Reconcile: provisioning retry failed for '{scope}': {exc}")
            await reg["store_registry"].update_one(
                {"handle": handle, "store": store},
                {"$set": {"status": STORE_STATUS_FAILED, "updated_at": datetime.now(timezone.utc)}},
            )
            result["failed"].append(scope)
            await _audit_store_event(engine, "store_provision_failed", handle, store, error=str(exc))

    async for doc in reg["store_registry"].find({"status": STORE_STATUS_DELETING}):
        handle, store = doc.get("handle"), doc.get("store")
        scope = scope_id(handle, store) if handle and store else None
        updated = _as_naive_utc(doc.get("updated_at") or doc.get("created_at"))
        if not scope or (updated is not None and updated > cutoff):
            continue
        try:
            KNOWN_STORES.discard(scope)
            dropped = await _drop_store_collections(engine, scope)
            await reg["store_registry"].delete_one({"handle": handle, "store": store})
            await _cleanup_namespace_if_empty(engine, handle)
            result["dropped"].extend(dropped)
            result["deleted"].append(scope)
            await _audit_store_event(engine, "store_delete_recovered", handle, store, dropped=len(dropped))
        except Exception as exc:  # noqa: BLE001 — one bad recovery never blocks the pass
            logger.warning(f"Reconcile: delete recovery failed for '{scope}': {exc}")

    orphans = await _find_orphan_scopes(engine, reg)
    result["orphans"] = orphans
    if orphans:
        logger.warning(f"Reconcile: orphan store collections with no registry row: {orphans}")
        if drop_orphans:
            for scope in orphans:
                handle, store = split_scope(scope)  # type: ignore[misc]
                dropped = await _drop_store_collections(engine, scope)
                result["dropped"].extend(dropped)
                await _audit_store_event(engine, "store_orphan_dropped", handle, store, dropped=len(dropped))

    await refresh_known_stores()
    return result


# ── Cross-worker cache sync (change stream + TTL backstop) ─────────────


async def _watch_registry_changes(engine) -> None:
    """Refresh ``KNOWN_STORES`` on any ``store_registry`` change.

    Uses a MongoDB change stream on the physical registry collection so a
    store created/archived/deleted on one worker propagates to the others
    near-instantly. Change streams require a replica set / Atlas (dev uses
    Atlas Local, a single-node replica set); on standalone Mongo this logs
    once and exits, leaving the TTL backstop to keep workers converged.
    """
    col = _registry_collection(engine)
    backoff = 1.0
    resume_token: dict[str, Any] | None = None
    while True:
        try:
            kwargs: dict[str, Any] = {"full_document": "updateLookup"}
            if resume_token is not None:
                # Resume from just after the last handled event so changes that
                # landed during a transient drop are not missed.
                kwargs["resume_after"] = resume_token
            async with col.watch(**kwargs) as stream:
                logger.info("store_registry change-stream watcher started")
                backoff = 1.0
                async for _event in stream:
                    resume_token = stream.resume_token
                    await refresh_known_stores()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — degrade gracefully off a replica set
            msg = str(exc).lower()
            if "replica set" in msg or "only supported on replica sets" in msg:
                logger.warning(
                    "store_registry change streams need a replica set / Atlas — "
                    "cross-worker cache updates fall back to the %ss TTL refresh.",
                    STORE_CACHE_TTL_SECONDS,
                )
                return
            # A stale/invalid resume token can wedge every reconnect; drop it
            # and start a fresh stream (the TTL backstop covers the gap).
            if "resume" in msg and ("token" in msg or "point" in msg or "oplog" in msg):
                resume_token = None
            logger.warning(f"store_registry watcher lost ({exc}); retrying in {backoff:.0f}s")
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, 30.0)


async def _refresh_known_stores_loop() -> None:
    """Periodic backstop refresh of ``KNOWN_STORES`` (covers standalone Mongo)."""
    while True:
        try:
            await asyncio.sleep(STORE_CACHE_TTL_SECONDS)
        except asyncio.CancelledError:
            raise
        await refresh_known_stores()


async def _bootstrap_stores() -> None:
    """Prepare the registry and hydrate the routing caches (ready stores only).

    Ensures the registry + members indexes exist, seeds a ``demo/shop`` store
    (owned by the platform admin) on a wholly fresh platform (no registry rows
    at all), then rebuilds the caches from ``ready`` rows.
    """
    try:
        engine = app.state.engine
        await _ensure_registry_indexes(engine)
        reg = await _platform_db(engine)
        has_any = await reg["store_registry"].find_one({}, {"_id": 1}) is not None
        if not has_any:
            await provision_store(
                engine, "demo", "shop", "Demo Store", owner_email=os.getenv("ADMIN_EMAIL")
            )
        await refresh_known_stores()
        logger.info(f"Multi-tenant ready — stores: {sorted(KNOWN_STORES)}")
    except Exception as exc:  # noqa: BLE001 — startup best-effort
        logger.warning(f"Store bootstrap skipped: {exc}")


# ── Rate limiting + abuse protection ───────────────────────────────────
#
# Reuses the engine's own Mongo-backed sliding-window limiter (TTL cleanup,
# multi-worker safe) — no Redis, no slowapi, no new infrastructure. One shared
# store lives on ``app.state`` (built in the lifespan); limits are env-tunable
# and also on ``app.state`` so they can be adjusted at runtime and in tests.


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


# Cross-worker cache-sync + reconciler tuning (env-overridable).
STORE_CACHE_TTL_SECONDS = _int_env("STORE_CACHE_TTL_SECONDS", 30)
PROVISION_STUCK_MINUTES = _int_env("PROVISION_STUCK_MINUTES", 10)


def _build_rate_limits() -> dict[str, list[RateLimit]]:
    """Per-route throttles. Inquiry is per-IP+store; AI/uploads are per-user."""
    return {
        "inquiry": [
            RateLimit(max_attempts=_int_env("INQUIRY_RATELIMIT_PER_MIN", 5), window_seconds=60),
            RateLimit(max_attempts=_int_env("INQUIRY_RATELIMIT_PER_HOUR", 30), window_seconds=3600),
        ],
        "ai": [
            RateLimit(max_attempts=_int_env("AI_RATELIMIT_PER_MIN", 20), window_seconds=60),
        ],
        "upload": [
            RateLimit(max_attempts=_int_env("UPLOAD_RATELIMIT_PER_MIN", 30), window_seconds=60),
        ],
        "signup": [
            RateLimit(max_attempts=_int_env("SIGNUP_RATELIMIT_PER_MIN", 5), window_seconds=60),
            RateLimit(max_attempts=_int_env("SIGNUP_RATELIMIT_PER_HOUR", 20), window_seconds=3600),
        ],
    }


def _client_ip(request: Request) -> str:
    """Best-effort client IP. Only trust proxy headers behind a known proxy."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    if request.client:
        return request.client.host
    return "unknown"


def _rate_limit(name: str, per: str = "ip"):
    """FastAPI dependency factory enforcing the ``name`` throttle.

    ``per="ip"`` keys on (store slug + client IP) so one store's abusers never
    throttle another's; ``per="user"`` keys on the signed-in admin. Returns a
    ``429`` with ``Retry-After`` once any configured window is exceeded.
    """

    async def _dep(request: Request) -> None:
        store = getattr(request.app.state, "rate_store", None)
        limits = (getattr(request.app.state, "rate_limits", {}) or {}).get(name)
        if store is None or not limits:
            return
        if per == "user":
            user = await get_current_user(request)
            uid = str((user or {}).get("id") or (user or {}).get("_id") or (user or {}).get("email") or "anon")
            base = f"user:{uid}"
        else:
            slug = request.scope.get("store_slug") or "-"
            base = f"{slug}:{_client_ip(request)}"
        for limit in limits:
            identifier = f"rl:{name}:{limit.window_seconds}:{base}"
            count = await store.record_attempt(identifier, limit.window_seconds)
            if count > limit.max_attempts:
                raise HTTPException(
                    status_code=429,
                    detail="Too many requests — please slow down and try again shortly.",
                    headers={"Retry-After": str(limit.window_seconds)},
                )

    return _dep


# ``quickstart`` builds the app with a custom ``lifespan``, so Starlette ignores
# ``@app.on_event("startup")``. Wrap the engine's lifespan instead: run it (which
# connects the engine and sets ``app.state.engine``), then set up the shared
# rate-limit store and bootstrap the stores.
_engine_lifespan = app.router.lifespan_context


@contextlib.asynccontextmanager
async def _lifespan_with_bootstrap(app_):
    async with _engine_lifespan(app_):
        try:
            engine = app_.state.engine
            app_.state.rate_store = create_rate_limit_store(db=engine.connection_manager.mongo_db)
            app_.state.rate_limits = _build_rate_limits()
        except Exception as exc:  # noqa: BLE001 — throttling is best-effort, never blocks boot
            logger.warning(f"Rate limiter setup skipped: {exc}")
            app_.state.rate_store = None
            app_.state.rate_limits = {}

        # Surface the AI editor's configuration once, clearly, at boot so an
        # operator immediately knows whether the conversational editor is live.
        ai_editor.log_startup_config()

        await _bootstrap_stores()

        # Best-effort reconcile on boot: finish provisions/deletes that a prior
        # crash left stranded (guarded by PROVISION_STUCK_MINUTES, so in-flight
        # work on a peer worker is never touched). Never blocks or fails boot.
        try:
            summary = await reconcile_stores(app_.state.engine)
            if any(summary.get(k) for k in ("retried", "failed", "deleted", "orphans")):
                logger.info(f"Startup reconcile: {summary}")
        except Exception as exc:  # noqa: BLE001 — reconcile is best-effort
            logger.warning(f"Startup reconcile skipped: {exc}")

        # Background cache-sync: a registry change-stream watcher (instant
        # cross-worker updates on a replica set) plus a TTL backstop that
        # covers standalone Mongo and any dropped stream.
        cache_tasks: list[asyncio.Task] = []
        try:
            engine = app_.state.engine
            cache_tasks.append(
                asyncio.create_task(_watch_registry_changes(engine), name="store-registry-watcher")
            )
            cache_tasks.append(
                asyncio.create_task(_refresh_known_stores_loop(), name="store-cache-ttl")
            )
            app_.state.store_cache_tasks = cache_tasks
        except Exception as exc:  # noqa: BLE001 — cache sync is best-effort, never blocks boot
            logger.warning(f"Store cache-sync tasks skipped: {exc}")

        try:
            yield
        finally:
            for task in cache_tasks:
                task.cancel()
            for task in cache_tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task


app.router.lifespan_context = _lifespan_with_bootstrap


# ── Custom endpoints ────────────────────────────────────────────────────


async def _read_request_body(request: Request) -> dict[str, Any]:
    """Parse a JSON body, falling back to form-urlencoded / multipart."""
    try:
        parsed = await request.json()
    except Exception:
        try:
            form = await request.form()
            parsed = dict(form)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid body") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="invalid body")
    return parsed


@app.post("/api/submit-inquiry")
async def submit_inquiry(
    request: Request,
    background: BackgroundTasks,
    db=Depends(get_scoped_db),
    _rl: None = Depends(_rate_limit("inquiry", per="ip")),
) -> JSONResponse:
    """Public endpoint for visitor lead/inquiry submissions.

    The ``inquiries`` collection auto-CRUD is locked to the ``admin`` role;
    this is the only public write path. It is per-IP+store rate limited, drops
    honeypot-tripped bot submissions silently, validates and normalises inputs
    before inserting directly via the scoped DB, then fires a best-effort lead
    notification without blocking the response.
    """
    body = await _read_request_body(request)

    # Honeypot: a hidden field real users never see. Bots that autofill it get
    # a convincing 201 while we drop the submission — no insert, no notify.
    if str(body.get("company_website") or "").strip():
        logger.info("Honeypot tripped on submit-inquiry — dropping silently")
        base_path = request.scope.get("root_path", "")
        return JSONResponse(
            {"ok": True, "id": None, "redirect": f"{base_path}/contact/thanks"}, status_code=201
        )

    customer_name = str(body.get("customer_name") or "").strip()
    customer_contact = str(body.get("customer_contact") or "").strip()
    if not customer_name or not customer_contact:
        raise HTTPException(status_code=422, detail="customer_name and customer_contact are required")

    item_id = (str(body.get("item_id") or "").strip() or "general")[:200]
    item_name = str(body.get("item_name") or "General inquiry").strip()[:200]
    service_category = str(body.get("service_category") or "").strip()
    issue = str(body.get("issue") or "").strip()
    message = str(body.get("message") or "").strip()
    preferred_date = str(body.get("preferred_date") or "").strip()

    if len(customer_name) > 200 or len(customer_contact) > 200 or len(message) > 5000:
        raise HTTPException(status_code=422, detail="field too long")
    if len(service_category) > 64 or len(issue) > 200 or len(preferred_date) > 32:
        raise HTTPException(status_code=422, detail="field too long")

    doc = {
        "type": "inquiry",
        "item_id": item_id,
        "item_name": item_name,
        "service_category": service_category[:64],
        "issue": issue[:200],
        "customer_name": customer_name,
        "customer_contact": customer_contact,
        "message": message,
        "preferred_date": preferred_date[:32],
        "date_submitted": datetime.now(timezone.utc).isoformat(),
        "status": "New",
        "read": False,
        "archived": False,
        "notes": "",
    }
    result = await db["inquiries"].insert_one(doc)

    # Best-effort lead notification — scheduled after the response so it never
    # blocks or fails the visitor's 201 (see notifications.notify_new_inquiry).
    try:
        store_doc = await db["stores"].find_one({}) or {}
        background.add_task(notifications.notify_new_inquiry, store_doc, doc)
    except Exception as exc:  # noqa: BLE001 — notification is never critical
        logger.warning(f"Lead notification scheduling skipped: {exc}")

    base_path = request.scope.get("root_path", "")
    return JSONResponse(
        {"ok": True, "id": str(result.inserted_id), "redirect": f"{base_path}/contact/thanks"},
        status_code=201,
    )


async def _check_upload_quota(request: Request) -> tuple[str, str]:
    """Enforce ``MAX_UPLOADS_PER_STORE`` before an upload. Returns ``(handle, store)``.

    The persisted per-store ``upload_count`` on the registry row is the source
    of truth (Cloudinary assets aren't enumerated per store), incremented on
    each successful upload by ``_increment_upload_count``.
    """
    handle = request.scope.get("handle") or ""
    store = request.scope.get("store") or ""
    if MAX_UPLOADS_PER_STORE and handle and store:
        reg = await _platform_db(request.app.state.engine)
        row = await reg["store_registry"].find_one(
            {"handle": handle, "store": store}, {"upload_count": 1}
        )
        if row and int(row.get("upload_count") or 0) >= MAX_UPLOADS_PER_STORE:
            raise HTTPException(
                status_code=409,
                detail=f"upload limit reached ({MAX_UPLOADS_PER_STORE}) for this store",
            )
    return handle, store


async def _increment_upload_count(request: Request, handle: str, store: str) -> None:
    """Best-effort ``$inc`` of the store's persisted upload counter."""
    if not (handle and store):
        return
    with contextlib.suppress(Exception):
        reg = await _platform_db(request.app.state.engine)
        await reg["store_registry"].update_one(
            {"handle": handle, "store": store}, {"$inc": {"upload_count": 1}}
        )


@app.post("/admin/upload-image")
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
    folder: str = Form("store_images"),
    _rl: None = Depends(_rate_limit("upload", per="user")),
) -> JSONResponse:
    """Admin-only Cloudinary image upload.

    Kept as a custom route because ``mdb-engine`` does not ship a managed
    uploader. Gated to owner/editor of the store's namespace (or the platform
    superuser), consistent with every other store-write surface.
    """
    await _require_store_write(request)
    handle, store = await _check_upload_quota(request)

    if not (_cloud_name and _cloud_key and _cloud_secret):
        raise HTTPException(status_code=503, detail="Cloudinary not configured")

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="must be an image")

    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="max 10MB")

    result = cloudinary.uploader.upload(
        data,
        folder=folder,
        transformation=[{"quality": "auto", "fetch_format": "auto"}],
    )
    await _increment_upload_count(request, handle, store)
    return JSONResponse(
        {
            "ok": True,
            "image_url": result["secure_url"],
            "public_id": result["public_id"],
        }
    )


MAX_VIDEO_UPLOAD_BYTES = 75 * 1024 * 1024

# Delivery transform applied to every uploaded slideshow video. Cloudinary
# transcodes on first request and CDNs subsequent hits; ``eager_async`` below
# also pre-warms the cache so the admin sees fast playback right away.
#   * quality auto:good — adaptive quality, visually clean.
#   * video_codec h264 — broadest browser compatibility.
#   * width 1920 + crop limit — never upscale, cap at 1080p.
#   * audio_codec none — hero plays muted; strip audio for bandwidth savings.
#   * bit_rate 2500k — hard ceiling so a chaotic source can't blow out size.
#   * fps 30 — cap framerate for 60fps phone clips.
_VIDEO_DELIVERY_TRANSFORM: dict[str, Any] = {
    "quality": "auto:good",
    "video_codec": "h264",
    "width": 1920,
    "crop": "limit",
    "audio_codec": "none",
    "bit_rate": "2500k",
    "fps": 30,
}

# Poster image transform: pick an "interesting" frame and serve it at a
# reasonable size with auto quality.
_VIDEO_POSTER_TRANSFORM: dict[str, Any] = {
    "start_offset": "auto",
    "width": 1280,
    "crop": "limit",
    "quality": "auto",
}


@app.post("/admin/upload-video")
async def upload_video(
    request: Request,
    file: UploadFile = File(...),
    folder: str = Form("slideshow_videos"),
    _rl: None = Depends(_rate_limit("upload", per="user")),
) -> JSONResponse:
    """Owner/editor Cloudinary video upload with intelligent compression."""
    await _require_store_write(request)
    handle, store = await _check_upload_quota(request)

    if not (_cloud_name and _cloud_key and _cloud_secret):
        raise HTTPException(status_code=503, detail="Cloudinary not configured")

    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="must be a video")

    data = await file.read()
    original_bytes = len(data)
    if original_bytes > MAX_VIDEO_UPLOAD_BYTES:
        mb = MAX_VIDEO_UPLOAD_BYTES // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"max {mb}MB")

    result = cloudinary.uploader.upload(
        data,
        folder=folder,
        resource_type="video",
        eager=[{**_VIDEO_DELIVERY_TRANSFORM, "format": "mp4"}],
        eager_async=True,
    )

    public_id = result["public_id"]
    version = result.get("version")

    video_url, _ = cloudinary_url(
        public_id,
        resource_type="video",
        format="mp4",
        transformation=[_VIDEO_DELIVERY_TRANSFORM],
        secure=True,
        version=version,
    )
    thumb_url, _ = cloudinary_url(
        public_id,
        resource_type="video",
        format="jpg",
        transformation=[_VIDEO_POSTER_TRANSFORM],
        secure=True,
        version=version,
    )

    await _increment_upload_count(request, handle, store)
    return JSONResponse(
        {
            "ok": True,
            "video_url": video_url,
            "thumbnail_url": thumb_url,
            "public_id": public_id,
            "duration": result.get("duration"),
            "original_bytes": original_bytes,
            "original_url": result.get("secure_url"),
            "max_bytes": MAX_VIDEO_UPLOAD_BYTES,
        }
    )


# ── Conversational store editor (Google Gemini, JSON response mode) ──────
#
# Two admin-only endpoints power the chat widget. They mirror the safety
# ethos of the rest of the project: Gemini only *proposes* structured ops
# (constrained by a JSON responseSchema), the backend validates them against
# the manifest, and nothing is written until the admin confirms via
# /admin/ai/apply.


async def _require_platform_admin(request: Request) -> dict[str, Any]:
    """Gate platform-wide operations (``/manage`` lifecycle, reconcile).

    Only the seeded global admin (platform superuser) qualifies — these actions
    span every namespace, so per-namespace membership does not grant them.
    """
    user = await get_current_user(request)
    if not rbac.is_platform_superuser(user):
        raise HTTPException(status_code=401, detail="platform admin required")
    return user


async def _require_store_write(request: Request) -> dict[str, Any]:
    """Gate store-content writes (AI editor, uploads) to owner/editor/superuser.

    Relies on ``StoreRoleOverlayMiddleware`` having rewritten
    ``request.state.user_roles`` from the caller's membership of the request's
    namespace. ``viewer`` (read-only) and non-members are rejected.
    """
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="authentication required")
    if not rbac.grants_admin(await get_user_roles(request)):
        raise HTTPException(status_code=403, detail="you don't have edit access to this store")
    return user


async def _require_namespace_owner(request: Request) -> tuple[dict[str, Any], str]:
    """Gate namespace-level ops (team, store create/delete) to an owner/superuser.

    Returns ``(user, handle)`` where ``handle`` is the request's namespace.
    Raises 400 if the request is not namespace-scoped.
    """
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="authentication required")
    handle = request.scope.get("handle")
    if not handle:
        raise HTTPException(status_code=400, detail="not a namespace request")
    if rbac.is_platform_superuser(user):
        return user, handle
    pdb = await _platform_db(request.app.state.engine)
    role = await rbac.get_namespace_role(pdb["namespace_members"], handle, user.get("email"))
    if role not in rbac.OWNER_ROLES:
        raise HTTPException(status_code=403, detail="only the namespace owner can do that")
    return user, handle


def _clean_messages(raw: Any) -> list[dict[str, str]]:
    """Keep only well-formed user/assistant turns; cap history length."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for m in raw[-12:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            out.append({"role": role, "content": content.strip()[:4000]})
    return out


@app.post("/admin/ai/chat")
async def ai_chat(
    request: Request,
    db=Depends(get_scoped_db),
    _rl: None = Depends(_rate_limit("ai", per="user")),
) -> JSONResponse:
    """Turn a natural-language request into validated, proposed ops.

    Returns ``{reply, ops, diff, warnings}``. ``ops`` are the raw, validated
    ops to echo back to /admin/ai/apply; ``diff`` is the human-readable
    change list for the confirm card. No writes happen here.
    """
    await _require_store_write(request)
    body = await _read_request_body(request)
    messages = _clean_messages(body.get("messages"))
    if not messages:
        raise HTTPException(status_code=422, detail="messages required")

    try:
        snapshot = await ai_editor.build_snapshot(db)
        result = await ai_editor.propose(messages, snapshot, _manifest_data)
    except ai_editor.AIEditorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if "ops" not in result:
        return JSONResponse({"reply": result.get("reply", ""), "ops": [], "diff": []})

    normalized, diff, errors, valid_raw = ai_editor.validate_ops(
        result["ops"], snapshot, _manifest_data
    )
    if not normalized:
        reply = " ".join(errors) or "I couldn't turn that into a valid change. Could you rephrase?"
        return JSONResponse({"reply": reply, "ops": [], "diff": []})

    reply = "Here's the change I'll make — review and Apply when ready."
    return JSONResponse({"reply": reply, "ops": valid_raw, "diff": diff, "warnings": errors})


@app.post("/admin/ai/apply")
async def ai_apply(
    request: Request,
    db=Depends(get_scoped_db),
    _rl: None = Depends(_rate_limit("ai", per="user")),
) -> JSONResponse:
    """Re-validate the confirmed ops against a fresh snapshot, then apply."""
    user = await _require_store_write(request)
    body = await _read_request_body(request)
    ops = body.get("ops")
    if not isinstance(ops, list) or not ops:
        raise HTTPException(status_code=422, detail="ops required")

    snapshot = await ai_editor.build_snapshot(db)
    normalized, diff, errors, _ = ai_editor.validate_ops(ops, snapshot, _manifest_data)
    if not normalized:
        raise HTTPException(
            status_code=422,
            detail="; ".join(errors) or "no valid operations to apply",
        )

    # AI creates bypass the CRUD path QuotaMiddleware guards, so enforce the
    # same per-store caps here against a fresh count (batch size included).
    for tool, (coll, cap) in {
        "create_item": ("items", MAX_ITEMS_PER_STORE),
        "add_section": ("sections", MAX_SECTIONS_PER_STORE),
    }.items():
        if not cap:
            continue
        adding = sum(1 for op in normalized if op.get("tool") == tool)
        if adding and await db[coll].count_documents({}) + adding > cap:
            raise HTTPException(
                status_code=409, detail=f"{coll} limit reached ({cap}) for this store"
            )

    results = await ai_editor.apply_ops(db, normalized, user)
    ok = all(r.get("ok") for r in results)
    return JSONResponse(
        {"ok": ok, "results": results, "diff": diff, "warnings": errors},
        status_code=200 if ok else 207,
    )


# ── Namespaces, team invites, and the /manage console ──────────────────
#
# A user owns a **handle** (their namespace) and every store under it at
# /{handle}/{store}. The platform superuser (seeded ADMIN_EMAIL) sees every
# namespace. Team roles (owner/editor/viewer) live per handle in
# namespace_members and are enforced by StoreRoleOverlayMiddleware.

INVITE_TTL_SECONDS = int(os.getenv("INVITE_TTL_SECONDS", str(7 * 24 * 3600)))
# Optional guards on self-serve abuse (0 = unlimited for each).
MAX_STORES_PER_HANDLE = int(os.getenv("MAX_STORES_PER_HANDLE", "0"))
MAX_ITEMS_PER_STORE = int(os.getenv("MAX_ITEMS_PER_STORE", "0"))
MAX_SECTIONS_PER_STORE = int(os.getenv("MAX_SECTIONS_PER_STORE", "0"))
MAX_UPLOADS_PER_STORE = int(os.getenv("MAX_UPLOADS_PER_STORE", "0"))


def _invite_secret() -> str:
    """Signing secret for namespace invite tokens (reuses the engine JWT secret)."""
    return get_jwt_secret() or os.getenv("MDB_JWT_SECRET") or "ai-stores-dev-invite-secret"


def _store_url(handle: str, store: str) -> str:
    return f"/{handle}/{store}/"


def _card_from_row(doc: dict[str, Any]) -> dict[str, Any]:
    handle = doc.get("handle", "")
    store = doc.get("store", "")
    return {
        "handle": handle,
        "store": store,
        "name": doc.get("name") or store,
        "status": doc.get("status") or STORE_STATUS_READY,
        "url": _store_url(handle, store),
        "admin_url": f"/{handle}/{store}/admin/dashboard",
        "team_url": f"/{handle}/{store}/admin/team",
    }


async def _store_usage(engine, doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-store usage badges (count + optional cap) for the console.

    Cheap single-collection counts for items/sections plus the persisted
    upload counter. ``cap`` of ``0`` means unlimited (rendered as ``X`` only).
    """
    scope = scope_id(doc.get("handle", ""), doc.get("store", ""))
    usage: list[dict[str, Any]] = []
    for label, collection, cap in (
        ("items", "items", MAX_ITEMS_PER_STORE),
        ("sections", "sections", MAX_SECTIONS_PER_STORE),
    ):
        try:
            count = await _store_doc_count(engine, scope, collection)
        except Exception:  # noqa: BLE001 — a badge must never break the console
            count = 0
        usage.append({"label": label, "count": count, "cap": cap})
    usage.append(
        {"label": "uploads", "count": int(doc.get("upload_count") or 0), "cap": MAX_UPLOADS_PER_STORE}
    )
    return usage


async def _namespaces_for_user(engine, user: dict[str, Any] | None) -> tuple[list[dict[str, Any]], bool]:
    """Build the console's namespace list for ``user``.

    Returns ``(namespaces, can_create)``. The superuser sees every handle;
    everyone else sees only handles they belong to. ``role`` is the caller's
    namespace role (``"superuser"`` for the platform admin), and ``can_create``
    is set for handles the caller owns (or any, for the superuser).
    """
    if not user:
        return [], False
    reg = await _platform_db(engine)
    superuser = rbac.is_platform_superuser(user)

    # handle -> role for this user
    roles: dict[str, str] = {}
    if superuser:
        async for doc in reg["store_registry"].find({}, {"handle": 1}):
            if doc.get("handle"):
                roles.setdefault(doc["handle"], "superuser")
    else:
        members = _members_collection(engine)
        async for m in members.find({"email": rbac.normalize_email(user.get("email"))}):
            if m.get("handle") and m.get("role") in rbac.NAMESPACE_ROLES:
                roles[m["handle"]] = m["role"]

    namespaces: list[dict[str, Any]] = []
    for handle in sorted(roles):
        role = roles[handle]
        is_owner = superuser or role == rbac.ROLE_OWNER
        stores: list[dict[str, Any]] = []
        async for doc in reg["store_registry"].find({"handle": handle}).sort("created_at", 1):
            card = _card_from_row(doc)
            card["usage"] = await _store_usage(engine, doc)
            stores.append(card)
        namespaces.append(
            {"handle": handle, "role": role, "is_owner": is_owner, "stores": stores}
        )
    can_create = superuser or any(ns["is_owner"] for ns in namespaces)
    return namespaces, can_create


@app.get("/manage", response_class=HTMLResponse)
async def manage_home(request: Request):
    """Role-aware console: namespaces + stores you can manage (login otherwise)."""
    user = await get_current_user(request)
    is_admin = bool(user)
    superuser = rbac.is_platform_superuser(user)
    namespaces, can_create = await _namespaces_for_user(app.state.engine, user)
    owned_handles = [ns["handle"] for ns in namespaces if ns["is_owner"]]
    return _templates.TemplateResponse(
        request,
        "manage.html",
        {
            "user": user,
            "is_admin": is_admin,
            "is_superuser": superuser,
            "namespaces": namespaces,
            "can_create": can_create,
            "owned_handles": owned_handles,
            "store_templates": STORE_TEMPLATES,
            "store": {},
        },
    )


@app.get("/manage/ns/{handle}", response_class=HTMLResponse)
async def namespace_landing(handle: str, request: Request):
    """Public ``/{handle}/`` landing — lists a handle's published stores.

    Rendered via this reserved platform route (the store-scope middleware
    rewrites ``/{handle}/`` here). Always public; shows a "manage" CTA when the
    viewer owns the handle.
    """
    engine = app.state.engine
    reg = await _platform_db(engine)
    handle = (handle or "").strip().lower()
    stores: list[dict[str, str]] = []
    async for doc in reg["store_registry"].find(
        {"handle": handle, **_routable_status_query()}
    ).sort("created_at", 1):
        stores.append(_card_from_row(doc))
    if not stores and not await _handle_is_registered(handle):
        return HTMLResponse(_HANDLE_404_HTML, status_code=404)

    user = await get_current_user(request)
    can_manage = rbac.is_platform_superuser(user)
    if user and not can_manage:
        role = await rbac.get_namespace_role(
            reg["namespace_members"], handle, user.get("email")
        )
        can_manage = role in rbac.OWNER_ROLES
    return _templates.TemplateResponse(
        request,
        "namespace_landing.html",
        {"handle": handle, "stores": stores, "can_manage": can_manage, "store": {}},
    )


async def _assert_namespace_owner(request: Request, handle: str) -> dict[str, Any]:
    """Return the caller if they own ``handle`` (or are the superuser); else 401/403."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="authentication required")
    if rbac.is_platform_superuser(user):
        return user
    pdb = await _platform_db(request.app.state.engine)
    role = await rbac.get_namespace_role(pdb["namespace_members"], handle, user.get("email"))
    if role not in rbac.OWNER_ROLES:
        raise HTTPException(status_code=403, detail="only the namespace owner can do that")
    return user


@app.post("/manage/stores")
async def manage_create_store(request: Request) -> JSONResponse:
    """Provision a store under a handle (owner of that handle, or superuser).

    Body: ``{handle, slug|store, name, business_type}``. Owners may only create
    under a handle they already own; the superuser may create under any handle.
    ``business_type`` selects the starter template (retail default).
    """
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="authentication required")
    body = await _read_request_body(request)
    try:
        handle = _validate_slug(str(body.get("handle") or ""))
        store = _validate_slug(str(body.get("slug") or body.get("store") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await _assert_namespace_owner(request, handle)

    engine = app.state.engine
    reg = await _platform_db(engine)
    if await reg["store_registry"].find_one({"handle": handle, "store": store}, {"_id": 1}):
        raise HTTPException(status_code=409, detail=f"/{handle}/{store} already exists")
    if MAX_STORES_PER_HANDLE and not rbac.is_platform_superuser(user):
        count = await reg["store_registry"].count_documents({"handle": handle})
        if count >= MAX_STORES_PER_HANDLE:
            raise HTTPException(status_code=409, detail="store limit reached for this namespace")

    owner_email = rbac.normalize_email(user.get("email"))
    try:
        result = await provision_store(
            engine, handle, store, str(body.get("name") or ""), owner_email,
            template=str(body.get("business_type") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Guarantee the handle always has an owner. For a superuser-created store in
    # a brand-new handle, seed the acting/target owner so it is manageable.
    if not await rbac.count_owners(reg["namespace_members"], handle):
        await rbac.add_member(reg["namespace_members"], handle, owner_email, rbac.ROLE_OWNER)

    await _audit_store_event(engine, "store_created", handle, store, user, name=result["name"])
    return JSONResponse(
        {"ok": True, "handle": handle, "store": store, "name": result["name"], "url": result and _store_url(handle, store)},
        status_code=201,
    )


async def _free_store_slug(reg, handle: str, base: str = "store") -> str:
    """Return a store slug under ``handle`` that isn't taken (``base``, ``base-2``, …)."""
    taken: set[str] = set()
    async for doc in reg["store_registry"].find({"handle": handle}, {"store": 1}):
        if doc.get("store"):
            taken.add(doc["store"])
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


@app.post("/manage/stores/quick")
async def manage_quick_store(request: Request) -> JSONResponse:
    """One-click "hello world" store — auto-named, auto-addressed, ready to edit.

    Body: ``{handle?}``. Owners get a fresh store under a handle they own (their
    only handle if unspecified); the superuser must name a ``handle``. The slug
    auto-increments (``store``, ``store-2``, …) so it never collides, and the
    caller can rename/customise everything from the admin right away.
    """
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="authentication required")
    engine = app.state.engine
    reg = await _platform_db(engine)

    body = await _read_request_body(request)
    raw_handle = str(body.get("handle") or "").strip().lower()
    if not raw_handle:
        # No handle given: fall back to the caller's sole owned namespace.
        namespaces, _ = await _namespaces_for_user(engine, user)
        owned = [ns["handle"] for ns in namespaces if ns["is_owner"]]
        if len(owned) == 1:
            raw_handle = owned[0]
        elif not owned:
            raise HTTPException(status_code=422, detail="claim a handle first at /signup")
        else:
            raise HTTPException(status_code=422, detail="choose which namespace to create under")
    try:
        handle = _validate_slug(raw_handle)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await _assert_namespace_owner(request, handle)

    if MAX_STORES_PER_HANDLE and not rbac.is_platform_superuser(user):
        count = await reg["store_registry"].count_documents({"handle": handle})
        if count >= MAX_STORES_PER_HANDLE:
            raise HTTPException(status_code=409, detail="store limit reached for this namespace")

    store = await _free_store_slug(reg, handle)
    owner_email = rbac.normalize_email(user.get("email"))
    result = await provision_store(engine, handle, store, "My Store", owner_email, template="retail")

    if not await rbac.count_owners(reg["namespace_members"], handle):
        await rbac.add_member(reg["namespace_members"], handle, owner_email, rbac.ROLE_OWNER)

    await _audit_store_event(engine, "store_created", handle, store, user, name=result["name"])
    return JSONResponse(
        {
            "ok": True,
            "handle": handle,
            "store": store,
            "name": result["name"],
            "url": _store_url(handle, store),
            "admin_url": f"/{handle}/{store}/admin/dashboard",
        },
        status_code=201,
    )


async def _require_store_row(engine, handle: str, store: str) -> dict[str, Any]:
    """Fetch a registry row for a lifecycle op, or raise 404."""
    reg = await _platform_db(engine)
    doc = await reg["store_registry"].find_one({"handle": handle, "store": store})
    if not doc:
        raise HTTPException(status_code=404, detail="store not found")
    return doc


@app.patch("/manage/stores/{handle}/{store}")
async def manage_rename_store(handle: str, store: str, request: Request) -> JSONResponse:
    """Rename a store's display name (namespace owner or superuser).

    The address is immutable in this version: changing it would break bookmarks
    and canonical URLs and require a full collection rename plus an ``app_id``
    rewrite. Only the human-readable ``name`` changes here.
    """
    engine = app.state.engine
    await _require_store_row(engine, handle, store)
    user = await _assert_namespace_owner(request, handle)
    body = await _read_request_body(request)
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    now = datetime.now(timezone.utc)
    reg = await _platform_db(engine)
    await reg["store_registry"].update_one(
        {"handle": handle, "store": store}, {"$set": {"name": name, "updated_at": now}}
    )
    # Keep the store's own singleton in sync so the storefront shows the new name.
    store_db = await engine.get_scoped_db(scope_id(handle, store))
    await store_db["stores"].update_one({}, {"$set": {"name": name}})
    await _audit_store_event(engine, "store_renamed", handle, store, user, name=name)
    logger.info(f"Renamed store '{scope_id(handle, store)}' -> {name!r}")
    return JSONResponse({"ok": True, "handle": handle, "store": store, "name": name})


@app.post("/manage/stores/{handle}/{store}/archive")
async def manage_archive_store(handle: str, store: str, request: Request) -> JSONResponse:
    """Suspend a store: it stops routing (404s) but its data is kept."""
    engine = app.state.engine
    if handle == PLATFORM_SLUG:
        raise HTTPException(status_code=400, detail="cannot archive the platform")
    await _require_store_row(engine, handle, store)
    user = await _assert_namespace_owner(request, handle)
    reg = await _platform_db(engine)
    await reg["store_registry"].update_one(
        {"handle": handle, "store": store},
        {"$set": {"status": STORE_STATUS_ARCHIVED, "updated_at": datetime.now(timezone.utc)}},
    )
    await refresh_known_stores()
    await _audit_store_event(engine, "store_archived", handle, store, user)
    logger.info(f"Archived store '{scope_id(handle, store)}'")
    return JSONResponse({"ok": True, "handle": handle, "store": store, "status": STORE_STATUS_ARCHIVED})


@app.post("/manage/stores/{handle}/{store}/restore")
async def manage_restore_store(handle: str, store: str, request: Request) -> JSONResponse:
    """Re-enable an archived (or failed) store."""
    engine = app.state.engine
    await _require_store_row(engine, handle, store)
    user = await _assert_namespace_owner(request, handle)
    reg = await _platform_db(engine)
    await reg["store_registry"].update_one(
        {"handle": handle, "store": store},
        {"$set": {"status": STORE_STATUS_READY, "updated_at": datetime.now(timezone.utc)}},
    )
    await refresh_known_stores()
    await _audit_store_event(engine, "store_restored", handle, store, user)
    logger.info(f"Restored store '{scope_id(handle, store)}'")
    return JSONResponse({"ok": True, "handle": handle, "store": store, "status": STORE_STATUS_READY})


@app.delete("/manage/stores/{handle}/{store}")
async def manage_delete_store(handle: str, store: str, request: Request) -> JSONResponse:
    """Permanently deprovision a store (namespace owner or superuser).

    Drops every ``{handle}__{store}_*`` collection and removes the registry
    row; if it was the handle's last store, the handle's memberships are
    cleaned up too. Irreversible — use archive for a reversible suspend.
    Requires ``{"confirm": "<store>"}`` in the body to guard against accidents.
    """
    engine = app.state.engine
    if handle == PLATFORM_SLUG:
        raise HTTPException(status_code=400, detail="cannot delete the platform")
    await _require_store_row(engine, handle, store)
    user = await _assert_namespace_owner(request, handle)
    body = await _read_request_body(request)
    if str(body.get("confirm") or "").strip() != store:
        raise HTTPException(status_code=422, detail="confirm must equal the store address")

    scope = scope_id(handle, store)
    reg = await _platform_db(engine)
    await reg["store_registry"].update_one(
        {"handle": handle, "store": store},
        {"$set": {"status": STORE_STATUS_DELETING, "updated_at": datetime.now(timezone.utc)}},
    )
    KNOWN_STORES.discard(scope)
    dropped = await _drop_store_collections(engine, scope)
    await reg["store_registry"].delete_one({"handle": handle, "store": store})
    await _cleanup_namespace_if_empty(engine, handle)
    await refresh_known_stores()
    await _audit_store_event(engine, "store_deleted", handle, store, user, dropped=len(dropped))
    logger.info(f"Deleted store '{scope}' — dropped {len(dropped)} collection(s)")
    return JSONResponse({"ok": True, "handle": handle, "store": store, "dropped": dropped})


async def _store_collection_names(engine, scope: str) -> list[str]:
    """Physical ``{scope}_*`` collection names (exact prefix, trailing ``_``)."""
    raw_db = engine.connection_manager.mongo_db
    prefix = f"{scope}_"
    return sorted(n for n in await raw_db.list_collection_names() if n.startswith(prefix))


@app.get("/manage/stores/{handle}/{store}/export")
async def manage_export_store(handle: str, store: str, request: Request) -> JSONResponse:
    """Dump every ``{scope}_*`` collection as portable JSON (owner/superuser).

    Uses ``bson.json_util`` so ObjectIds and dates round-trip cleanly. The
    result is a single JSON document a caller can save and later re-``import``;
    it is the low-risk, blast-radius mitigation for the shared-DB model.
    """
    engine = app.state.engine
    await _require_store_row(engine, handle, store)
    user = await _assert_namespace_owner(request, handle)

    scope = scope_id(handle, store)
    raw_db = engine.connection_manager.mongo_db
    prefix = f"{scope}_"
    collections: dict[str, list[Any]] = {}
    total = 0
    for full in await _store_collection_names(engine, scope):
        short = full[len(prefix):]
        docs = await raw_db[full].find({}).to_list(length=None)
        collections[short] = docs
        total += len(docs)

    await _audit_store_event(engine, "store_exported", handle, store, user, documents=total)
    # Emit MongoDB Extended JSON (via json_util) as a raw body so ObjectIds and
    # dates survive verbatim; JSONResponse would re-encode and choke on them.
    body = json_util.dumps(
        {"handle": handle, "store": store, "scope": scope, "collections": collections},
        ensure_ascii=False,
    )
    filename = f"{scope}-export.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/manage/stores/{handle}/{store}/import")
async def manage_import_store(handle: str, store: str, request: Request) -> JSONResponse:
    """Restore an ``export`` dump into ``{handle}/{store}`` (owner/superuser).

    Provisions the scope if missing, then inserts each collection's docs into
    ``{scope}_{name}`` via ``json_util`` (so ids/dates round-trip). Refuses to
    write into a non-empty target unless ``?overwrite=1`` (which first drops the
    scope's collections). This is a coarse, whole-store restore, not a merge.
    """
    engine = app.state.engine
    user = await _assert_namespace_owner(request, handle)
    try:
        handle = _validate_slug(handle)
        store = _validate_slug(store)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    raw_body = await request.body()
    try:
        payload = json_util.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="body must be valid export JSON") from exc
    collections = payload.get("collections") if isinstance(payload, dict) else None
    if not isinstance(collections, dict) or not collections:
        raise HTTPException(status_code=422, detail="no 'collections' found in the import body")

    scope = scope_id(handle, store)
    overwrite = request.query_params.get("overwrite") in ("1", "true", "yes")

    # Ensure the target exists so its scope routes and has indexes. A store we
    # create here is brand new (its only content is the provisioning seed), so
    # we treat it as an implicit overwrite target — the caller is restoring.
    reg = await _platform_db(engine)
    created = await reg["store_registry"].find_one({"handle": handle, "store": store}) is None
    if created:
        await provision_store(engine, handle, store, str(payload.get("name") or store), user.get("email"))
        if not await rbac.count_owners(reg["namespace_members"], handle):
            await rbac.add_member(
                reg["namespace_members"], handle, rbac.normalize_email(user.get("email")), rbac.ROLE_OWNER
            )

    raw_db = engine.connection_manager.mongo_db
    non_empty = False
    for full in await _store_collection_names(engine, scope):
        if await raw_db[full].estimated_document_count() > 0:
            non_empty = True
            break
    if non_empty and not overwrite and not created:
        raise HTTPException(status_code=409, detail="target store has data; pass ?overwrite=1 to replace")
    if overwrite or created:
        await _drop_store_collections(engine, scope)

    inserted: dict[str, int] = {}
    for short, docs in collections.items():
        if not isinstance(docs, list) or not docs:
            continue
        short = str(short)
        if not re.fullmatch(r"[A-Za-z0-9_]+", short):
            continue  # never let a crafted name escape the scope prefix
        # The engine tags every doc with an ``app_id`` equal to its scope and
        # filters reads on it, so re-home imported docs to the target scope
        # (a dump from another store would otherwise be invisible here).
        prepared = [
            {**d, "app_id": scope} if isinstance(d, dict) and "app_id" in d else d
            for d in docs
        ]
        await raw_db[f"{scope}_{short}"].insert_many(prepared)
        inserted[short] = len(prepared)

    await refresh_known_stores()
    await _audit_store_event(
        engine, "store_imported", handle, store, user,
        documents=sum(inserted.values()), overwrite=overwrite,
    )
    logger.info(f"Imported {sum(inserted.values())} doc(s) into '{scope}' (overwrite={overwrite})")
    return JSONResponse(
        {"ok": True, "handle": handle, "store": store, "inserted": inserted, "overwrite": overwrite}
    )


@app.post("/manage/reconcile")
async def manage_reconcile(request: Request) -> JSONResponse:
    """Run the store reconciler (platform superuser only).

    Finishes stuck provisions and reports orphaned ``{scope}_*`` collections;
    pass ``{"drop_orphans": true}`` to also drop them.
    """
    await _require_platform_admin(request)
    body = await _read_request_body(request)
    result = await reconcile_stores(app.state.engine, drop_orphans=bool(body.get("drop_orphans")))
    return JSONResponse({"ok": True, **result})


# ── Namespace team management (owner-gated) ─────────────────────────────
#
# Team membership is per handle. These live under the store admin surface
# (/{handle}/{store}/admin/team) so the handle is taken from the request scope,
# and are gated by _require_namespace_owner. Invites are stateless signed JWTs;
# the invitee accepts while logged in with the invited email.


@app.get("/admin/team/members")
async def team_list(request: Request) -> JSONResponse:
    """List the namespace's members (owner or superuser)."""
    user, handle = await _require_namespace_owner(request)
    pdb = await _platform_db(request.app.state.engine)
    members = await rbac.list_members(pdb["namespace_members"], handle)
    return JSONResponse({"ok": True, "handle": handle, "members": members})


@app.post("/admin/team/invite")
async def team_invite(request: Request) -> JSONResponse:
    """Issue a signed invite for ``{email, role}`` in this namespace (owner only)."""
    user, handle = await _require_namespace_owner(request)
    body = await _read_request_body(request)
    email = rbac.normalize_email(body.get("email"))
    role = str(body.get("role") or rbac.ROLE_VIEWER).strip().lower()
    if "@" not in email or "." not in email:
        raise HTTPException(status_code=422, detail="a valid email is required")
    if role not in rbac.NAMESPACE_ROLES:
        raise HTTPException(status_code=422, detail=f"role must be one of {rbac.NAMESPACE_ROLES}")

    token = encode_jwt_token(
        {"purpose": "ns_invite", "handle": handle, "email": email, "role": role},
        _invite_secret(),
        expires_in=INVITE_TTL_SECONDS,
    )
    # Acceptance lives on the reserved /manage surface (not /admin) so the
    # invitee — a non-member by definition — is not blocked by the overlay.
    invite_url = f"/manage/invite?token={token}"
    await _audit_store_event(
        request.app.state.engine, "team_invited", handle, request.scope.get("store"), user,
        invited=email, role=role,
    )
    return JSONResponse({"ok": True, "handle": handle, "email": email, "role": role, "token": token, "invite_url": invite_url})


@app.get("/manage/invite", response_class=HTMLResponse)
async def invite_page(request: Request):
    """Landing for an invite link: shows accept CTA (or a sign-in prompt)."""
    token = (request.query_params.get("token") or "").strip()
    user = await get_current_user(request)
    claims: dict[str, Any] | None = None
    if token:
        try:
            decoded = decode_jwt_token(token, _invite_secret())
            if decoded.get("purpose") == "ns_invite":
                claims = decoded
        except Exception:  # noqa: BLE001 — render the "invalid/expired" state
            claims = None
    return _templates.TemplateResponse(
        request,
        "invite.html",
        {"user": user, "token": token, "invite": claims, "store": {}},
    )


@app.post("/manage/invite/accept")
async def team_accept(request: Request) -> JSONResponse:
    """Accept a namespace invite (must be logged in as the invited email)."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="sign in to accept an invite")
    body = await _read_request_body(request)
    token = str(body.get("token") or (request.query_params.get("token") or "")).strip()
    if not token:
        raise HTTPException(status_code=422, detail="token is required")
    try:
        claims = decode_jwt_token(token, _invite_secret())
    except Exception as exc:  # noqa: BLE001 — any decode failure is a bad/expired invite
        raise HTTPException(status_code=400, detail="invalid or expired invite") from exc
    if claims.get("purpose") != "ns_invite":
        raise HTTPException(status_code=400, detail="invalid invite")

    handle = claims.get("handle")
    email = rbac.normalize_email(claims.get("email"))
    role = claims.get("role")
    if role not in rbac.NAMESPACE_ROLES or not handle:
        raise HTTPException(status_code=400, detail="invalid invite")
    if rbac.normalize_email(user.get("email")) != email:
        raise HTTPException(status_code=403, detail="this invite was issued to a different email")

    pdb = await _platform_db(request.app.state.engine)
    await rbac.add_member(pdb["namespace_members"], handle, email, role)
    await refresh_known_stores()
    await _audit_store_event(
        request.app.state.engine, "team_joined", handle, None, user, email=email, role=role
    )
    return JSONResponse({"ok": True, "handle": handle, "email": email, "role": role})


@app.patch("/admin/team/members")
async def team_change_role(request: Request) -> JSONResponse:
    """Change a member's role (owner only), keeping ≥1 owner per handle."""
    user, handle = await _require_namespace_owner(request)
    body = await _read_request_body(request)
    email = rbac.normalize_email(body.get("email"))
    role = str(body.get("role") or "").strip().lower()
    if role not in rbac.NAMESPACE_ROLES:
        raise HTTPException(status_code=422, detail=f"role must be one of {rbac.NAMESPACE_ROLES}")
    pdb = await _platform_db(request.app.state.engine)
    members = pdb["namespace_members"]
    existing = await members.find_one({"handle": handle, "email": email})
    if not existing:
        raise HTTPException(status_code=404, detail="member not found")
    # Guardrail: never demote the last remaining owner.
    if existing.get("role") == rbac.ROLE_OWNER and role != rbac.ROLE_OWNER:
        if await rbac.count_owners(members, handle) <= 1:
            raise HTTPException(status_code=409, detail="a namespace must keep at least one owner")
    await rbac.add_member(members, handle, email, role)
    await _audit_store_event(
        request.app.state.engine, "team_role_changed", handle, None, user, email=email, role=role
    )
    return JSONResponse({"ok": True, "handle": handle, "email": email, "role": role})


@app.delete("/admin/team/members")
async def team_remove_member(request: Request) -> JSONResponse:
    """Remove a member (owner only), keeping ≥1 owner per handle."""
    user, handle = await _require_namespace_owner(request)
    body = await _read_request_body(request)
    email = rbac.normalize_email(body.get("email"))
    pdb = await _platform_db(request.app.state.engine)
    members = pdb["namespace_members"]
    existing = await members.find_one({"handle": handle, "email": email})
    if not existing:
        raise HTTPException(status_code=404, detail="member not found")
    if existing.get("role") == rbac.ROLE_OWNER and await rbac.count_owners(members, handle) <= 1:
        raise HTTPException(status_code=409, detail="a namespace must keep at least one owner")
    await members.delete_one({"handle": handle, "email": email})
    await _audit_store_event(
        request.app.state.engine, "team_removed", handle, None, user, email=email
    )
    return JSONResponse({"ok": True, "handle": handle, "email": email})


@app.get("/admin/team", response_class=HTMLResponse, include_in_schema=False, name="team_page")
async def team_page(request: Request):  # pragma: no cover - thin template wrapper
    """Owner-only team management page (rendered under the store admin)."""
    user, handle = await _require_namespace_owner(request)
    pdb = await _platform_db(request.app.state.engine)
    members = await rbac.list_members(pdb["namespace_members"], handle)
    store_db = await _scoped_db_for_request(request)
    store_doc = await store_db["stores"].find_one({}) or {}
    return _templates.TemplateResponse(
        request,
        "admin_team.html",
        {"handle": handle, "members": members, "store": store_doc, "roles": rbac.NAMESPACE_ROLES},
    )


# ── Public self-serve signup ────────────────────────────────────────────


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    """Public "claim your handle + create your first store" page."""
    user = await get_current_user(request)
    return _templates.TemplateResponse(
        request, "signup.html", {"user": user, "store_templates": STORE_TEMPLATES, "store": {}}
    )


@app.post("/signup")
async def signup(
    request: Request, _rl: None = Depends(_rate_limit("signup", per="ip"))
) -> JSONResponse:
    """Claim a handle + first store, create a member user, and seed ownership.

    Validates the (globally unique) handle and store slug, creates a non-admin
    user carrying its ``handle``, provisions ``{handle}__{store}``, and inserts
    the owner membership. The client then logs in via ``/auth/login``.
    """
    engine = app.state.engine
    body = await _read_request_body(request)
    email = rbac.normalize_email(body.get("email"))
    password = str(body.get("password") or "")
    try:
        handle = _validate_slug(str(body.get("handle") or ""))
        store = _validate_slug(str(body.get("slug") or body.get("store") or "shop"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if "@" not in email or "." not in email:
        raise HTTPException(status_code=422, detail="a valid email is required")
    if len(password) < 8:
        raise HTTPException(status_code=422, detail="password must be at least 8 characters")

    pdb = await _platform_db(engine)
    reg = await _platform_db(engine)
    if await reg["store_registry"].find_one({"handle": handle}, {"_id": 1}):
        raise HTTPException(status_code=409, detail=f"the handle '{handle}' is already taken")
    if await pdb["users"].find_one({"email": email}, {"_id": 1}):
        raise HTTPException(status_code=409, detail="an account with that email already exists")

    created = await create_app_user(pdb, email, password, role="member")
    if not created:
        raise HTTPException(status_code=409, detail="could not create the account")
    with contextlib.suppress(Exception):
        await pdb["users"].update_one({"_id": created["_id"]}, {"$set": {"handle": handle}})

    try:
        result = await provision_store(
            engine, handle, store, str(body.get("store_name") or ""), email,
            template=str(body.get("business_type") or ""),
        )
    except ValueError as exc:
        # Roll back the just-created user so the handle/email stay claimable.
        with contextlib.suppress(Exception):
            await pdb["users"].delete_one({"_id": created["_id"]})
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await rbac.add_member(pdb["namespace_members"], handle, email, rbac.ROLE_OWNER)
    await _audit_store_event(engine, "signup", handle, store, {"email": email}, name=result["name"])
    logger.info(f"Signup: {email} claimed /{handle} with store '{store}'")
    return JSONResponse(
        {"ok": True, "handle": handle, "store": store, "email": email, "url": _store_url(handle, store)},
        status_code=201,
    )


# ── Observability ────────────────────────────────────────────────────────
#
# /healthz is a pure liveness probe (process up). /readyz is a readiness probe
# that actually pings Mongo, so a load balancer can drain a replica that lost
# its database. /manage/status is a richer, superuser-only metrics snapshot.


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness: the process is up and serving. No dependencies touched."""
    return JSONResponse({"status": "ok"})


@app.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness: verify Mongo answers a ping; report AI + store-count context.

    Returns 200 only when the database is reachable; 503 otherwise, so
    orchestrators route traffic away from a replica that can't serve data.
    """
    engine = getattr(app.state, "engine", None)
    out: dict[str, Any] = {
        "status": "ok",
        "request_id": request.scope.get("request_id"),
        "mongo": False,
        "ai_configured": bool(ai_editor.GEMINI_API_KEY),
    }
    try:
        await engine.connection_manager.mongo_db.command("ping")
        out["mongo"] = True
        reg = await _platform_db(engine)
        out["stores"] = await reg["store_registry"].count_documents(_routable_status_query())
    except Exception as exc:  # noqa: BLE001 — readiness must report, not raise
        out["status"] = "unavailable"
        out["error"] = str(exc)[:200]
        return JSONResponse(out, status_code=503)
    return JSONResponse(out)


@app.get("/manage/status")
async def manage_status(request: Request) -> JSONResponse:
    """Superuser-only platform metrics snapshot (counts, not per-store scans)."""
    await _require_platform_admin(request)
    engine = app.state.engine
    reg = await _platform_db(engine)

    by_status: dict[str, int] = {}
    handles: set[str] = set()
    async for doc in reg["store_registry"].find({}, {"status": 1, "handle": 1}):
        by_status[doc.get("status") or STORE_STATUS_READY] = (
            by_status.get(doc.get("status") or STORE_STATUS_READY, 0) + 1
        )
        if doc.get("handle"):
            handles.add(doc["handle"])

    mongo_ok = True
    with contextlib.suppress(Exception):
        await engine.connection_manager.mongo_db.command("ping")

    return JSONResponse(
        {
            "ok": True,
            "request_id": request.scope.get("request_id"),
            "mongo": mongo_ok,
            "ai": {"configured": bool(ai_editor.GEMINI_API_KEY), "model": ai_editor.GEMINI_MODEL},
            "stores": {"total": sum(by_status.values()), "by_status": by_status},
            "namespaces": len(handles),
            "members": await reg["namespace_members"].count_documents({}),
            "quotas": {
                "max_stores_per_handle": MAX_STORES_PER_HANDLE,
                "max_items_per_store": MAX_ITEMS_PER_STORE,
                "max_sections_per_store": MAX_SECTIONS_PER_STORE,
                "max_uploads_per_store": MAX_UPLOADS_PER_STORE,
            },
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("RELOAD")),
    )
