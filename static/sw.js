// fiT-X service worker — static caching + offline fallback.
//
// Live data is never cached: page navigations are network-first (the app
// always shows fresh data when online) and fall back to an offline page
// when the network is gone. Only the app shell (/static/*) and known
// font/chart CDNs use cache-with-refresh, so updates land on the next
// load. Bump VERSION whenever shipped static assets change.
const VERSION = "fitx-v5";  // bumped: in-app navigations never answered from a stale cached page

const PRECACHE = [
  "/static/offline.html",
  "/static/manifest.json",
  "/static/app-install.js",
  "/static/favicon.ico",
  "/static/favicon-32x32.png",
  "/static/favicon-16x16.png",
  "/static/apple-touch-icon.png",
  "/static/android-chrome-192x192.png",
  "/static/android-chrome-512x512.png",
  "/static/branding/runner.png",
  "/static/branding/logo_transparent.png",
];

// Successful navigations live in their own runtime cache so a slow or
// cold-started launch can paint the user's last dashboard immediately.
const NAV_CACHE = VERSION + "-nav";
// How long a navigation may hold the screen blank before we show the cached
// page (while the fresh response keeps loading in the background).
const NAV_TIMEOUT_MS = 2500;

// CDNs the app shell depends on; responses from them are opaque but
// safe to store and serve.
const CACHEABLE_CDN_HOSTS = new Set([
  "fonts.googleapis.com",
  "fonts.gstatic.com",
  "cdn.jsdelivr.net",
]);

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(VERSION)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  // App navigations: network-first with a timeout — a cold-started server
  // (Render free tier sleeps) or a weak network used to hold the launch
  // screen blank for the whole wait. If we have the user's last copy of this
  // page, paint it after the timeout; the fresh response still lands in the
  // background and is stored for the next launch.
  if (request.mode === "navigate") {
    event.respondWith(
      (async () => {
        const navCache = await caches.open(NAV_CACHE);
        const network = fetch(request);
        // In-app navigation (a form submit's redirect, a link tap): the user
        // is already inside the app, so the page MUST reflect what they just
        // did. Answering from the cache after the timeout showed the
        // dashboard from before their coach message. Wait for the network;
        // use the cache only if it actually fails.
        let inApp = false;
        try {
          inApp = !!request.referrer &&
            new URL(request.referrer).origin === self.location.origin;
        } catch (_err) {
          inApp = false;
        }
        if (inApp) {
          try {
            const response = await network;
            if (response && response.ok && !response.redirected) {
              navCache.put(request, response.clone());
            }
            return response;
          } catch (_err) {
            const cachedCopy = await navCache.match(request);
            return cachedCopy || caches.match("/static/offline.html");
          }
        }
        // Keep the background fetch alive and refresh the cache even when we
        // have already answered from the cache.
        network
          .then((response) => {
            if (response && response.ok && !response.redirected) {
              navCache.put(request, response.clone());
            }
          })
          .catch(() => {});
        const timedOut = new Promise((resolve) =>
          setTimeout(() => resolve(null), NAV_TIMEOUT_MS),
        );
        let fresh = null;
        try {
          fresh = await Promise.race([network, timedOut]);
        } catch (_err) {
          fresh = null;
        }
        if (fresh && fresh.ok && !fresh.redirected) {
          navCache.put(request, fresh.clone());
          return fresh;
        }
        if (fresh && fresh.ok && fresh.redirected) return fresh;
        const cached = await navCache.match(request);
        if (cached) return cached;
        try {
          return await network;
        } catch (_err) {
          return caches.match("/static/offline.html");
        }
      })(),
    );
    return;
  }

  const url = new URL(request.url);
  const isStatic =
    url.origin === self.location.origin && url.pathname.startsWith("/static/");
  const isCdn = CACHEABLE_CDN_HOSTS.has(url.hostname);
  if (!isStatic && !isCdn) return; // API calls, AI endpoints, photos: untouched

  // Static assets + CDNs: respond from cache immediately, refresh quietly.
  event.respondWith(
    caches.open(VERSION).then(async (cache) => {
      const cached = await cache.match(request);
      const network = fetch(request)
        .then((response) => {
          if (response && (response.ok || response.type === "opaque")) {
            cache.put(request, response.clone());
          }
          return response;
        })
        .catch(() => cached);
      return cached || network;
    }),
  );
});

// Workout reminder pushes from the reminder cron. iOS requires a visible
// notification for every push, so always show one.
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (err) {
    data = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "fiT-X", {
      body: data.body || "",
      icon: "/static/android-chrome-192x192.png",
      badge: "/static/android-chrome-192x192.png",
      tag: data.tag || "fitx-reminder",
      data: { url: data.url || "/dashboard" },
    }),
  );
});

// Tapping the notification opens (or focuses) the app at the payload URL.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/dashboard";
  const fullUrl = new URL(target, self.location.origin).href;
  event.waitUntil(
    clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((windowClients) => {
        for (const client of windowClients) {
          if (client.url.startsWith(self.location.origin)) {
            client.focus();
            return client.navigate(fullUrl);
          }
        }
        return clients.openWindow(fullUrl);
      }),
  );
});
