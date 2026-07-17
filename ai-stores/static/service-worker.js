// AI Stores — Progressive Web App service worker.
// Cache-first for static assets, network-first for navigations with an
// offline fallback to the cached home shell.
const CACHE_NAME = 'ai-stores-pwa-v2';
const PRECACHE = [
  '/static/css/app.css',
  '/static/js/reveal.js',
  '/static/js/navigation.js',
  '/static/js/microfx.js',
  '/static/img/logo.png',
  '/static/icons/icon-192x192.png',
  '/static/icons/icon-512x512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE))
      .catch(() => { /* precache best-effort */ })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  if (!req.url.startsWith(self.location.origin)) return;

  // Never cache admin, API, auth, platform-management, or engine traffic —
  // always go to network. Matches both "/admin/..." and store-prefixed
  // "/{store}/admin/..." paths (store slugs are [a-z0-9-]).
  const url = new URL(req.url);
  if (/^\/([a-z0-9-]+\/)?(admin|api|auth|__mdb|manage)(\/|$)/.test(url.pathname)) {
    return;
  }

  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req).then((res) => {
        if (res && res.status === 200 && res.type === 'basic') {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
        }
        return res;
      }).catch(() => cached || caches.match('/'));
      return cached || network;
    })
  );
});

// ── Push notifications (admin lead alerts) ─────────────────────────────
self.addEventListener('push', (event) => {
  let data = {
    title: 'New inquiry',
    body: 'You have a new unread inquiry',
    icon: '/static/icons/icon-192x192.png',
    badge: '/static/icons/icon-192x192.png',
    tag: 'new-inquiry',
    data: { url: '/admin/inquiries' },
  };
  if (event.data) {
    try {
      const payload = event.data.json();
      data = { ...data, title: payload.title || data.title, body: payload.body || data.body, data: payload.data || data.data };
    } catch (_) { /* keep defaults */ }
  }
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: data.icon,
      badge: data.badge,
      tag: data.tag,
      data: data.data,
      vibrate: [200, 100, 200],
      actions: [
        { action: 'view', title: 'View inquiries' },
        { action: 'close', title: 'Close' },
      ],
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  if (event.action === 'close') return;
  const target = (event.notification.data && event.notification.data.url) || '/admin/inquiries';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if (client.url.endsWith(target) && 'focus' in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(target);
    })
  );
});
