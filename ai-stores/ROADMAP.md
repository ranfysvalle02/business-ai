# Roadmap

Where AI Stores is headed. The multi-tenant **foundation** is in place — the
work below is what turns "my stores" into "a platform other people use."

Each item names the concrete code seams it slots into, so it stays actionable
rather than aspirational. Nothing here is committed to a date; it's ordered by
what unlocks the most value next.

---

## Shipped (the foundation)

- **Full store lifecycle** — create, rename, archive/restore, and irreversible
  delete from `/manage` or the API, each admin-gated. See
  [`README.md`](README.md#managing-the-store-lifecycle).
- **Provisioning atomicity + reconciler** — `provision_store` registers a
  `provisioning` row *before* creating collections, so a mid-way failure is
  observable and retryable, never an orphan. `reconcile_stores` self-heals stuck
  provisions, stranded deletes, and orphaned collections, and runs best-effort
  on every boot.
- **Cross-worker cache correctness** — `KNOWN_STORES` stays consistent via a
  `store_registry` change-stream watcher (with resume tokens) plus a TTL
  backstop and a status-aware registry fallback on a cache miss.
- **Audit trail** — every lifecycle action and reconcile repair writes to the
  platform `audit_log` (event, slug, actor, timestamp).

See [`SCALE.md`](SCALE.md) for how these behave under multiple workers/replicas.

---

## Next up

### 1. Per-store users & RBAC

**Goal:** move from one global admin to real teams per store.

**Chosen model — layered.** Keep the existing shared admin as a **platform
superuser** (full access to `/manage` and every store), and add **per-store
memberships** on top:

- Roles: `owner` · `editor` · `viewer`, stored per (store, user).
- **Signed invite links** — a store owner invites a teammate by email; the link
  carries a short-lived signed token that binds `{store, role, email}`.
- **Enforcement by effective-role rewrite.** Auth stays global (the engine
  resolves *who* you are, path-agnostic); authorization becomes per-store by
  rewriting the request's *effective* role from the membership for
  `scope["store_slug"]` before the route's `write_roles` check runs. A platform
  superuser always resolves to `owner`-equivalent.

**Seams:**
- New `store_members` collection in the platform scope (or a `members[]` array
  on the `store_registry` row for small teams): `{slug, email, role, invited_by,
  created_at}`.
- Resolve membership right after `StoreScopeMiddleware` sets `store_slug`, and
  overlay the effective role where `get_current_user` / the engine's auth
  dependency is consumed.
- Invite issue/accept endpoints under `/{store}/admin/team`; sign tokens with the
  existing `MDB_JWT_SECRET` (or a dedicated key).
- `/manage` gains a per-store "Team" panel; deletes/renames cascade membership
  cleanup (fold into `_drop_store_collections` / the delete path).

**Guardrails:** membership changes should land in the `audit_log`; a store must
always retain at least one `owner`.

### 2. Custom domains / subdomains

**Goal:** `shop.acme.com → acme` (and `acme.stores.com → acme`) alongside the
existing path routing.

**Seams:**
- Add `domains: [ ... ]` to the `store_registry` row (verified hostnames).
- In `StoreScopeMiddleware`, when the first path segment isn't a known store, fall
  back to a **Host-header → slug** lookup against `domains[]` before 404ing. This
  reuses the same central scope-resolution point, so the rest of the request path
  is unchanged.
- Cache the host→slug map alongside `KNOWN_STORES` and invalidate it on the same
  `store_registry` change stream.
- Domain **verification** (DNS TXT / `CNAME` challenge) + status on the registry
  row; expose add/verify/remove in `/manage`.
- **TLS** is an ops concern: terminate at the proxy/load balancer with on-demand
  certs (e.g. Caddy `on_demand_tls`, or an ACME sidecar). Document in
  [`SCALE.md`](SCALE.md).

**Watch out for:** canonical-URL/SEO (redirect path ↔ custom domain
consistently), cookie scope for auth across hostnames, and the public CDN cache
key now including Host.

### 3. Billing, plans & quotas

**Goal:** turn stores into a business — metered plans with enforced limits.

**Seams:**
- Store the `plan` and usage counters on the `store_registry` row (e.g. `plan`,
  `limits: {items, uploads, members}`, `usage: {...}`).
- **Stripe** integration: checkout + a webhook that flips `plan`/`status` on the
  registry row (the same row the cache and routing already read from).
- **Quota enforcement** at the natural chokepoints:
  - item count → item-create route + the AI editor's `apply_ops`;
  - uploads → the upload route (which already has a per-admin rate limit to hang
    a quota check next to);
  - members → the RBAC invite path (item 1).
- **Feature flags** per plan (e.g. custom domains, AI editor) — read from the
  plan on the registry row; degrade gracefully (`402`/`403` with a clear upgrade
  message) rather than 500.

**Note:** a suspended/past-due store can reuse the existing `archived` status
(stops routing, keeps data) — no new plumbing for the "downgrade to read-only"
case.

---

## Smaller improvements & nice-to-haves

- **Slug rename (address change).** Today only the display name is mutable.
  A true slug change means renaming every `{slug}_*` collection and rewriting the
  per-doc `app_id`, plus a redirect from the old address — worth a dedicated,
  reconciler-backed migration path.
- **Soft-delete / trash window.** A grace period (status `deleting` with a TTL)
  before the hard drop, so an accidental delete is recoverable. The
  `deleting`-recovery reconcile pass is already most of the machinery.
- **Surface the audit log in `/manage`.** A read-only activity feed per store
  (and platform-wide) from the `audit_log` already being written.
- **Per-store data export / import.** Dump a store's `{slug}_*` collections to a
  portable bundle (backup, clone-to-new-store, offboarding).
- **Bulk operations in `/manage`.** Multi-select archive/reconcile for operators
  running many stores.
- **Provisioning progress/observability.** Surface `provisioning` → `ready`
  transitions (and `failed` reasons) in the UI instead of only in logs.
- **CSRF tokens (defense-in-depth).** Auth is currently CSRF-mitigated by the
  `SameSite=Lax` session cookie (see [Session cookies & CSRF](SCALE.md#session-cookies--csrf)).
  Flipping `csrf_protection: true` in [`manifest.json`](manifest.json) adds a
  double-submit `X-CSRF-Token`; the prerequisite is routing every mutating
  `fetch` in the admin UI through one shared helper that injects the header
  (today they're spread across ~12 files). Worth it if untrusted same-site
  subdomains or CORS ever enter the picture.

---

## Non-goals (for now)

- **Cross-store data sharing.** Isolation is a core guarantee; a store never sees
  another's data. Any "shared catalog" feature would be an explicit, opt-in join
  at the platform scope, not a relaxation of scoping.
- **Sharding / multi-database splits.** Single-database, prefix-scoped collections
  are intentional until per-database limits actually bite — see the honest limits
  in [`SCALE.md`](SCALE.md).
- **Replacing the engine's global auth.** RBAC is layered *on top* of it (item 1),
  not a fork of the auth model.
