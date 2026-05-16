// StreamLink Service Worker — one-shot eviction stub.
//
// The Handoff/Offline-Playback feature was replaced by Stream-to-Device.
// Older versions of this file registered a real SW that cached the app shell
// and read-only library APIs. Devices that had it installed will keep using
// the stale cache until a newer SW activates, so this file ships once just
// to unregister itself and wipe every cache it ever created.
//
// Once enough time has passed that no device still has the old SW alive,
// this file (and the evictLegacyServiceWorker() call in index.html) can be
// deleted outright.

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    try {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => caches.delete(k)));
    } catch (_) {}
    try { await self.registration.unregister(); } catch (_) {}
    const clients = await self.clients.matchAll({ type: "window" });
    for (const c of clients) {
      try { c.navigate(c.url); } catch (_) {}
    }
  })());
});

// Pass every request straight through to the network — no caching, no fallback.
self.addEventListener("fetch", () => {});
