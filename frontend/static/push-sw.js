// Urgent-push worker (R17). It shows a notification on a push and opens the item on a tap, and nothing else.
//
// It is NOT sw.js: that file is the kill-switch that removes every stuck worker. This one has no fetch handler
// (so it sits in no request path), no caching, no `clients.claim()` and no `skipWaiting()`, and its activate step
// neither unregisters nor navigates a window. app.html unregisters every worker except this one by its URL.
//
// A push always shows a notification: a push event that shows nothing makes browsers drop the subscription
// (Safari after three silent pushes; Chrome and Firefox show a generic one themselves). A payload that is missing
// or unreadable still shows the generic text.

const GENERIC_TITLE = 'Something needs you';
const GENERIC_LINE = 'Open CORSIA to see it.';
// The one place a tap may open (R20): an Activity item on this origin.
const ACTIVITY_PATH = /^\/\?activity=[A-Za-z0-9._~-]{1,100}$/;

self.addEventListener('push', (event) => {
	let data = {};
	try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
	const text = (value, fallback) => (typeof value === 'string' && value.trim() ? value.trim() : fallback);
	const path = typeof data.path === 'string' && ACTIVITY_PATH.test(data.path) ? data.path : '/';
	event.waitUntil(self.registration.showNotification(text(data.title, GENERIC_TITLE), {
		body: text(data.body, GENERIC_LINE),
		// One notification per item: a repeat replaces the earlier one (R8).
		tag: text(data.tag, path),
		data: { path },
	}));
});

self.addEventListener('notificationclick', (event) => {
	event.notification.close();
	const requested = event.notification.data && event.notification.data.path;
	// Re-checked here: whatever the payload said, only an Activity item route or the home page opens.
	const path = typeof requested === 'string' && ACTIVITY_PATH.test(requested) ? requested : '/';
	event.waitUntil((async () => {
		const open = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
		const target = new URL(path, self.location.origin).href;
		// No navigate(): the spec rejects it for a window this worker does not control, which is any window opened before
		// the worker registered. Focus a window of this origin and hand it the path; the app opens the item itself.
		for (const client of open) {
			if (new URL(client.url).origin === self.location.origin && 'focus' in client) {
				// Focus is best effort (a platform may refuse it); the path goes to the window either way.
				await client.focus().catch(() => undefined);
				client.postMessage({ type: 'corsia-open-activity', path });
				return;
			}
		}
		await self.clients.openWindow(target);
	})());
});

// Intentionally NO `fetch` listener, no `install`/`activate` handlers, no `clients.claim()` and no `skipWaiting()`.
