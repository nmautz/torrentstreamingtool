// StreamLink Service Worker — offline shell + cached library list.
//
// Strategy:
//   • Navigation HTML: network-first, falls back to cached '/' so the dashboard
//     boots when the device is offline (or when DNS for remote.local fails).
//   • App shell (CSS/JS/Tailwind CDN): cache-first.
//   • Read-only library APIs (/api/library, /api/library/{id}/files, /api/profiles,
//     /api/library/{id}/skip-data, /api/library/{id}/subtitle): stale-while-revalidate
//     so the offline player has the data it needs even with no connection.
//   • Everything else: network-only passthrough. Notably we do NOT cache
//     /api/library/offline-cache/* (huge MP4s — they go in IndexedDB instead) or
//     /api/events (SSE).
//
// Bump SW_VERSION whenever this file changes so browsers pick up the new logic.

const SW_VERSION = "v1";
const SHELL_CACHE = `streamlink-shell-${SW_VERSION}`;
const API_CACHE   = `streamlink-api-${SW_VERSION}`;

const SHELL_URLS = [
  "/",
  "/index.html",
  "/manifest.json",
  "https://cdn.tailwindcss.com",
];

// Read-only API paths whose responses we cache for offline boot.
function isCacheableApi(url) {
  const p = url.pathname;
  if (p === "/api/profiles") return true;
  if (p === "/api/library") return true;
  if (/^\/api\/library\/[^/]+\/files$/.test(p)) return true;
  if (/^\/api\/library\/[^/]+\/skip-data$/.test(p)) return true;
  if (/^\/api\/library\/[^/]+\/subtitle$/.test(p)) return true;
  return false;
}

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    // Pre-cache requests individually — one failure (e.g. CDN offline at install
    // time) shouldn't abort install of the rest.
    await Promise.all(SHELL_URLS.map(async (u) => {
      try { await cache.add(u); } catch (_) {}
    }));
    self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keep = new Set([SHELL_CACHE, API_CACHE]);
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => !keep.has(k)).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;     // POST/PUT/DELETE all bypass the SW
  const url = new URL(req.url);

  // SSE never goes through the cache — it's a long-lived connection.
  if (url.pathname === "/api/events") return;
  // Big offline-cache MP4s are stored in IndexedDB on the client; don't double up.
  if (url.pathname.startsWith("/api/library/offline-cache/")) return;
  // qBit/VLC/ffmpeg state-changing endpoints all use POST — already filtered above.

  // Navigation requests: try network, fall back to cached '/' for the offline shell.
  if (req.mode === "navigate") {
    event.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        return fresh;
      } catch (_) {
        const cache = await caches.open(SHELL_CACHE);
        return (await cache.match("/")) ||
               (await cache.match("/index.html")) ||
               new Response("Offline", { status: 503, statusText: "Offline" });
      }
    })());
    return;
  }

  // Read-only library APIs: stale-while-revalidate.
  if (isCacheableApi(url)) {
    event.respondWith((async () => {
      const cache = await caches.open(API_CACHE);
      const cached = await cache.match(req);
      const network = fetch(req).then(async (resp) => {
        if (resp && resp.ok) {
          try { await cache.put(req, resp.clone()); } catch (_) {}
        }
        return resp;
      }).catch(() => null);
      return cached || (await network) ||
             new Response(JSON.stringify({ offline: true }),
                          { status: 503, headers: { "Content-Type": "application/json" } });
    })());
    return;
  }

  // App shell + Tailwind CDN: cache-first.
  if (url.origin === self.location.origin && (
      url.pathname === "/" ||
      url.pathname === "/index.html" ||
      url.pathname === "/manifest.json")) {
    event.respondWith((async () => {
      const cache = await caches.open(SHELL_CACHE);
      const cached = await cache.match(req);
      if (cached) {
        // Refresh in the background so future loads are current.
        fetch(req).then((r) => { if (r && r.ok) cache.put(req, r.clone()).catch(()=>{}); }).catch(()=>{});
        return cached;
      }
      try {
        const fresh = await fetch(req);
        if (fresh && fresh.ok) cache.put(req, fresh.clone()).catch(()=>{});
        return fresh;
      } catch (_) {
        return new Response("Offline", { status: 503 });
      }
    })());
    return;
  }
  if (url.origin === "https://cdn.tailwindcss.com") {
    event.respondWith((async () => {
      const cache = await caches.open(SHELL_CACHE);
      const cached = await cache.match(req);
      if (cached) return cached;
      try {
        const fresh = await fetch(req);
        if (fresh && fresh.ok) cache.put(req, fresh.clone()).catch(()=>{});
        return fresh;
      } catch (_) {
        return cached || new Response("/* offline */", { status: 503 });
      }
    })());
    return;
  }

  // Everything else: network-only (SW does not respond → browser handles normally).
});
