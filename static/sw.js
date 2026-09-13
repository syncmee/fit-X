// Minimal service worker — just enough for Chrome/Android installability.
// Deliberately does NOT cache app responses, so the app always fetches fresh.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
