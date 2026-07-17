# Scaling AI Stores

This system makes one promise about scale, and it keeps it by making a store
cheap enough to be an afterthought:

> **One app instance serves many stores. Adding a store is a runtime action, not
> a deploy.**

A store is not a deployment — it's a *database scope*. The whole operational
model falls out of that one decision.

---

## The decision that pays for everything

AI Stores is **single-instance, multi-tenant**. One process, one Mongo database,
one shared admin, and an unbounded number of stores served at
`stores.com/{store}`. A store is identified by the first path segment and
resolved to a scope on every request.

The engine (`mdb-engine`) already isolates data by scope: it prefixes every
collection with the scope slug (`acme_items`, `bettershop_items`) and tags every
document with an `app_id`. So a store is just a string, and isolation is
structural — not a `WHERE tenant_id = ?` you have to remember to add.

- **No redeploy to add a store.** The shared admin opens `/manage`, types a name,
  and the store exists — seeded, indexed, and live — in one request. See
  `provision_store` in [main.py](main.py).
- **One thing to operate.** One image, one process (or N identical replicas), one
  database to back up. The fleet does not grow when the store count grows.
- **The unit of scale is traffic, not tenancy.** You scale the *instance* to
  serve more visitors; you do not stand up infrastructure per store.

The honest tradeoff: stores now **share a blast radius**. One process and one
database serve everyone, so isolation is logical (scope prefixes) rather than
physical (separate deployments). That is the right trade for a shared-admin
platform where one operator runs every store; it is the wrong trade if each
store must be owned, secured, and billed by a different party (see *Honest
limits*).

---

## How a request becomes a store

```mermaid
flowchart TD
  req["GET stores.com/acme/admin/items"] --> mw{StoreScopeMiddleware}
  mw -->|"reserved 1st segment (static, auth, manage, __mdb…)"| plat["platform scope (no prefix)"]
  mw -->|"known store 'acme'"| scoped["root_path=/acme · scope[store_slug]=acme"]
  mw -->|"unknown segment"| nf["404 — store not found"]
  scoped --> auth["engine auth middleware (one global admin pool)"]
  auth --> route["router matches get_route_path = /admin/items"]
  route --> dep["Depends(get_scoped_db) OVERRIDE → get_scoped_db('acme')"]
  dep --> data["reads/writes acme_items (app_id=acme)"]
```

Three moving parts, all in [main.py](main.py):

1. **`StoreScopeMiddleware`** (outermost, added after `quickstart`) reads the
   first path segment. For a known store it sets `scope["store_slug"]` and
   `root_path="/acme"`. Starlette routes on the *remaining* path
   (`get_route_path` = full path minus `root_path`), so every existing engine
   route matches unchanged — while `request.url` / `base_url` keep the `/acme`
   prefix so canonical URLs, sitemaps, and OG tags stay per-store correct.
2. **`get_scoped_db` override** re-scopes the single dependency every data path
   already uses (SSR pages, auto-CRUD `/api/*`, feeds, and the custom endpoints).
   Store requests get their slug; platform pages get the platform scope.
3. **Auth stays global.** The engine's session middleware always resolves the
   user against the platform scope and only sets `request.state.user`; admin
   protection is enforced by SSR `auth` flags and `require_user` deps, which are
   path-agnostic. One admin pool, one cookie, valid on every store.

### Scopes and reserved paths

- **Platform scope** (`= manifest slug`) holds cross-store data: the single
  global admin `users` pool and the `store_registry`.
- **Store scope** (one per slug) holds that store's `stores` singleton, `items`,
  `sections`, `specials`, `slideshow`, and `inquiries`.
- **Reserved first segments** are never a store: `static`, `__mdb`, `health`,
  `favicon.ico`, `robots.txt`, `auth`, `manage`. Slugs are validated
  (`^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$`) and a banned-list prevents collisions
  with `admin`, `api`, `item`, `contact`, `sitemap.xml`, etc.
- **`/manage`** is the admin-gated console (platform scope). Bare `/` redirects
  to the first store, or to `/manage` when no store exists yet.

---

## Provisioning a store (the whole flow)

`provision_store(engine, slug, name)` is idempotent and step-logged, so a
partial failure is safe to retry:

1. Validate + normalise the slug (`_validate_slug`).
2. **Upsert `store_registry` with `status: "provisioning"`** — the store is
   observable *before* its collections exist. If seeding then fails mid-way the
   row stays `provisioning` (never an orphan set of `{slug}_*` collections with
   no registry row), and a re-run or the reconciler finishes it safely.
3. `get_scoped_db(slug)` — brand-new scopes need no secret, so no token.
4. `_ensure_store_indexes` — creates the manifest's `managed_indexes` (including
   the unique constraints on `stores.slug_id`, `sections.key` and
   `items.item_code`) per scope via the engine's **public**
   `run_index_creation_for_collection` against `{slug}_*` collections. No
   private engine internals, so it stays stable across engine upgrades. The
   per-doc `app_id` index auto-ensures on first access.
5. Seed `store_template.json` additively (by stable key), overriding
   `name`/`slug_id`. Re-running never clobbers admin edits.
6. **Flip `status` to `"ready"`** and warm the in-memory `KNOWN_STORES` set.

At startup, `_bootstrap_stores` ensures the registry's indexes (unique `slug` +
`status`), provisions a `demo` store when the platform has *no* registry rows at
all, then rebuilds `KNOWN_STORES` from `ready` rows.

### Reconciliation

`reconcile_stores(engine, drop_orphans=False)` — run **best-effort on every boot**
(right after bootstrap) and on demand via `POST /manage/reconcile` — repairs drift
in three passes, each guarded by `PROVISION_STUCK_MINUTES` (default 10) so an
in-flight operation on a peer worker is never disturbed:

- **Stuck provisioning.** Rows left `provisioning` past the cutoff are retried
  once via `provision_store`; if the retry fails they are marked `failed` for an
  operator to act on.
- **Stranded deleting.** Rows left `deleting` past the cutoff (a delete that
  crashed mid-drop) have their `{slug}_*` collections re-dropped and the registry
  row removed, finishing the deprovision.
- **Orphan collections.** `{slug}_*` collections whose slug owns a
  `{slug}_stores` singleton but has no registry row (a crash before the first
  registry write, or a delete that lost its row before dropping) are reported,
  and dropped when `drop_orphans` is set.

### Deprovisioning

`DELETE /manage/stores/{slug}` marks the row `deleting`, drops every physical
`{slug}_*` collection directly via the raw Motor database
(`_drop_store_collections`), removes the registry row, and refreshes the cache.
The trailing underscore in the `{slug}_` prefix makes this exact: validated slugs
never contain `_`, so `acme_` matches `acme_items` but never `acme2_items` or the
platform's own `{slug}_*` collections. The `{slug}_stores` singleton is dropped
**last** on purpose: it is the marker the orphan scan keys on, so a crash mid-drop
leaves the store detectable (and re-cleanable) rather than a silent leftover — the
`deleting` reconcile pass then finishes it. This is intentionally a hard delete
(bypassing the engine's trash/reconciler); **archive** is the reversible option.

### Audit trail

Every lifecycle action (create / rename / archive / restore / delete) and every
reconcile-driven repair writes a best-effort entry to the **platform** `audit_log`
— `{event, slug, actor, timestamp, …}`, `actor` being the acting admin's email
(or `system` for reconcile). It lives in the platform scope, so a deleted store's
trail survives the drop of its `{slug}_*` collections. Audit writes never raise:
they can never fail the operation they record.

---

## Scaling the single instance

The most important sentence in this file:

> **The app is stateless. The database is the state.**

The process keeps nothing durable on disk. Sessions are JWTs signed with
`MDB_JWT_SECRET` and carried in a cookie; all real state lives in MongoDB. That
one fact unlocks everything below.

### Run more replicas, not bigger processes

Because the app is stateless, run *K* identical replicas behind a load balancer
and the read path scales horizontally. Do **not** scale by cranking in-process
workers — `python main.py` intentionally runs a single uvicorn process (see the
`__main__` block in [main.py](main.py)). Containers are cattle; add more.

> Multi-process/multi-replica note: `KNOWN_STORES` is an in-memory cache, kept
> correct across workers three ways. (1) The acting worker refreshes it
> immediately after any lifecycle change. (2) Every worker runs a
> `store_registry` **change-stream watcher** (`_watch_registry_changes`) that
> refreshes on any insert/update/delete — near-instant on a replica set / Atlas.
> The watcher tracks the stream's **resume token** and resumes after it across
> transient reconnects, so changes that land during a brief drop aren't missed.
> (3) A **TTL backstop** (`STORE_CACHE_TTL_SECONDS`, default 30s) refreshes on a
> timer, covering standalone Mongo (no change streams) and any dropped stream.
> On a cache miss the middleware still falls back to a status-aware
> `store_registry` lookup (`_store_is_registered`) before returning a 404, so it
> is correct even before the next refresh. Only `ready` (or legacy status-less)
> stores route — archived/deleted stores stop routing everywhere within seconds.

### Let the cache do the work

A storefront is ~99% anonymous reads of slowly-changing data. The SSR routes
already declare it:

```json
"cache": { "ttl": "60s", "scope": "public" }
```

Put a CDN or caching reverse proxy in front and most storefront hits never reach
Python or Mongo. Cache keys include the full path, so `/acme/` and
`/bettershop/` cache independently. The cost: an admin edit can take up to 60s to
appear publicly — a feature that shields your origin, but know it's there.

### Media never touches your app

Images and videos upload to **Cloudinary** and serve from its CDN with on-the-fly
transforms (see the upload routes in [main.py](main.py)). Your container ships
HTML, not megabytes — which is why one small replica can serve many rich stores.

### The local AI runtime (optional, admin-only)

The conversational editor's **Ollama** service is the heaviest component per
compute but the easiest to reason about: it's admin-only and off the storefront
hot path. Zero visitors touch it, so it scales with the number of admins editing
at once (≈ a handful), not with traffic.

- **Keep it as its own tier.** Don't co-locate the model with web replicas — give
  it its own box (GPU for snappy larger models) and keep the app replicas tiny.
- **One runtime for the whole platform.** Every store shares one
  `OLLAMA_BASE_URL`, or you target a hosted OpenAI-compatible endpoint. The app
  degrades gracefully (a clear 503) when the model is absent, so AI is switchable
  per environment, not a hard dependency.

Its only state is the `ollama-models` volume (downloaded weights) — cache-like
and reproducible with `make ai-pull`.

### The one stateful tier

MongoDB is the only thing holding state, so it's the only thing you treat with
care. Past local dev, replace **Atlas Local** with a managed replica set
(**MongoDB Atlas**) by changing one variable:

```bash
MONGODB_URI="mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true&w=majority"
```

Every store lives in the one database `MDB_DB_NAME` as `{slug}_*` collections.
Reads scale onto secondaries; back up continuously. Size the cluster's connection
limit against `replicas x pool_size`, and watch total collection count as the
store count climbs (see *Honest limits*).

---

## Know where the state lives

You can only protect what you can name. The durable state is *exactly*:

1. The **one MongoDB database** (`MDB_DB_NAME`) holding every store's `{slug}_*`
   collections plus the platform `users` and `store_registry`.
2. The instance **secrets** — `MDB_ENGINE_MASTER_KEY`, `MDB_JWT_SECRET`,
   Cloudinary creds. These are platform-wide, not per store.
3. **Media** in Cloudinary (a separate durability domain with its own backups).

Everything else — image, templates, manifest, `store_template.json` — is
reproducible from this repo.

Consequences:

- **Backup = the whole database.** Atlas continuous backup, or `mongodump` of
  `MDB_DB_NAME`. To restore or export a single store, scope to its `{slug}_*`
  collections.
- **`MDB_JWT_SECRET` rotation logs out every admin session** (there's one pool).
- **`MDB_ENGINE_MASTER_KEY` encrypts secrets at rest** — back it up out-of-band.

---

## Honest limits — where the model creaks

A masterclass tells you where the walls are.

- **Shared admin, by design.** There is one global `users` pool and one flat
  `admin` role, so every admin can manage every store. This is a
  *one-operator, many-stores* platform, not a SaaS where each store owner logs
  into only their own store. Per-store owners/roles would require scoping auth
  (today auth is deliberately global) — a different product.
- **Shared blast radius.** One process and one database serve all stores. A
  runaway query, a bad deploy, or a corrupted platform collection can affect
  everyone. Isolation is logical (scope prefixes + `app_id`), not physical. If
  you need hard isolation per store, run separate deployments instead.
- **Collection fan-out.** Each store adds ~6 collections (plus indexes) to the
  one database. Thousands of stores means tens of thousands of collections;
  watch Mongo's per-database limits and index memory. At that scale, sharding or
  splitting the platform across databases becomes the next move.
- **In-memory store cache across workers.** `KNOWN_STORES` is kept in sync by a
  `store_registry` change stream (near-instant on a replica set / Atlas) plus a
  `STORE_CACHE_TTL_SECONDS` backstop, with a status-aware registry fallback on a
  cache miss. On **standalone** Mongo there are no change streams, so cross-worker
  updates lag by up to the TTL (default 30s); the fallback still keeps routing
  correct in the meantime. Run a replica set (Atlas / Atlas Local) for instant
  propagation.
- **No per-store secrets.** New scopes never store secrets (so provisioning needs
  no token). Don't pre-store per-store secrets in the engine's secret manager, or
  `get_scoped_db(slug)` will start requiring a token.
- **Writes aren't cached.** A viral lead spike hits `POST /api/submit-inquiry`
  (uncached, writes Mongo + an `audit_log` hook). It's now throttled per IP+store
  and honeypot-guarded (see below), which blunts spam/DoS — but a legitimate
  spike still hits the DB. Size the DB tier for your worst inbound-lead minute,
  not the average.
- **Abuse protection is per-instance-correct but IP-trust-dependent.** The
  rate limiter is Mongo-backed (correct across replicas), but per-IP keying is
  only as trustworthy as `X-Forwarded-For` — set it from a proxy you control.
- **Lead notifications are best-effort.** Email goes out via Resend on a
  background task after the `201`; a mail outage is logged and swallowed, never
  affecting capture. If guaranteed delivery matters, add a queue/outbox — out of
  scope for v1.
- **PWA/service worker is global.** One service worker and one manifest with
  `scope: "/"` cover the whole origin; the SW only bypasses admin/api/auth/manage
  traffic from caching. Per-store offline installs are out of scope for v1.

---

## Production checklist

- [ ] Managed MongoDB (Atlas) with backups — not Atlas Local.
- [ ] Unique, generated `MDB_ENGINE_MASTER_KEY` and `MDB_JWT_SECRET`
      (`make secrets`), stored in a secrets manager, never committed.
- [ ] `ADMIN_PASSWORD` changed off the default.
- [ ] TLS at the edge (Caddy / Cloudflare / ALB) + HSTS.
- [ ] `G_NOME_ENV=production` set so the admin session cookie is marked `Secure`
      (never sent over plain HTTP). See "Session cookies & CSRF" below.
- [ ] CDN in front of the storefront; the 60s public cache respected.
- [ ] Cloudinary configured (uploads return 503 otherwise).
- [ ] Orchestrator health checks wired to `/__mdb/health/live` (already in the
      Dockerfile and compose).
- [ ] Replicas behind a load balancer; single uvicorn process per container.
- [ ] MongoDB is a replica set (Atlas) so `store_registry` change streams
      propagate lifecycle changes (archive/delete/rename) across replicas
      instantly; otherwise the `STORE_CACHE_TTL_SECONDS` backstop applies.
- [ ] `X-Forwarded-For` set by a trusted proxy so per-IP rate limits are real.
- [ ] Rate limits tuned for your traffic (`INQUIRY_/AI_/UPLOAD_RATELIMIT_*`).
- [ ] Lead notifications wired (`RESEND_API_KEY` + verified `RESEND_FROM`), or
      knowingly left off (`NOTIFY_ENABLED=false`).
- [ ] CI green — the in-process suite (isolation/auth/slug/lifecycle/security)
      plus a `smoke` job that builds and boots the real image both run on every
      push/PR against Atlas Local (`.github/workflows/ci.yml`).
- [ ] Log aggregation (`LOG_LEVEL`, structured lines); alert on Mongo collection
      count and connection saturation as the store count grows.

---

## Session cookies & CSRF

Admin auth is a signed-JWT session cookie (`ais_session_{slug}`), issued by the
engine on `/auth/login`. It is set with `HttpOnly` (JS can't read the token, so
XSS can't exfiltrate it) and `SameSite=Lax`, and `Secure` whenever the request is
HTTPS **or** `G_NOME_ENV=production` — hence the checklist item above.

CSRF protection (`auth.csrf_protection`) is deliberately left **off**. The
reasoning, made explicit so it's a decision rather than an accident:

- **`SameSite=Lax` is the mitigation.** Browsers withhold the session cookie on
  cross-site state-changing requests (POST/PATCH/DELETE from another origin), so
  a classic form-POST CSRF against `/{store}/admin/*`, `/admin/upload-*`, or the
  `/manage/*` lifecycle routes carries no credentials and fails auth.
- **No CORS is configured**, so the browser's same-origin policy also blocks
  credentialed cross-origin `fetch()`. The admin UI's own calls are same-origin
  and send `credentials: "same-origin"`.
- **The one public write, `POST /{store}/api/submit-inquiry`, needs no CSRF** —
  it's intentionally open to anonymous visitors and hardened instead by the
  honeypot + per-IP/store rate limiting (see the README).
- A regression test ([tests/test_security.py](tests/test_security.py)) asserts the
  login cookie stays `HttpOnly` + `SameSite=Lax`, so an engine upgrade can't
  silently weaken this.

**When to turn it on.** If the threat model grows to include untrusted *same-site*
subdomains (which `SameSite=Lax` does not defend against) or you enable CORS, set
`"csrf_protection": true` in [manifest.json](manifest.json). The engine then
enforces a double-submit `X-CSRF-Token`, and every mutating `fetch` in the admin
UI must forward that header — track it as the defense-in-depth upgrade in
[ROADMAP.md](ROADMAP.md).

---

## The one-liner to remember

Scale here isn't a fleet you grow — it's an instance you feed. One process serves
one store or ten thousand; the store count changes a row in `store_registry`, not
your infrastructure. That **sameness** — one image, one database, per-request
scope — *is* the scalability.

See also: the [README](README.md) for the run path and the `/manage` flow, and
[blog.md](blog.md) for how the engine works under the hood.
