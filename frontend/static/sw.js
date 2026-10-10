// Self-uninstalling service worker (v3, kill-switch).
//
// History: v1/v2 had a `fetch` handler that put this SW in the path of
// every request. Even with `event.respondWith(fetch(event.request))`
// (network-passthrough), the SW remained registered and intercepted
// every page load — and once it had `skipWaiting()` + `clients.claim()`
// in its activate handler, it persisted across app restarts via
// ~/Library/WebKit/<bundle>/WebsiteData/. Net effect: deploys to remote
// Tauri clients didn't pick up new code without manually wiping the
// WebView cache, which we can't ask remote users to do.
//
// This version takes over once, deletes every Cache-API cache,
// unregisters itself, force-reloads all controlled clients, and
// registers NO `fetch` handler. After a single update cycle the page is
// SW-free forever, and normal HTTP cache headers + Vite-hashed chunk
// URLs handle freshness correctly on every subsequent deploy.
//
// Why this reaches stuck clients: per the SW spec, the WebView's update
// check fetches /sw.js with the skip-service-worker flag set, bypassing
// any active SW. Combined with the backend's
// `Cache-Control: no-cache, no-store, must-revalidate` on /sw.js
// (backend/main.py:303-305), every navigation ≥24h apart triggers a
// fresh fetch — so this kill-switch eventually replaces any stuck
// older SW, even if that older SW had aggressive caching.
//
// Once enough deploy cycles have passed that we're confident every
// client has run this at least once, this file can be deleted entirely.
// Keep it for now as a permanent bouncer.

self.addEventListener('install', (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    // 1. Wipe every Cache-API cache the previous SW may have populated.
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));

    // 2. Take control of any pages the old SW was controlling so we can
    //    navigate them in step 4.
    await self.clients.claim();

    // 3. Self-destruct. After unregister() resolves, future page loads
    //    have no SW at all — fetches go straight to network.
    await self.registration.unregister();

    // 4. Force-reload every controlled client so they re-fetch the page
    //    from network (with no SW interception now). Use `client.navigate`
    //    rather than postMessage because the page's old JS may not be
    //    listening for our message — `navigate()` is unilateral.
    const clientList = await self.clients.matchAll({ includeUncontrolled: true });
    for (const client of clientList) {
      if ('navigate' in client) {
        try {
          await client.navigate(client.url);
        } catch (e) {
          // Cross-origin navigates are blocked; nothing to do.
        }
      }
    }
  })());
});

// Intentionally NO `fetch` listener. The presence of a fetch handler is
// what makes the browser route every request through this SW. With no
// handler registered, the browser short-circuits the SW entirely for
// resource fetches.
