# AI Stores — Quickstart

Get a multi-tenant storefront platform running locally in about a minute, then
open your first store. This is the fast path; for the architecture see
[`README.md`](README.md), [`SCALE.md`](SCALE.md), and [`blog.md`](blog.md).

**The model in one line:** one app instance serves many stores at
`stores.com/{handle}/{store}`. A user claims a **handle** (their namespace) and
owns every store under it. The seeded admin is a **platform superuser**;
everyone else signs up and owns only their own handle.

---

## Prerequisites

- **Docker** + Docker Compose (the only requirement for the container path).
- Or, for the local path: **Python 3.11+** and a **MongoDB replica set** on
  `:27017` (change streams need a replica set — Atlas Local is one).

---

## 1. Run it (Docker — recommended)

```bash
cd ai-stores
make init          # writes .env with freshly generated secrets
make up            # build + start app + MongoDB at :8000
```

`make init` generates real `MDB_ENGINE_MASTER_KEY` and `MDB_JWT_SECRET` values.
**Edit `ADMIN_EMAIL` / `ADMIN_PASSWORD` in `.env` before exposing anything** —
that account is the platform superuser.

On first boot with an empty database, a **`demo/shop`** store is provisioned
automatically (owned by the admin).

| URL | What it is |
| --- | --- |
| http://localhost:8000/ | redirects to `/manage` |
| http://localhost:8000/manage | the console — sign in (superuser or member) |
| http://localhost:8000/signup | claim a handle + open your first store |
| http://localhost:8000/demo/ | the `demo` namespace landing (public) |
| http://localhost:8000/demo/shop/ | the demo storefront (public) |
| http://localhost:8000/demo/shop/admin/dashboard | that store's admin |

> **Can't stay logged in over plain HTTP?** Leave `G_NOME_ENV` **blank** for
> local dev. Setting it to `production` marks the session cookie `Secure`, so
> the browser won't send it back over `http://` and login appears to fail.

---

## 2. Create a store

Three ways, depending on who you are:

**Self-serve (new user)** — open `/signup`, pick a globally unique **handle**,
an email + password (min 8 chars), a first store slug, and a **store type**
(`retail` or `restaurant`). The form fills itself in as you type — it suggests a
handle from your email, an address from the store name, and previews your live
`/handle/store` URL. You become the `owner` of that handle, and land in your new
store's admin.

**Quick store (one click)** — signed in at `/manage`, click **Quick store**. It
provisions a starter "My Store" at the next free address (`store`, `store-2`, …)
and opens its admin immediately — zero fields to fill. Fastest path from account
to an editable storefront.

**From the console** — sign in at `/manage`. A superuser can create under any
handle; an owner picks one of their handles, enters a name, chooses a type, and
submits. The store slug is suggested from the name and validated
(`^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$`, no `_`).

**From the CLI** (against a running instance, using the `.env` admin):

```bash
make provision-store HANDLE=acme STORE=coffee NAME="Acme Coffee"
```

**From the API** (owner of the handle, or superuser):

```bash
# Named store
curl -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"handle":"acme","slug":"coffee","name":"Acme Coffee","business_type":"restaurant"}' \
  http://localhost:8000/manage/stores

# Quick store — auto address + name, retail template
curl -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"handle":"acme"}' http://localhost:8000/manage/stores/quick
```

The store is registered, indexed, and seeded from the starter template for its
`business_type` (`retail` default, or `restaurant`) in one request — no redeploy.

---

## 3. Roles & teams

Authorization is **per namespace** (per handle). Identity stays global (one
`users` pool + one session cookie); a middleware overlay maps your handle
membership to what you can do.

| Role | Can |
| --- | --- |
| **platform superuser** (`ADMIN_EMAIL`) | everything, across every namespace |
| **owner** | edit content, create/delete stores, manage the team |
| **editor** | edit store content |
| **viewer** | read-only admin (writes 403) |
| **non-member / anonymous** | browse storefronts + submit inquiries only |

**Public storefronts stay public** — viewing a store and submitting an inquiry
never require membership. Only writes and the `/admin` surface are gated.

**Invite a teammate:** as an owner, open `/{handle}/{store}/admin/team`, send an
invite (owner/editor/viewer). The invitee opens the link (`/manage/invite`),
signs in with the invited email, and accepts. A handle always keeps ≥1 owner.

---

## 4. Customize a store

Everything below is editable from the store's admin, or via the conversational
**AI editor** (the floating chat on admin pages):

- **Appearance** — colors, fonts, radius, style preset.
- **Layout** — reorder/toggle sections (hero, catalog, specials, richtext,
  gallery, contact, cta); each has its own settings.
- **Catalog** — items with categories, price, status, attributes.
- **Content** — tagline, about, hours, contact, socials, slideshow, specials.
- **Analytics** — per-store Google Tag / Meta Pixel IDs.

---

## 5. Optional features

| Feature | Enable by |
| --- | --- |
| **AI editor** (Google Gemini) | set `GEMINI_API_KEY` in `.env` (get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)). Optional: `GEMINI_MODEL` (default `gemini-3.5-flash`), `GEMINI_FALLBACK_MODEL`. Chat routes return 503 until set; transient errors auto-retry and a 404 falls back once. |
| **Image/video uploads** | set `CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` (routes return 503 if unset). |
| **Lead-notification email** | set `RESEND_API_KEY` + a verified `RESEND_FROM`. Per-store toggle lives in the dashboard. |

---

## 6. Run it without Docker (local dev / tests)

```bash
cd ai-stores
python -m pip install -r requirements.txt -r requirements-dev.txt

# a Mongo replica set on :27017 — e.g. Atlas Local in a container:
docker run -d --rm -p 27017:27017 mongodb/mongodb-atlas-local:latest

MONGODB_URI="mongodb://localhost:27017/?directConnection=true" \
ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD=change-me-123 \
MDB_JWT_SECRET="dev-only-jwt-secret-at-least-32-characters" \
python main.py           # serves on :8000
```

Run the test suite (needs the same local Mongo):

```bash
make test-deps
make test                # 80 tests: isolation, auth, namespaces, rbac, signup, lifecycle …
```

Tests run the real ASGI app against a throwaway `ai_stores_test_*` database that
is dropped on teardown.

---

## Environment cheat sheet

Everything is driven by one `.env` file (see [`.env.example`](.env.example)),
consumed automatically by Docker Compose. `MONGODB_URI` is **not** in `.env` —
Compose injects it; you only set it yourself when running outside Compose.

**Required**

| Var | Purpose |
| --- | --- |
| `MDB_DB_NAME` | the one database holding every `{handle}__{store}_*` scope |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | the platform superuser, seeded on first boot |
| `MDB_JWT_SECRET` | signs session cookies **and** team-invite tokens (≥32 chars) |

**Recommended in production**

| Var | Purpose |
| --- | --- |
| `MDB_ENGINE_MASTER_KEY` | base64 32-byte key; encrypts secrets at rest |
| `G_NOME_ENV=production` | marks the session cookie `Secure` (behind TLS only) |

**Tuning (sane defaults if unset)**

| Var | Default | Purpose |
| --- | --- | --- |
| `SIGNUP_RATELIMIT_PER_MIN` / `_PER_HOUR` | 5 / 20 | public signup throttle (per IP) |
| `INQUIRY_RATELIMIT_PER_MIN` / `_PER_HOUR` | 5 / 30 | public inquiry throttle (per IP+store) |
| `AI_RATELIMIT_PER_MIN` / `UPLOAD_RATELIMIT_PER_MIN` | 20 / 30 | per-user AI + upload throttle |
| `INVITE_TTL_SECONDS` | 604800 | team-invite link lifetime (7 days) |
| `MAX_STORES_PER_HANDLE` | 0 | cap stores per handle (0 = unlimited) |
| `MAX_ITEMS_PER_STORE` / `MAX_SECTIONS_PER_STORE` / `MAX_UPLOADS_PER_STORE` | 0 | per-store content caps (0 = unlimited); over-cap → `409` |
| `STORE_CACHE_TTL_SECONDS` | 30 | routing-cache backstop refresh interval |
| `PROVISION_STUCK_MINUTES` | 10 | reconciler retry window for stuck provisions |

**Optional features (off unless set)**

| Var | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | enables the AI editor (Gemini JSON mode); unset → chat routes 503 |
| `GEMINI_MODEL` / `GEMINI_TEMPERATURE` | AI editor model (default `gemini-3.5-flash`) + sampling temperature |
| `CLOUDINARY_*` | image/video uploads |
| `RESEND_API_KEY` / `RESEND_FROM` | lead-notification email |

Generate fresh secrets any time with `make secrets`.

---

## Operations & backups

- **Health probes:** `GET /healthz` (liveness) and `GET /readyz` (pings Mongo —
  `503` if unreachable). Point your orchestrator's readiness check at `/readyz`.
- **Platform snapshot:** `GET /manage/status` (superuser) returns store counts
  by status, namespaces, members, quotas, and the AI model in use.
- **Per-store backup:** `make export-store HANDLE=acme STORE=coffee` writes a
  JSON dump; `make import-store HANDLE=acme STORE=coffee FILE=… [OVERWRITE=1]`
  restores it (auto-provisions the target if it doesn't exist).
- **Request tracing:** every response carries an `X-Request-ID`.

```bash
curl -s localhost:8000/healthz            # {"status":"ok"}
curl -s localhost:8000/readyz             # {"status":"ok","mongo":true,...}
```

---

## Troubleshooting

- **Everything 404s / login fails locally** → make sure Mongo is a running
  **replica set** on `:27017`, and `G_NOME_ENV` is blank for HTTP dev.
- **`/signup` or store create returns 422** → the handle/slug must be 3–40 chars,
  lowercase `a-z0-9-`, no underscore, and not a reserved word (`admin`, `api`,
  `manage`, `signup`, …).
- **AI chat returns 503** → set `GEMINI_API_KEY` in `.env` and restart. If it
  says the model wasn't found, set `GEMINI_MODEL` to an available model.
- **Uploads return 503** → Cloudinary isn't configured.
- **A store won't route after create** → give the cross-worker cache a moment
  (`STORE_CACHE_TTL_SECONDS`), or run `make reconcile`.

---

## Handy commands

```bash
make up-d                                          # start detached
make logs                                          # follow logs
make provision-store HANDLE=acme STORE=coffee NAME="Acme Coffee"
make archive-store  HANDLE=acme STORE=coffee       # suspend (keeps data)
make restore-store  HANDLE=acme STORE=coffee       # re-enable
make delete-store   HANDLE=acme STORE=coffee       # irreversible deprovision
make export-store   HANDLE=acme STORE=coffee       # back up one store to JSON
make import-store   HANDLE=acme STORE=coffee FILE=acme__coffee-export.json
make reconcile                                     # finish stuck provisions/deletes
make down                                          # stop (keeps the mongo volume)
make clean                                         # stop + drop the mongo volume
```
