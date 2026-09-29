// fiT-X service worker — static caching + offline fallback.
//
// Live data is never cached: page navigations are network-first (the app
// always shows fresh data when online) and fall back to an offline page
// when the network is gone. Only the app shell (/static/*) and known
// font/chart CDNs use cache-with-refresh, so updates land on the next
// load. Bump VERSION whenever shipped static assets change.
const VERSION = "fitx-v1";

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
];

// CDNs the app shell depends on; responses from them are opaque but
// safe to store and serve.
const CACHEABLE_CDN_HOSTS = new Set([
  "fonts.googleapis.com",
  "fonts.gstatic.com",
  "cdn.apexcharts.com",
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

  // App navigations: network-first, offline page as the last resort.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() => caches.match("/static/offline.html")),
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
