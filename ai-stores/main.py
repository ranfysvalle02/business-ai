"""
AI Stores — a multi-tenant shell over ``mdb-engine``.

One app instance serves **many stores** from a single entrypoint at
``stores.com/{store}``, administered by **one shared admin**. A store is just
a per-request database *scope*: the engine prefixes every collection with it
(``{slug}_items``) and tags documents with an ``app_id``, so stores are fully
isolated while sharing one deployment and one Mongo database.

Everything about the domain (collections, auto-CRUD, auth, SSR routes,
indexes, admin plane, reconciler, trash sweeper) is declared in
``manifest.json`` and wired by ``mdb_engine.quickstart``.

This module keeps only what does not belong in the manifest:

    * ``StoreScopeMiddleware`` + a ``get_scoped_db`` override that resolve
      ``/{store}/...`` to the right scope per request (auth stays global).
    * Runtime store provisioning (``provision_store``) and a ``store_registry``
      held in the platform scope, plus the ``/manage`` console to create and
      open stores — no redeploy needed to add a store.
    * Static asset mount for ``/static`` (PWA icons, service worker, css/js).
    * SSR route mounting (reads the manifest ``ssr`` block).
    * A public ``POST /api/submit-inquiry`` endpoint so unauthenticated
      visitors can submit leads without opening the ``inquiries`` collection
      to anonymous writes.
    * Cloudinary-backed ``POST /admin/upload-image`` / ``/admin/upload-video``
      endpoints gated to the ``admin`` role.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import cloudinary
import cloudinary.uploader
from cloudinary.utils import cloudinary_url
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mdb_engine import get_current_user, quickstart
from mdb_engine.auth.rate_limiter import RateLimit, create_rate_limit_store
from mdb_engine.dependencies import get_scoped_db
from mdb_engine.indexes import run_index_creation_for_collection
from mdb_engine.routing._ssr import mount_ssr_routes

import ai_editor
import notifications

load_dotenv()

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
# Default content copied into each new store scope (name/slug overridden per store).
_STORE_TEMPLATE: dict[str, list[dict[str, Any]]] = json.loads(STORE_TEMPLATE_PATH.read_text())

# ── Multi-tenant runtime configuration ─────────────────────────────────
#
# The platform scope holds cross-store data: the single global admin ``users``
# pool (seeded by the engine from ``auth.users``) and the ``store_registry``.
# Each store scope holds its own ``stores`` singleton, ``items``, ``sections``,
# ``specials``, ``slideshow`` and ``inquiries``.
PLATFORM_SLUG: str = _manifest_data["slug"]

# First path segments that are global — never a store, never scoped.
RESERVED_SEGMENTS = frozenset(
    {"static", "__mdb", "health", "favicon.ico", "robots.txt", "auth", "manage"}
)
# Slugs that would collide with a global or storefront route's first segment,
# or with the platform scope's own ``{PLATFORM_SLUG}_*`` collections.
BANNED_SLUGS = RESERVED_SEGMENTS | frozenset(
    {"admin", "api", "item", "contact", "sitemap.xml", "sitemap", "www", PLATFORM_SLUG}
)
# 3–40 chars, lowercase alnum + hyphen, no leading/trailing hyphen.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")

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

# Provisioned store slugs (status=ready), rebuilt from the registry at startup,
# on every lifecycle mutation, on a short TTL, and on registry change-stream
# events so multi-worker deployments converge. On a cache miss the middleware
# still falls back to a status-aware registry lookup.
KNOWN_STORES: set[str] = set()

_STORE_404_HTML = (
    "<!doctype html><meta charset='utf-8'><title>Store not found</title>"
    "<div style=\"font-family:system-ui;max-width:32rem;margin:12vh auto;"
    "text-align:center;background:#0b1120;color:#e2e8f0;padding:2rem 1.5rem;"
    "border-radius:12px\"><h1 style='font-size:1.5rem;margin:0 0 .5rem'>"
    "Store not found</h1><p style='color:#94a3b8;margin:0'>No store is "
    "published at this address.</p></div>"
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
    """Rebuild ``KNOWN_STORES`` from the registry (ready stores only).

    Reassigns the module-level set atomically so in-flight requests always
    read a complete snapshot. Returns the new set (handy in tests). Never
    raises — a refresh error leaves the previous cache in place.
    """
    global KNOWN_STORES
    try:
        reg = await _platform_db(app.state.engine)
        fresh: set[str] = set()
        async for doc in reg["store_registry"].find(_routable_status_query(), {"slug": 1}):
            if doc.get("slug"):
                fresh.add(doc["slug"])
        KNOWN_STORES = fresh
        return fresh
    except Exception as exc:  # noqa: BLE001 — never crash on a refresh error
        logger.warning(f"KNOWN_STORES refresh skipped: {exc}")
        return KNOWN_STORES


async def _store_is_registered(slug: str) -> bool:
    """Status-aware registry lookup used on an in-memory cache miss.

    Only ``ready`` (or legacy status-less) stores route; ``archived``,
    ``deleting``, ``provisioning`` and ``failed`` stores return ``False`` so
    they 404 across every worker.
    """
    try:
        reg = await _platform_db(app.state.engine)
        return await reg["store_registry"].find_one({"slug": slug, **_routable_status_query()}) is not None
    except Exception:  # noqa: BLE001 — never fail a request on a lookup error
        return False


class StoreScopeMiddleware:
    """Resolve ``/{store}/...`` to a per-request database scope.

    Added after ``quickstart`` so it is the outermost middleware — it runs
    before the engine's auth middleware and the router. For a known store it
    sets ``root_path`` to the ``/{store}`` prefix: Starlette then routes on the
    remaining path (``get_route_path``) so the engine's existing routes match
    unchanged, while ``request.url`` / ``base_url`` keep the prefix so canonical
    URLs, sitemaps and OG tags stay per-store correct. Auth is untouched — it
    is enforced by SSR ``auth`` flags and ``require_user`` deps, not by path.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        raw_path = scope.get("path") or "/"

        # Bare root → visitors land on a store; admins can head to /manage.
        if raw_path == "/":
            target = ("/" + sorted(KNOWN_STORES)[0] + "/") if KNOWN_STORES else "/manage"
            await self._send_redirect(send, target)
            return

        seg = raw_path.lstrip("/").split("/", 1)[0]

        # Global, un-scoped surfaces (static, auth, manage, health, …).
        if not seg or seg in RESERVED_SEGMENTS:
            await self.app(scope, receive, send)
            return

        # Everything else must be a store. Fall back to the registry on a miss.
        if seg not in KNOWN_STORES:
            if not await _store_is_registered(seg):
                await self._send_html(send, 404, _STORE_404_HTML)
                return
            KNOWN_STORES.add(seg)

        # Scope this request to the store.
        scope["store_slug"] = seg
        scope["root_path"] = scope.get("root_path", "") + "/" + seg
        if raw_path[len(seg) + 1:] == "":
            # "/x" → "/x/" so get_route_path() yields "/" for the home route.
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


async def _ensure_store_indexes(engine, slug: str) -> None:
    """Create a store scope's managed indexes via the public engine API.

    Driven entirely by the manifest's ``managed_indexes`` (including the unique
    constraints on ``stores.slug_id``, ``sections.key`` and ``items.item_code``),
    applied per store scope with ``run_index_creation_for_collection`` against
    the raw database using scoped ``{slug}_{collection}`` names. No private
    engine internals are touched, so this stays stable across engine upgrades.
    The per-doc ``app_id`` index is auto-ensured by the engine on first access.
    """
    managed = _manifest_data.get("managed_indexes") or {}
    if not managed:
        return
    try:
        raw_db = engine.connection_manager.mongo_db
    except Exception as exc:  # noqa: BLE001 — no raw handle → skip (best-effort)
        logger.warning(f"[{slug}] index creation skipped (no db handle): {exc}")
        return
    for col_name, index_defs in managed.items():
        if not index_defs:
            continue
        try:
            await run_index_creation_for_collection(
                db=raw_db,
                slug=slug,
                collection_name=f"{slug}_{col_name}",
                index_definitions=index_defs,
            )
        except Exception as exc:  # noqa: BLE001 — one bad collection never blocks the rest
            logger.warning(f"[{slug}] index creation for '{col_name}' skipped: {exc}")


def _validate_slug(slug: str) -> str:
    """Normalise + validate a store slug, or raise ``ValueError``."""
    slug = (slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError(
            "Store address must be 3–40 characters: lowercase letters, numbers and hyphens."
        )
    if slug in BANNED_SLUGS:
        raise ValueError(f"'{slug}' is reserved and can't be used as a store address.")
    return slug


async def provision_store(engine, slug: str, name: str) -> dict[str, str]:
    """Create a store scope end to end: register → indexes → seed → mark ready.

    Idempotent and step-logged. The registry row is written **first** with
    ``status="provisioning"`` so the store is observable before its
    collections exist. If seeding fails mid-way the row stays
    ``provisioning`` — never leaving orphan ``{slug}_*`` collections
    unaccounted for — and the reconciler (or a plain re-run) can finish it
    safely: seeding is additive by key and index creation is idempotent, so
    retries never clobber admin edits.
    """
    slug = _validate_slug(slug)
    name = (name or "").strip() or slug
    now = datetime.now(timezone.utc)

    reg = await _platform_db(engine)
    await reg["store_registry"].update_one(
        {"slug": slug},
        {
            "$set": {"name": name, "status": STORE_STATUS_PROVISIONING, "updated_at": now},
            "$setOnInsert": {"slug": slug, "created_at": now},
        },
        upsert=True,
    )

    db = await engine.get_scoped_db(slug)
    await _ensure_store_indexes(engine, slug)

    template = copy.deepcopy(_STORE_TEMPLATE)
    stores = template.get("stores") or [{}]
    stores[0]["name"] = name
    stores[0]["slug_id"] = slug
    await _seed_singleton(db, "stores", stores)
    await _seed_by_key(db, "sections", "key", template.get("sections", []))
    await _seed_by_key(db, "items", "item_code", template.get("items", []))
    await _seed_singleton(db, "specials", template.get("specials", []))
    await _seed_singleton(db, "slideshow", template.get("slideshow", []))

    await reg["store_registry"].update_one(
        {"slug": slug},
        {"$set": {"status": STORE_STATUS_READY, "updated_at": datetime.now(timezone.utc)}},
    )
    KNOWN_STORES.add(slug)
    logger.info(f"Provisioned store '{slug}' ({name})")
    return {"slug": slug, "name": name}


# ── Registry indexes, deprovisioning & reconciliation ──────────────────


def _registry_collection(engine):
    """Raw Motor handle for the physical ``{PLATFORM_SLUG}_store_registry``."""
    return engine.connection_manager.mongo_db[f"{PLATFORM_SLUG}_store_registry"]


async def _ensure_registry_indexes(engine) -> None:
    """Best-effort unique+status indexes on the store registry collection."""
    try:
        col = _registry_collection(engine)
    except Exception as exc:  # noqa: BLE001 — no raw handle → skip
        logger.warning(f"registry index creation skipped (no db handle): {exc}")
        return
    try:
        await col.create_index("slug", unique=True, name="store_registry_slug_unique")
        await col.create_index("status", name="store_registry_status")
    except Exception as exc:  # noqa: BLE001 — never block boot on index creation
        logger.warning(f"registry index creation skipped: {exc}")


async def _drop_store_collections(engine, slug: str) -> list[str]:
    """Drop every physical ``{slug}_*`` collection. Returns dropped names.

    The trailing underscore in the prefix makes this exact: validated slugs
    never contain ``_``, so ``acme_`` matches ``acme_items`` but never
    ``acme2_items`` or the platform's ``{PLATFORM_SLUG}_*`` collections.

    The ``{slug}_stores`` singleton is dropped **last** on purpose: it is the
    marker the orphan scan keys on, so if the process dies mid-drop the
    leftover collections remain detectable (and re-cleanable) as an orphan.
    """
    raw_db = engine.connection_manager.mongo_db
    prefix = f"{slug}_"
    stores_name = f"{slug}_stores"
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
    engine, event: str, slug: str, actor: dict[str, Any] | None = None, **extra: Any
) -> None:
    """Best-effort platform-scope audit entry for a store lifecycle action.

    Writes to the platform ``audit_log`` so there is an operational trail of
    who did what (created/renamed/archived/restored/deleted) and when. Never
    raises — an audit write must never fail the operation it records.
    """
    try:
        reg = await _platform_db(engine)
        doc: dict[str, Any] = {
            "event": event,
            "slug": slug,
            "actor": (actor or {}).get("email") or (actor or {}).get("role") or "system",
            "timestamp": datetime.now(timezone.utc),
        }
        doc.update(extra)
        await reg["audit_log"].insert_one(doc)
    except Exception as exc:  # noqa: BLE001 — audit is best-effort
        logger.debug(f"store audit '{event}' for '{slug}' skipped: {exc}")


async def _find_orphan_slugs(engine, reg) -> list[str]:
    """Slugs that own a ``{slug}_stores`` collection but have no registry row.

    Every real store owns a ``{slug}_stores`` singleton, so that collection is
    the reliable marker of a store slug (platform/engine collections such as
    ``apps_config`` or ``{PLATFORM_SLUG}_store_registry`` never match).
    """
    raw_db = engine.connection_manager.mongo_db
    suffix = "_stores"
    candidates: set[str] = set()
    for full in await raw_db.list_collection_names():
        if full.endswith(suffix):
            slug = full[: -len(suffix)]
            if slug and slug != PLATFORM_SLUG:
                candidates.add(slug)
    orphans: list[str] = []
    for slug in sorted(candidates):
        if await reg["store_registry"].find_one({"slug": slug}, {"_id": 1}) is None:
            orphans.append(slug)
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
      that crashed mid-drop) have their ``{slug}_*`` collections re-dropped and
      the registry row removed, finishing the deprovision.
    * **Orphan collections** — physical ``{slug}_*`` collections whose slug has
      no registry row (a crash before the first registry write, or a delete
      that lost its row before dropping). Reported and returned; pass
      ``drop_orphans=True`` to drop them.
    """
    result: dict[str, Any] = {"retried": [], "failed": [], "deleted": [], "orphans": [], "dropped": []}
    reg = await _platform_db(engine)
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_naive - timedelta(minutes=PROVISION_STUCK_MINUTES)

    async for doc in reg["store_registry"].find({"status": STORE_STATUS_PROVISIONING}):
        slug = doc.get("slug")
        updated = _as_naive_utc(doc.get("updated_at") or doc.get("created_at"))
        if not slug or (updated is not None and updated > cutoff):
            continue
        try:
            await provision_store(engine, slug, doc.get("name") or slug)
            result["retried"].append(slug)
            await _audit_store_event(engine, "store_provision_retried", slug)
        except Exception as exc:  # noqa: BLE001 — surface as failed, never crash the pass
            logger.warning(f"Reconcile: provisioning retry failed for '{slug}': {exc}")
            await reg["store_registry"].update_one(
                {"slug": slug},
                {"$set": {"status": STORE_STATUS_FAILED, "updated_at": datetime.now(timezone.utc)}},
            )
            result["failed"].append(slug)
            await _audit_store_event(engine, "store_provision_failed", slug, error=str(exc))

    async for doc in reg["store_registry"].find({"status": STORE_STATUS_DELETING}):
        slug = doc.get("slug")
        updated = _as_naive_utc(doc.get("updated_at") or doc.get("created_at"))
        if not slug or (updated is not None and updated > cutoff):
            continue
        try:
            KNOWN_STORES.discard(slug)
            dropped = await _drop_store_collections(engine, slug)
            await reg["store_registry"].delete_one({"slug": slug})
            result["dropped"].extend(dropped)
            result["deleted"].append(slug)
            await _audit_store_event(engine, "store_delete_recovered", slug, dropped=len(dropped))
        except Exception as exc:  # noqa: BLE001 — one bad recovery never blocks the pass
            logger.warning(f"Reconcile: delete recovery failed for '{slug}': {exc}")

    orphans = await _find_orphan_slugs(engine, reg)
    result["orphans"] = orphans
    if orphans:
        logger.warning(f"Reconcile: orphan store collections with no registry row: {orphans}")
        if drop_orphans:
            for slug in orphans:
                dropped = await _drop_store_collections(engine, slug)
                result["dropped"].extend(dropped)
                await _audit_store_event(engine, "store_orphan_dropped", slug, dropped=len(dropped))

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
    """Prepare the registry and hydrate ``KNOWN_STORES`` (ready stores only).

    Ensures the registry indexes exist, seeds a ``demo`` store on a wholly
    fresh platform (no registry rows at all), then rebuilds the cache from
    ``ready`` rows.
    """
    try:
        engine = app.state.engine
        await _ensure_registry_indexes(engine)
        reg = await _platform_db(engine)
        has_any = await reg["store_registry"].find_one({}, {"_id": 1}) is not None
        if not has_any:
            await provision_store(engine, "demo", "Demo Store")
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
        base_path = f"/{request.scope['store_slug']}" if request.scope.get("store_slug") else ""
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

    base_path = f"/{request.scope['store_slug']}" if request.scope.get("store_slug") else ""
    return JSONResponse(
        {"ok": True, "id": str(result.inserted_id), "redirect": f"{base_path}/contact/thanks"},
        status_code=201,
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
    uploader. Auth is enforced manually against the engine session so we
    stay consistent with every other admin-gated surface.
    """
    user = await get_current_user(request)
    if not user or str(user.get("role") or "").lower() != "admin":
        raise HTTPException(status_code=401, detail="admin required")

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
    """Admin-only Cloudinary video upload with intelligent compression."""
    user = await get_current_user(request)
    if not user or str(user.get("role") or "").lower() != "admin":
        raise HTTPException(status_code=401, detail="admin required")

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


# ── Conversational store editor (local Ollama) ──────────────────────────
#
# Two admin-only endpoints power the chat widget. They mirror the safety
# ethos of the rest of the project: the model only *proposes* structured
# ops, the backend validates them against the manifest, and nothing is
# written until the admin confirms via /admin/ai/apply.


async def _require_admin(request: Request) -> dict[str, Any]:
    user = await get_current_user(request)
    if not user or str(user.get("role") or "").lower() != "admin":
        raise HTTPException(status_code=401, detail="admin required")
    return user


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
    await _require_admin(request)
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
    user = await _require_admin(request)
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

    results = await ai_editor.apply_ops(db, normalized, user)
    ok = all(r.get("ok") for r in results)
    return JSONResponse(
        {"ok": ok, "results": results, "diff": diff, "warnings": errors},
        status_code=200 if ok else 207,
    )


# ── Platform store-management console (/manage) ─────────────────────────
#
# One shared admin manages every store here. The page shows a login form when
# signed out (posts to the global /auth/login) and a store picker + create
# form when signed in. All platform data lives in the platform scope.


@app.get("/manage", response_class=HTMLResponse)
async def manage_home(request: Request):
    """Store picker + create form for the shared admin (login form otherwise)."""
    user = await get_current_user(request)
    is_admin = bool(user and str(user.get("role") or "").lower() == "admin")
    stores: list[dict[str, str]] = []
    if is_admin:
        reg = await _platform_db(app.state.engine)
        async for doc in reg["store_registry"].find({}).sort("created_at", 1):
            stores.append(
                {
                    "slug": doc.get("slug", ""),
                    "name": doc.get("name") or doc.get("slug", ""),
                    "status": doc.get("status") or STORE_STATUS_READY,
                }
            )
    return _templates.TemplateResponse(
        request,
        "manage.html",
        {"user": user, "is_admin": is_admin, "stores": stores, "store": {}},
    )


@app.post("/manage/stores")
async def manage_create_store(request: Request) -> JSONResponse:
    """Provision a new store at runtime (admin only) — no redeploy needed."""
    user = await _require_admin(request)
    body = await _read_request_body(request)
    try:
        result = await provision_store(
            app.state.engine, str(body.get("slug") or ""), str(body.get("name") or "")
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _audit_store_event(app.state.engine, "store_created", result["slug"], user, name=result["name"])
    return JSONResponse(
        {"ok": True, "slug": result["slug"], "name": result["name"], "url": f"/{result['slug']}/"},
        status_code=201,
    )


async def _require_store_row(engine, slug: str) -> dict[str, Any]:
    """Fetch a registry row for a lifecycle op, or raise 404."""
    reg = await _platform_db(engine)
    doc = await reg["store_registry"].find_one({"slug": slug})
    if not doc:
        raise HTTPException(status_code=404, detail="store not found")
    return doc


@app.patch("/manage/stores/{slug}")
async def manage_rename_store(slug: str, request: Request) -> JSONResponse:
    """Rename a store's display name (admin only).

    The address/slug is immutable in this version: changing it would break
    bookmarks and canonical URLs and require a full collection rename plus an
    ``app_id`` rewrite. Only the human-readable ``name`` changes here.
    """
    user = await _require_admin(request)
    engine = app.state.engine
    await _require_store_row(engine, slug)
    body = await _read_request_body(request)
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    now = datetime.now(timezone.utc)
    reg = await _platform_db(engine)
    await reg["store_registry"].update_one({"slug": slug}, {"$set": {"name": name, "updated_at": now}})
    # Keep the store's own singleton in sync so the storefront shows the new name.
    store_db = await engine.get_scoped_db(slug)
    await store_db["stores"].update_one({}, {"$set": {"name": name}})
    await _audit_store_event(engine, "store_renamed", slug, user, name=name)
    logger.info(f"Renamed store '{slug}' -> {name!r}")
    return JSONResponse({"ok": True, "slug": slug, "name": name})


@app.post("/manage/stores/{slug}/archive")
async def manage_archive_store(slug: str, request: Request) -> JSONResponse:
    """Suspend a store: it stops routing (404s) but its data is kept."""
    user = await _require_admin(request)
    if slug == PLATFORM_SLUG:
        raise HTTPException(status_code=400, detail="cannot archive the platform")
    reg = await _platform_db(app.state.engine)
    res = await reg["store_registry"].update_one(
        {"slug": slug},
        {"$set": {"status": STORE_STATUS_ARCHIVED, "updated_at": datetime.now(timezone.utc)}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="store not found")
    await refresh_known_stores()
    await _audit_store_event(app.state.engine, "store_archived", slug, user)
    logger.info(f"Archived store '{slug}'")
    return JSONResponse({"ok": True, "slug": slug, "status": STORE_STATUS_ARCHIVED})


@app.post("/manage/stores/{slug}/restore")
async def manage_restore_store(slug: str, request: Request) -> JSONResponse:
    """Re-enable an archived (or failed) store."""
    user = await _require_admin(request)
    reg = await _platform_db(app.state.engine)
    res = await reg["store_registry"].update_one(
        {"slug": slug},
        {"$set": {"status": STORE_STATUS_READY, "updated_at": datetime.now(timezone.utc)}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="store not found")
    await refresh_known_stores()
    await _audit_store_event(app.state.engine, "store_restored", slug, user)
    logger.info(f"Restored store '{slug}'")
    return JSONResponse({"ok": True, "slug": slug, "status": STORE_STATUS_READY})


@app.delete("/manage/stores/{slug}")
async def manage_delete_store(slug: str, request: Request) -> JSONResponse:
    """Permanently deprovision a store (admin only).

    Drops every ``{slug}_*`` collection and removes the registry row. This is
    **irreversible** — use archive for a reversible suspend. Requires
    ``{"confirm": "<slug>"}`` in the body to guard against accidents.
    """
    user = await _require_admin(request)
    if slug == PLATFORM_SLUG:
        raise HTTPException(status_code=400, detail="cannot delete the platform")
    engine = app.state.engine
    await _require_store_row(engine, slug)
    body = await _read_request_body(request)
    if str(body.get("confirm") or "").strip() != slug:
        raise HTTPException(status_code=422, detail="confirm must equal the store slug")

    reg = await _platform_db(engine)
    await reg["store_registry"].update_one(
        {"slug": slug}, {"$set": {"status": STORE_STATUS_DELETING, "updated_at": datetime.now(timezone.utc)}}
    )
    KNOWN_STORES.discard(slug)
    dropped = await _drop_store_collections(engine, slug)
    await reg["store_registry"].delete_one({"slug": slug})
    await refresh_known_stores()
    await _audit_store_event(engine, "store_deleted", slug, user, dropped=len(dropped))
    logger.info(f"Deleted store '{slug}' — dropped {len(dropped)} collection(s)")
    return JSONResponse({"ok": True, "slug": slug, "dropped": dropped})


@app.post("/manage/reconcile")
async def manage_reconcile(request: Request) -> JSONResponse:
    """Run the store reconciler (admin only).

    Finishes stuck provisions and reports orphaned ``{slug}_*`` collections;
    pass ``{"drop_orphans": true}`` to also drop them.
    """
    await _require_admin(request)
    body = await _read_request_body(request)
    result = await reconcile_stores(app.state.engine, drop_orphans=bool(body.get("drop_orphans")))
    return JSONResponse({"ok": True, **result})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("RELOAD")),
    )
