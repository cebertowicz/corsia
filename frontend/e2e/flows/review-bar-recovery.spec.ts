import { test, expect } from '../support/test';
import { open, seed } from '../support/reviewFixtures';
import { beginComment, submitButton } from '../support/liveCommentProof';
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

	test('recovers a failed read when the window regains focus', async ({ page, request }) => {
		const doc = await seed(request, 'Retry review');
		raiseAsk(doc.threadId, doc.artifactId, 'Retry recovery ask');
		let failures = 0, allowRead = false;
		const reads: { at: number; phase: string; status: number | string }[] = [];
		await page.route(reviewUrl(doc.artifactId), async (route) => {
			const at = Date.now(), phase = allowRead ? 'after-focus' : 'before-focus';
			if (!allowRead) {
				await route.abort('failed'); failures += 1;
				reads.push({ at, phase, status: 'aborted' });
				console.log('review read', JSON.stringify(reads.at(-1)));
			} else {
				const response = await route.fetch();
				reads.push({ at, phase, status: response.status() });
				console.log('review read', JSON.stringify(reads.at(-1)));
				await route.fulfill({ response });
			}
		});
		await open(page, doc, 'default', 'light');
		await expect.poll(() => failures).toBeGreaterThan(0);
		await expect(ask(page, 'Retry recovery ask')).toHaveCount(0);
		// No artifact, plan or comment event is seeded after this point.
		allowRead = true;
		const focusedAt = await page.evaluate(() => { const at = Date.now(); window.dispatchEvent(new Event('focus')); return at; });
		console.log('focus', focusedAt);
		await expect(ask(page, 'Retry recovery ask')).toBeVisible({ timeout: 3_000 });
		const success = reads.find((read) => typeof read.status === 'number' && read.status >= 200 && read.status < 300);
		expect(success?.phase).toBe('after-focus');
		expect(success?.at).toBeGreaterThanOrEqual(focusedAt);
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

	// Preserve the existing loadSequence guard in load() in reviewBarState.svelte.ts.
	test('an older response cannot erase a newer real ask', async ({ page, request }) => {
		const doc = await seed(request, 'Stale review');
		let release!: () => void, entered!: () => void, finished!: () => void;
		const held = new Promise<void>((resolve) => { release = resolve; });
		const intercepted = new Promise<void>((resolve) => { entered = resolve; });
		const completed = new Promise<void>((resolve) => { finished = resolve; });
		let first = true, freshDelivered = false, staleDeliveredAfterFresh = false;
		await page.route(reviewUrl(doc.artifactId), async (route) => {
			if (first) {
				first = false;
				const response = await route.fetch();
				entered(); await held; await route.fulfill({ response });
				staleDeliveredAfterFresh = freshDelivered;
				finished();
			} else {
				const response = await route.fetch();
				await route.fulfill({ response });
				freshDelivered = true;
			}
		});
		try {
			await open(page, doc, 'default', 'light');
			await intercepted;
			raiseAsk(doc.threadId, doc.artifactId, 'Newer recovery ask');
			// A comment created through the editor emits the live refresh event.
			await beginComment(page, 'Step 1: the rollout proceeds to the next ring.', 'Refresh the review read.');
			await submitButton(page).click();
			await expect(page.getByPlaceholder('Comment, or @ to tag an agent')).toHaveCount(0);
			await expect(ask(page, 'Newer recovery ask')).toBeVisible({ timeout: 15_000 });
			expect(freshDelivered).toBe(true);
			release(); await completed;
			expect(staleDeliveredAfterFresh).toBe(true);
			await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
			await expect(ask(page, 'Newer recovery ask')).toBeVisible();
		} finally { release(); }
	});
});
