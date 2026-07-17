# AI Stores

A single app instance that serves **many storefronts** at `stores.com/{store}`,
managed by **one shared admin** — no redeploy to add a store. Built as a thin
shell over [`mdb-engine`](blog.md): the domain (collections, auto-CRUD, auth, SSR
routes, indexes, admin plane) is declared in [`manifest.json`](manifest.json);
[`main.py`](main.py) adds only what makes it multi-tenant.

- **Path-based multi-tenancy.** The first URL segment picks the store; the
  request is scoped to that store's data for its whole lifetime.
- **Dynamic stores.** Create a store from the UI at `/manage` (or the API); it's
  seeded, indexed, and live in one request. Rename, archive/restore, and
  delete/deprovision from the same console — no redeploy.
- **Shared admin.** One login manages every store. Auth is global; only data is
  scoped.
- **Isolated data.** The engine prefixes each store's collections (`acme_items`)
  and tags docs with an `app_id`, so stores can't see each other.

---

## Quick start

```bash
cd ai-stores
make init        # create .env with generated secrets (edit ADMIN_* first)
make up          # build + run at http://localhost:8000
make ai-pull     # (optional) pull the local model for the AI editor
```

On first boot with an empty database, a **`demo`** store is provisioned
automatically. Open:

- `http://localhost:8000/` → redirects to a store (or `/manage` if none exist)
- `http://localhost:8000/manage` → sign in as the shared admin, create stores
- `http://localhost:8000/demo/` → the demo storefront
- `http://localhost:8000/demo/admin/dashboard` → that store's admin

Default admin credentials come from `.env` (`ADMIN_EMAIL` / `ADMIN_PASSWORD`) —
change them before exposing anything.

---

## How it works

```
GET /acme/admin/items
      │
      ▼
StoreScopeMiddleware ── first segment "acme" is a known store
      │  sets scope[store_slug]=acme, root_path=/acme
      ▼
engine auth middleware ── resolves the ONE global admin (path-agnostic)
      │
      ▼
router ── matches /admin/items (root_path stripped)
      │
      ▼
Depends(get_scoped_db) override ── returns get_scoped_db("acme")
      │
      ▼
reads/writes acme_items  (app_id = acme)
```

Three pieces in [`main.py`](main.py):

1. **`StoreScopeMiddleware`** resolves `/{store}/...` to a per-request scope by
   setting `root_path`. Existing engine routes match on the stripped path, while
   `request.url`/`base_url` keep the prefix so canonical URLs, sitemaps, and OG
   tags stay per-store correct.
2. **`get_scoped_db` override** re-scopes the one dependency every data path uses
   (SSR pages, auto-CRUD `/api/*`, feeds, custom endpoints).
3. **Auth stays global** — one `users` pool in the platform scope, one cookie,
   valid on every store.

### Scopes

- **Platform scope** (`= manifest slug`): global admin `users`, `store_registry`.
- **Store scope** (per slug): `stores` singleton, `items`, `sections`,
  `specials`, `slideshow`, `inquiries`.
- **Reserved first segments** (never a store): `static`, `__mdb`, `health`,
  `favicon.ico`, `robots.txt`, `auth`, `manage`.

---

## Creating a store

**From the UI:** sign in at `/manage`, enter a name, submit. The slug is
suggested from the name and validated (`^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$`).

**From the CLI** (against a running instance, using `.env` admin creds):

```bash
make provision-store SLUG=acme NAME="Acme Co"
```

**From the API** (admin session required):

```bash
curl -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"slug":"acme","name":"Acme Co"}' \
  http://localhost:8000/manage/stores
```

Provisioning (`provision_store`) is idempotent and step-logged: validate slug →
**register the store in `store_registry` with `status: "provisioning"`** →
`get_scoped_db(slug)` → create indexes → seed [`store_template.json`](store_template.json)
additively → **flip `status` to `"ready"`**. Writing the registry row *first*
means a mid-way failure never leaves orphan `{slug}_*` collections unaccounted
for — the row stays `provisioning` and a re-run (or the reconciler) finishes it
safely. Re-running never clobbers admin edits.

Editing storefront content is per store: catalog, layout, slideshow, specials,
and inquiries all live under `/{store}/admin/*`, and the conversational **AI
editor** (local Ollama) edits only the active store.

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
| Rename | `PATCH /manage/stores/{slug}` `{name}` | Changes the display name on the registry and the store's `stores` singleton. The address/slug is immutable (changing it would break bookmarks and canonical URLs). |
| Archive | `POST /manage/stores/{slug}/archive` | Suspends the store: it stops routing (404s) but all data is kept. |
| Restore | `POST /manage/stores/{slug}/restore` | Re-enables an archived or failed store. |
| Delete | `DELETE /manage/stores/{slug}` `{confirm: "<slug>"}` | **Irreversible** deprovision: drops every `{slug}_*` collection and removes the registry row. Requires the confirm token to equal the slug. |
| Reconcile | `POST /manage/reconcile` `{drop_orphans?: bool}` | Finishes stuck `provisioning` rows, completes deletes stranded in `deleting`, and reports (optionally drops) orphaned `{slug}_*` collections that have no registry row. |

Every lifecycle action is recorded to the platform `audit_log` (event, slug,
acting admin, timestamp) — the trail for a deleted store survives, since it
lives in the platform scope, not the dropped `{slug}_*` collections. The same
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
| `MDB_DB_NAME` | The one database that holds every store's `{slug}_*` collections |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | The single shared admin, seeded on first boot |
| `MDB_ENGINE_MASTER_KEY` | Encrypts app secrets at rest (base64 32-byte key) |
| `MDB_JWT_SECRET` | Signs session JWTs (rotating it logs the admin out) |
| `G_NOME_ENV` | Set to `production` behind TLS so the admin session cookie is `Secure` (see [`SCALE.md`](SCALE.md#session-cookies--csrf)) |
| `CLOUDINARY_*` | Image/video uploads (routes return 503 if unset) |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | Local AI editor (degrades to 503 if absent) |
| `RESEND_API_KEY` / `RESEND_FROM` | Lead-notification email (unset → notifications off) |
| `NOTIFY_ENABLED` | Global lead-notification kill switch (default `true`) |
| `INQUIRY_RATELIMIT_PER_MIN` / `_PER_HOUR` | Public inquiry throttle, per IP+store (default 5 / 30) |
| `AI_RATELIMIT_PER_MIN` / `UPLOAD_RATELIMIT_PER_MIN` | Per-admin AI + upload throttles (default 20 / 30) |
| `STORE_CACHE_TTL_SECONDS` | Backstop interval for refreshing the `KNOWN_STORES` routing cache across workers (default 30) |
| `PROVISION_STUCK_MINUTES` | How long a `provisioning` store may sit before the reconciler retries or fails it (default 10) |

Generate secrets with `make secrets`. See [`SCALE.md`](SCALE.md) for the
production checklist, scaling model, and honest limits.

---

## Leads, notifications & abuse protection

The public inquiry endpoint (`POST /{store}/api/submit-inquiry`) is the only
unauthenticated write path, so it is hardened three ways:

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
builds the Docker image and boots it against Atlas Local — catching image-level
breakage (e.g. a module missing from the build) that in-process tests can't see.
See [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

---

## Make targets

```
make init             Create .env with generated secrets
make up               Build + run (foreground)
make up-d             Build + run (detached)
make provision-store  Create a store: SLUG=acme NAME="Acme Co"
make archive-store    Suspend a store (keeps data): SLUG=acme
make restore-store    Re-enable an archived/failed store: SLUG=acme
make delete-store     Permanently deprovision a store: SLUG=acme
make reconcile        Finish stuck provisions/deletes, report orphans (DROP_ORPHANS=1 to drop)
make secrets          Print freshly generated secrets
make ai-pull          Pull the Ollama model for the AI editor
make test-deps        Install test deps (pytest, pytest-asyncio, asgi-lifespan)
make test             Run the test suite (needs a local Mongo on :27017)
make logs / ps / down / clean
```

---

## Layout

```
main.py               Multi-tenant runtime, provisioning, custom endpoints
ai_editor.py          Conversational store-editor (propose → validate → apply)
notifications.py      Best-effort Resend email on new inquiries
manifest.json         Domain: collections, auth, SSR routes, indexes (platform)
store_template.json   Default content copied into every new store scope
templates/            SSR templates (base_path-aware links)
static/               CSS/JS, PWA icons, service worker
tests/                In-process pytest suite (isolation, auth, slug, lifecycle, security)
docker-compose.yml    App + MongoDB Atlas Local + Ollama
../.github/workflows/ CI: in-process suite + Docker build-and-boot smoke on push/PR
```

More on the engine and design philosophy: [`blog.md`](blog.md). Scaling and
operations: [`SCALE.md`](SCALE.md). Where it's headed: [`ROADMAP.md`](ROADMAP.md).
