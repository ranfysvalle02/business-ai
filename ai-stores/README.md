# AI Stores

A single app instance that serves **many storefronts** at
`stores.com/{handle}/{store}`, where anyone can **claim a handle** (their
namespace) and open stores under it — no redeploy to add a store. Built as a
thin shell over [`mdb-engine`](blog.md): the domain (collections, auto-CRUD,
auth, SSR routes, indexes, admin plane) is declared in
[`manifest.json`](manifest.json); [`main.py`](main.py) adds only what makes it
multi-tenant, plus [`rbac.py`](rbac.py) for per-namespace roles.

- **Per-user namespaces.** The first URL segment is a user's **handle**, the
  second is a **store** under it (`/acme/coffee`). One user owns many stores;
  `/acme/` is a public landing listing that handle's stores.
- **Self-serve signup.** Anyone can `/signup` to claim a globally unique handle
  and open their first store — created, seeded, indexed, and live in one request.
- **Layered RBAC.** Identity stays global (one `users` pool + cookie);
  authorization is layered **per namespace** — `owner`/`editor`/`viewer` roles
  in `namespace_members` map to the engine's effective roles. The seeded
  `ADMIN_EMAIL` is a **platform superuser** with access to every namespace.
- **Public storefronts stay public.** Viewing a store and submitting an inquiry
  never require membership; only writes and admin surfaces are gated.
- **Isolated data.** The engine prefixes each store's collections
  (`acme__coffee_items`) and tags docs with an `app_id`, so stores can't see
  each other — across handles *or* within one.
- **Operable.** Liveness/readiness probes (`/healthz`, `/readyz`), a superuser
  metrics snapshot (`/manage/status`), per-request IDs, optional per-store
  quotas, and per-store JSON export/restore for backups.

---

## Quick start

```bash
cd ai-stores
make init        # create .env with generated secrets (edit ADMIN_* first)
make up          # build + run at http://localhost:8000
# (optional) set GEMINI_API_KEY in .env to enable the AI editor
```

On first boot with an empty database, a **`demo/shop`** store is provisioned
automatically (owned by the platform admin). Open:

- `http://localhost:8000/` → redirects to `/manage`
- `http://localhost:8000/manage` → sign in (superuser or namespace member)
- `http://localhost:8000/signup` → claim a handle + open your first store
- `http://localhost:8000/demo/` → the `demo` namespace landing
- `http://localhost:8000/demo/shop/` → the demo storefront
- `http://localhost:8000/demo/shop/admin/dashboard` → that store's admin

Default admin credentials come from `.env` (`ADMIN_EMAIL` / `ADMIN_PASSWORD`) —
change them before exposing anything. That account is the platform superuser.

---

## How it works

```
GET /acme/coffee/admin/items
      │
      ▼
StoreScopeMiddleware ── "acme" is a known handle, "coffee" a known store
      │  sets scope[handle]=acme, scope[store]=coffee,
      │  scope[store_slug]=acme__coffee, root_path=/acme/coffee
      ▼
engine auth middleware ── resolves the ONE global identity (path-agnostic)
      │
      ▼
StoreRoleOverlayMiddleware ── membership(acme, user) → user_roles;
      │  hard-blocks non-members on /admin/* only (public reads pass)
      ▼
router ── matches /admin/items (root_path stripped)
      │
      ▼
Depends(get_scoped_db) override ── returns get_scoped_db("acme__coffee")
      │
      ▼
reads/writes acme__coffee_items  (app_id = acme__coffee)
```

Pieces in [`main.py`](main.py) + [`rbac.py`](rbac.py):

1. **`StoreScopeMiddleware`** resolves `/{handle}/{store}/...` to a per-request
   scope by setting `root_path`. Existing engine routes match on the stripped
   path, while `request.url`/`base_url` keep the prefix so canonical URLs,
   sitemaps, and OG tags stay per-store correct. Unknown handle/store → 404;
   `/{handle}/` (no store) → a public namespace landing.
2. **`get_scoped_db` override** re-scopes the one dependency every data path uses
   (SSR pages, auto-CRUD `/api/*`, feeds, custom endpoints).
3. **Auth stays global** — one `users` pool in the platform scope, one cookie,
   valid on every store.
4. **`StoreRoleOverlayMiddleware`** layers per-namespace RBAC: it rewrites the
   caller's effective `user_roles` from their membership of the handle (gating
   `/api` writes) and hard-blocks non-members on the store's `/admin/*` surface —
   never on a public read. See [`rbac.py`](rbac.py) for the role mapping.

### Scopes

- **Platform scope** (`= manifest slug`): global `users`, `store_registry`,
  `namespace_members`, `audit_log`.
- **Store scope** (per `{handle}__{store}`): `stores` singleton, `items`,
  `sections`, `specials`, `slideshow`, `inquiries`.
- **Reserved first segments** (never a handle): `static`, `__mdb`, `health`,
  `favicon.ico`, `robots.txt`, `auth`, `manage`, `signup`.

### Roles

| Role | Scope | Can |
| --- | --- | --- |
| **platform superuser** (`ADMIN_EMAIL`) | every namespace | everything, incl. `/manage` all-namespace ops + reconcile |
| **owner** | one handle | edit content, create/delete stores, manage team |
| **editor** | one handle | edit store content |
| **viewer** | one handle | read-only admin (writes 403) |
| **non-member / anon** | — | browse storefronts + submit inquiries only |

---

## Creating a store

**Self-serve (new users):** open `/signup`, pick a globally unique **handle**,
an email/password, a first store slug, and a **store type**. The form does the
work for you — it suggests a handle from your email, an address from the store
name, and shows a live `/handle/store` preview. This creates a non-admin `member`
user, provisions `{handle}__{store}` from the matching starter template, and
makes you the `owner` of the handle.

**Quick store (one click):** signed in at `/manage`, hit **Quick store** to
instantly spin up a starter "My Store" at a free address (`store`, `store-2`, …,
retail template) and drop straight into its admin — nothing to fill in. It uses
your namespace automatically (superusers name one), so you can go from account to
editable storefront in a single click.

**From the UI (existing owner/superuser):** sign in at `/manage`, pick a handle
you own, enter a name, choose a type, submit. The store slug is suggested from the
name and validated (`^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$`); handles are validated
the same way and are globally unique.

**From the API** (owner of the handle, or superuser):

```bash
# Named store
curl -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"handle":"acme","slug":"coffee","name":"Acme Coffee","business_type":"restaurant"}' \
  http://localhost:8000/manage/stores

# Quick store (auto address + name)
curl -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"handle":"acme"}' http://localhost:8000/manage/stores/quick
```

Provisioning (`provision_store`) is idempotent and step-logged: validate handle
+ slug → **register the store in `store_registry` with `status: "provisioning"`**
→ `get_scoped_db("{handle}__{store}")` → create indexes → seed the selected
starter template additively → **flip `status` to `"ready"`**. Writing the registry
row *first* means a mid-way failure never leaves orphan `{handle}__{store}_*`
collections unaccounted for — the row stays `provisioning` and a re-run (or the
reconciler) finishes it safely. Re-running never clobbers admin edits.

### Starter templates

`business_type` selects the starter content copied into a new store. The
`template` used is stored on the registry row so a reconcile-driven retry
re-seeds from the same one; a blank or unknown type falls back to the default.

| `business_type` | Template file |
| --- | --- |
| `retail` (default) | [`store_template.json`](store_template.json) |
| `restaurant` | [`store_template.restaurant.json`](store_template.restaurant.json) |

Add a vertical by dropping a `store_template.<key>.json` next to these and
listing the key in `_ALT_TEMPLATE_FILES` / `STORE_TEMPLATES` in
[`main.py`](main.py) — the create-store and signup pickers update automatically.

Editing storefront content is per store: catalog, layout, slideshow, specials,
and inquiries all live under `/{handle}/{store}/admin/*`, and the conversational
**AI editor** (Google Gemini, JSON response mode) edits only the active store.
Team membership lives per handle at `/{handle}/{store}/admin/team` (owner-gated)
— invites are signed JWTs accepted at `/manage/invite`.

---

## Managing the store lifecycle

Every store carries a `status` on its `store_registry` row — the single source
of truth for what routes: `provisioning`, `ready`, `archived`, `failed`, or
`deleting` (a missing status counts as `ready` for older rows). Only `ready`
(and legacy status-less) rows route; everything else 404s. Manage the full
lifecycle from `/manage` or the API (all admin-only, and mirrored by
`make archive-store` / `restore-store` / `delete-store` / `reconcile`):

| Action | Endpoint | Effect |
| --- | --- | --- |
| Rename | `PATCH /manage/stores/{handle}/{store}` `{name}` | Changes the display name on the registry and the store's `stores` singleton. The address is immutable (changing it would break bookmarks and canonical URLs). |
| Archive | `POST /manage/stores/{handle}/{store}/archive` | Suspends the store: it stops routing (404s) but all data is kept. |
| Restore | `POST /manage/stores/{handle}/{store}/restore` | Re-enables an archived or failed store. |
| Delete | `DELETE /manage/stores/{handle}/{store}` `{confirm: "<store>"}` | **Irreversible** deprovision: drops every `{handle}__{store}_*` collection and removes the registry row; if it was the handle's last store, the handle's `namespace_members` are cleaned up too. Requires the confirm token to equal the store slug. |
| Reconcile | `POST /manage/reconcile` `{drop_orphans?: bool}` | Finishes stuck `provisioning` rows, completes deletes stranded in `deleting`, and reports (optionally drops) orphaned `{handle}__{store}_*` collections that have no registry row. Superuser only. |
| Export | `GET /manage/stores/{handle}/{store}/export` | Dumps every `{handle}__{store}_*` collection as MongoDB Extended JSON (`bson.json_util`, so ObjectIds/dates round-trip). The portable backup for one store. |
| Import | `POST /manage/stores/{handle}/{store}/import` `?overwrite=1` | Restores an export dump. Auto-provisions the target if missing (its scope's `app_id` is re-homed on the way in); refuses a non-empty existing target unless `?overwrite=1`. |

Rename, archive, restore, delete, export, and import are gated to the
**namespace owner** (or the superuser); reconcile is **superuser-only**.
Export/import (mirrored by `make export-store` / `import-store`) is the
low-risk, blast-radius mitigation for the shared-DB model — a per-store backup
and restore without a per-tenant-database rearchitecture. Every lifecycle and team action
is recorded to the platform `audit_log` (event, handle, store, actor,
timestamp) — the trail for a deleted store survives, since it lives in the
platform scope, not the dropped `{handle}__{store}_*` collections. The same
reconcile pass runs **best-effort on startup**, so a store left mid-provision or
mid-delete by a crash self-heals on the next boot (guarded by
`PROVISION_STUCK_MINUTES`, so in-flight work on a peer worker is never touched).

The `KNOWN_STORES` routing cache stays correct across workers automatically: the
acting worker refreshes immediately, and every other worker converges via a
`store_registry` **change stream** (near-instant on a replica set / Atlas) with a
short **TTL refresh** (`STORE_CACHE_TTL_SECONDS`, default 30s) as a backstop. So
an archive or delete on one replica stops routing everywhere within seconds — no
redeploy, no restart. (Storefront pages may linger in a CDN for up to the 60s
public cache; see [`SCALE.md`](SCALE.md).)

---

## Configuration

Everything is environment-driven; see [`.env.example`](.env.example). Key vars:

| Variable | Purpose |
| --- | --- |
| `MDB_DB_NAME` | The one database that holds every store's `{handle}__{store}_*` collections |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | The platform superuser, seeded on first boot (access to every namespace) |
| `MDB_ENGINE_MASTER_KEY` | Encrypts app secrets at rest (base64 32-byte key) |
| `MDB_JWT_SECRET` | Signs session JWTs **and** namespace invite tokens (rotating it logs users out + invalidates open invites) |
| `INVITE_TTL_SECONDS` | Lifetime of a team invite token (default 7 days) |
| `MAX_STORES_PER_HANDLE` | Optional per-handle store cap to blunt signup abuse (`0` = off) |
| `MAX_ITEMS_PER_STORE` / `MAX_SECTIONS_PER_STORE` / `MAX_UPLOADS_PER_STORE` | Optional per-store content caps (`0` = unlimited). Enforced on CRUD writes, AI-driven creates, and uploads; over-cap returns `409`. Console shows usage badges |
| `G_NOME_ENV` | Set to `production` behind TLS so the session cookie is `Secure` (see [`SCALE.md`](SCALE.md#session-cookies--csrf)) |
| `CLOUDINARY_*` | Image/video uploads (routes return 503 if unset) |
| `GEMINI_API_KEY` | Enables the AI editor (Gemini); unset → chat routes return 503 |
| `GEMINI_MODEL` / `GEMINI_TEMPERATURE` | AI editor model (default `gemini-3.5-flash`) and sampling temperature |
| `GEMINI_FALLBACK_MODEL` / `GEMINI_MAX_RETRIES` / `GEMINI_RETRY_BACKOFF` | Resilience: fall back once on a `404`, and retry `429`/`5xx`/transport errors with exponential backoff (defaults `gemini-2.5-flash` / `2` / `0.5s`) |
| `RESEND_API_KEY` / `RESEND_FROM` | Lead-notification email (unset → notifications off) |
| `NOTIFY_ENABLED` | Global lead-notification kill switch (default `true`) |
| `INQUIRY_RATELIMIT_PER_MIN` / `_PER_HOUR` | Public inquiry throttle, per IP+store (default 5 / 30) |
| `SIGNUP_RATELIMIT_PER_MIN` / `_PER_HOUR` | Public signup throttle, per IP (default 5 / 20) |
| `AI_RATELIMIT_PER_MIN` / `UPLOAD_RATELIMIT_PER_MIN` | Per-user AI + upload throttles (default 20 / 30) |
| `STORE_CACHE_TTL_SECONDS` | Backstop interval for refreshing the `KNOWN_STORES` routing cache across workers (default 30) |
| `PROVISION_STUCK_MINUTES` | How long a `provisioning` store may sit before the reconciler retries or fails it (default 10) |

Generate secrets with `make secrets`. See [`SCALE.md`](SCALE.md) for the
production checklist, scaling model, and honest limits.

### Observability

Every request is stamped with an `X-Request-ID` (honoured from an upstream proxy
if present, echoed on the response) and gets one structured access-log line, so
a client error can be correlated to a server log. Three probes back the ops
story:

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `GET /healthz` | public | Liveness — process is up, touches nothing. Always `200`. |
| `GET /readyz` | public | Readiness — pings Mongo and reports `{mongo, stores, ai_configured}`. `200` healthy / `503` if Mongo is unreachable, so a load balancer drains the replica. |
| `GET /manage/status` | superuser | Platform metrics snapshot: store counts by status, namespaces, members, configured quotas, and the AI model in use. |

The container healthcheck uses `/healthz`; point your orchestrator's readiness
probe at `/readyz`. The AI editor also logs a clear one-line summary at boot
(model + fallback when configured, or a warning when disabled).

---

## Leads, notifications & abuse protection

The public inquiry endpoint (`POST /{handle}/{store}/api/submit-inquiry`) is the
only unauthenticated write path, so it is hardened three ways:

- **Rate limiting.** A Mongo-backed sliding-window limiter (the engine's own,
  so no Redis/extra infra) throttles submissions **per IP + per store** — one
  store's abusers never affect another's. AI-editor and upload routes are
  throttled **per admin**. Over-limit requests get `429` + `Retry-After`. Tune
  via the env vars above.
- **Honeypot.** Every contact form carries a hidden `company_website` field.
  Bots that autofill it receive a convincing `201` while the submission is
  dropped server-side (no insert, no notification).
- **Lead notifications.** On a real inquiry, the store owner is emailed via
  [Resend](https://resend.com) as a best-effort background task — it never
  blocks or fails the visitor's `201`. Configure `RESEND_API_KEY` + `RESEND_FROM`
  once; each store toggles it (and can override the recipient) from its admin
  dashboard. Recipient falls back to the store's contact email.

> Caveat: `X-Forwarded-For` is only trustworthy behind a proxy/load balancer you
> control. Behind an untrusted network, clients can spoof it to dodge per-IP
> limits.

Admin auth rides a `HttpOnly` + `SameSite=Lax` session cookie, which is what
guards the authenticated `/admin/*` and `/manage/*` writes against cross-site
requests (CSRF) — set `G_NOME_ENV=production` behind TLS so it's also `Secure`.
See [Session cookies & CSRF](SCALE.md#session-cookies--csrf) for the full stance.

---

## Testing

An in-process suite drives the real ASGI app (engine lifespan +
`_bootstrap_stores`) against a live MongoDB Atlas Local — the same path
production uses — and asserts the guarantees that matter for a multi-tenant
system: **cross-tenant isolation**, auth gating, slug validation, the full store
lifecycle, and the session-cookie CSRF posture.

```bash
cd ai-stores
docker compose up -d mongo   # a Mongo on :27017 (or use your own)
make test-deps               # pip install -r requirements-dev.txt
make test                    # runs pytest against ai_stores_test_* (dropped after)
```

Tests use a throwaway database and leave `MDB_ENGINE_MASTER_KEY` unset (secrets
manager off). On every push/PR, CI runs this suite **plus** a `smoke` job that
builds the Docker image and boots it against Atlas Local, **plus** an `e2e` job
that drives real-browser flows (signup + Quick store) with Playwright — catching
image-level and front-end breakage in-process tests can't see.
See [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

A separate, opt-in browser suite lives in `tests/e2e` (excluded from the fast
run). To run it locally:

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
pytest tests/e2e        # signup + Quick store; the AI-chat flow runs only if GEMINI_API_KEY is set
```

---

## Make targets

```
make init             Create .env with generated secrets
make up               Build + run (foreground)
make up-d             Build + run (detached)
make provision-store  Create a store: HANDLE=acme STORE=coffee NAME="Acme Coffee"
make archive-store    Suspend a store (keeps data): HANDLE=acme STORE=coffee
make restore-store    Re-enable an archived/failed store: HANDLE=acme STORE=coffee
make delete-store     Permanently deprovision a store: HANDLE=acme STORE=coffee
make export-store     Back up a store to JSON: HANDLE=acme STORE=coffee [FILE=out.json]
make import-store     Restore a store: HANDLE=acme STORE=coffee FILE=out.json [OVERWRITE=1]
make reconcile        Finish stuck provisions/deletes, report orphans (DROP_ORPHANS=1 to drop)
make secrets          Print freshly generated secrets
make test-deps        Install test deps (pytest, pytest-asyncio, asgi-lifespan)
make test             Run the test suite (needs a local Mongo on :27017)
make logs / ps / down / clean
```

---

## Layout

```
main.py               Multi-tenant runtime, namespace routing, provisioning, endpoints
rbac.py               Per-namespace roles + effective-role mapping (namespace_members)
ai_editor.py          Conversational store-editor: Gemini JSON mode → validate → apply
notifications.py      Best-effort Resend email on new inquiries
manifest.json         Domain: collections, auth, SSR routes, indexes (platform)
store_template.json   Default (retail) starter content copied into a new store
store_template.*.json Per-business-type starter templates (e.g. restaurant)
templates/            SSR templates (base_path-aware links) incl. signup, invite, team, landing
static/               CSS/JS, PWA icons, service worker
tests/                In-process pytest suite (isolation, auth, slug, lifecycle, namespaces, rbac, signup, security)
docker-compose.yml    App + MongoDB Atlas Local (AI editor calls the Gemini API)
../.github/workflows/ CI: in-process suite + Docker build-and-boot smoke on push/PR
```

More on the engine and design philosophy: [`blog.md`](blog.md). Scaling and
operations: [`SCALE.md`](SCALE.md). Where it's headed: [`ROADMAP.md`](ROADMAP.md).
