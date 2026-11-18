import { test, expect } from '../support/test';
import { comment, open, seed } from '../support/reviewFixtures';
import { raiseAsk } from '../support/reviewAsk';

const ask = (page: import('@playwright/test').Page, text: string) => page.getByRole('region', { name: 'Review request' }).filter({ hasText: text });
const reviewUrl = (id: string) => `**/api/attention/${id}/review*`;

test.describe('review bar recovery', () => {
	test.setTimeout(90_000);

	test('reads a real ask raised while the socket is disconnected', async ({ page, context, request }) => {
		const doc = await seed(request, 'Reconnect review');
		await page.addInitScript(() => {
			const NativeSocket = WebSocket;
			class TrackedSocket extends NativeSocket {
				constructor(url: string | URL, protocols?: string | string[]) {
					super(url, protocols);
					(window as any).__reviewSocket = this;
				}
			}
			window.WebSocket = TrackedSocket;
		});
		await open(page, doc, 'default', 'light');
		await page.waitForFunction(() => (window as any).__reviewSocket?.readyState === WebSocket.OPEN);
		await context.setOffline(true);
		await page.evaluate(() => (window as any).__reviewSocket.close());
		await page.waitForFunction(() => (window as any).__reviewSocket?.readyState === WebSocket.CLOSED);
		try {
			raiseAsk(doc.threadId, doc.artifactId, 'Reconnect recovery ask');
		} finally {
			await context.setOffline(false);
		}
		await expect(ask(page, 'Reconnect recovery ask')).toBeVisible({ timeout: 20_000 });
		await expect(ask(page, 'Reconnect recovery ask').getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
	});

	test('retries a failed read after service returns', async ({ page, request }) => {
		const doc = await seed(request, 'Retry review');
		raiseAsk(doc.threadId, doc.artifactId, 'Retry recovery ask');
		let failed = false;
		await page.route(reviewUrl(doc.artifactId), async (route) => {
			if (!failed) { failed = true; await route.abort('failed'); }
			else await route.continue();
		});
		await open(page, doc, 'default', 'light');
		await expect(ask(page, 'Retry recovery ask')).toBeVisible({ timeout: 15_000 });
		expect(failed).toBe(true);
	});

	test('foregrounding reads again while an earlier read is outstanding', async ({ page, request }) => {
		const doc = await seed(request, 'Foreground review');
		raiseAsk(doc.threadId, doc.artifactId, 'Foreground recovery ask');
		let release!: () => void, entered!: () => void;
		const held = new Promise<void>((resolve) => { release = resolve; });
		const intercepted = new Promise<void>((resolve) => { entered = resolve; });
		let first = true, foregrounded = false, foregroundReads = 0;
		await page.route(reviewUrl(doc.artifactId), async (route) => {
			if (!foregrounded) {
				const response = await route.fetch();
				if (first) { first = false; entered(); }
				await held; await route.fulfill({ response });
			} else { foregroundReads += 1; await route.continue(); }
		});
		await page.addInitScript(() => {
			let state = 'visible';
			Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
			(window as any).__setVisibility = (next: string) => { state = next; document.dispatchEvent(new Event('visibilitychange')); };
		});
		try {
			await open(page, doc, 'default', 'light');
			await intercepted;
			foregrounded = true;
			await page.evaluate(() => { (window as any).__setVisibility('hidden'); (window as any).__setVisibility('visible'); });
			await expect(ask(page, 'Foreground recovery ask')).toBeVisible({ timeout: 8_000 });
			expect(foregroundReads).toBeGreaterThan(0);
		} finally { release(); }
	});

	test('an older response cannot erase a newer real ask', async ({ page, request }) => {
		const doc = await seed(request, 'Stale review');
		let release!: () => void, entered!: () => void, finished!: () => void;
		const held = new Promise<void>((resolve) => { release = resolve; });
		const intercepted = new Promise<void>((resolve) => { entered = resolve; });
		const completed = new Promise<void>((resolve) => { finished = resolve; });
		let first = true;
		await page.route(reviewUrl(doc.artifactId), async (route) => {
			if (first) {
				first = false;
				const response = await route.fetch();
				entered(); await held; await route.fulfill({ response }); finished();
			} else await route.continue();
		});
		try {
			await open(page, doc, 'default', 'light');
			await intercepted;
			raiseAsk(doc.threadId, doc.artifactId, 'Newer recovery ask');
			// The fixture writes directly; a real comment emits the live refresh event.
			await comment(request, doc, 'Refresh the review read.', 1);
			await expect(ask(page, 'Newer recovery ask')).toBeVisible({ timeout: 15_000 });
			release(); await completed;
			await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
			await expect(ask(page, 'Newer recovery ask')).toBeVisible();
		} finally { release(); }
	});
});
