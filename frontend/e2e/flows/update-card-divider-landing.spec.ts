import { expect, test, type Page } from '@playwright/test';
import { setFocusMode } from '../support/focusMode';
import { injectMessage } from '../support/mock';
import {
	createSession,
	openAppByUrl,
	TEST_TOKEN,
	watchSessionSubscriptions,
} from '../support/corsia';

const CARD_TIME = '2026-09-29T12:00:00Z';
const DIVIDER_TIME = '2026-09-29T11:00:00Z';

function longTranscript(label: string): string {
	return Array.from({ length: 180 }, (_, index) => `${label} transcript row ${index}`).join('\n\n');
}

function card(threadId: string, id = 1) {
	return {
		id,
		thread_id: threadId,
		kind: 'done',
		node_id: `node-${id}`,
		title: id === 1 ? 'Finished work' : 'Later update',
		after_seq: 0,
		created_at: CARD_TIME,
		node_exists: true,
	};
}

function updateResponse(threadId: string, divider: boolean): string {
	return JSON.stringify({
		cards: [card(threadId)],
		has_more: false,
		divider: divider ? { since: DIVIDER_TIME, first_seq: null } : null,
	});
}

async function useUpdateFixture(
	page: Page,
	threadId: string,
	{ divider, delayed }: { divider: boolean; delayed: boolean },
): Promise<void> {
	await page.route(`**/api/threads/${threadId}/updates**`, async (route) => {
		if (delayed) await new Promise((resolve) => setTimeout(resolve, 75));
		await route.fulfill({ contentType: 'application/json', body: updateResponse(threadId, divider) });
	});
}

async function useDeferredUpdateFixture(page: Page, threadId: string): Promise<{
	waitForRequest: () => Promise<void>;
	release: () => void;
}> {
	let requested!: () => void;
	let release!: () => void;
	const requestStarted = new Promise<void>((resolve) => { requested = resolve; });
	const responseReleased = new Promise<void>((resolve) => { release = resolve; });
	await page.route(`**/api/threads/${threadId}/updates**`, async (route) => {
		requested();
		await responseReleased;
		await route.fulfill({ contentType: 'application/json', body: updateResponse(threadId, true) });
	});
	return { waitForRequest: () => requestStarted, release };
}

async function showUpdateDot(page: Page, threadId: string): Promise<void> {
	await page.route('**/api/plans/operator/thread-states**', async (route) => {
		await route.fulfill({
			contentType: 'application/json',
			body: JSON.stringify({
				states: { [threadId]: { state: 'idle', paused: false, attention_items: [], has_update: true } },
			}),
		});
	});
}

async function expectAtLiveEdge(page: Page): Promise<void> {
	await expect.poll(() => page.locator('.messages').evaluate((element) => {
		const scroller = element as HTMLElement;
		return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
	})).toBeLessThanOrEqual(2);
}

async function openSidebarIfNarrow(page: Page): Promise<void> {
	if ((page.viewportSize()?.width ?? 0) <= 768) {
		await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	}
}

test.describe('@surface:chat update-card landing', () => {
	// Focus is on by default and lists update cards inside its collapsed outcomes; these flows cover the transcript cards.
	test.beforeEach(async ({ request }, testInfo) => {
		test.skip(testInfo.project.name === 'chromium-mobile', 'This flow sets its own desktop and 393px viewports.');
		await setFocusMode(request, false);
	});
	test.afterEach(async ({ request }) => {
		await setFocusMode(request, true);
	});

	test('a normal row remains at the live edge when cards return late on desktop', async ({ page, request }) => {
		const title = `delayed-card-normal-${Date.now()}`;
		const threadId = await createSession(request, title);
		await injectMessage(request, threadId, { role: 'assistant', content: longTranscript('normal') }, TEST_TOKEN);
		await useUpdateFixture(page, threadId, { divider: false, delayed: true });

		await openAppByUrl(page);
		await openSidebarIfNarrow(page);
		await page.getByText(title).first().click();
		await expect(page.locator(`[data-update-id="1"]`)).toBeVisible();
		await expectAtLiveEdge(page);
	});

	test('an update-dot keeps the visit divider visible after delayed cards and a live event at 393px', async ({ page, request }) => {
		await page.setViewportSize({ width: 393, height: 852 });
		const title = `delayed-card-update-${Date.now()}`;
		const threadId = await createSession(request, title);
		await injectMessage(request, threadId, { role: 'assistant', content: longTranscript('update') }, TEST_TOKEN);
		await useUpdateFixture(page, threadId, { divider: true, delayed: true });
		await showUpdateDot(page, threadId);
		const subscriptions = watchSessionSubscriptions(page);

		await openAppByUrl(page);
		await openSidebarIfNarrow(page);
		await expect(page.getByLabel('Update since your last visit')).toBeVisible();
		await page.getByLabel('Update since your last visit').click();
		await subscriptions.waitForSubscribed(threadId);
		await expect(page.locator('[data-visit-divider]')).toBeVisible();
		await injectMessage(request, threadId, { role: 'assistant', content: 'live transcript event after card read' }, TEST_TOKEN);

		await expect(page.locator('[data-visit-divider]')).toBeInViewport();
		await expect(page.locator(`[data-update-id="1"]`)).toBeInViewport();
	});

	test('a pre-response user gesture prevents a delayed update-dot landing', async ({ page, request }) => {
		const title = `delayed-card-gesture-${Date.now()}`;
		const threadId = await createSession(request, title);
		await injectMessage(request, threadId, { role: 'assistant', content: longTranscript('gesture') }, TEST_TOKEN);
		const update = await useDeferredUpdateFixture(page, threadId);
		await showUpdateDot(page, threadId);

		await openAppByUrl(page);
		await openSidebarIfNarrow(page);
		await page.evaluate(() => {
			const original = HTMLElement.prototype.scrollIntoView;
			document.documentElement.dataset.dividerScrolls = '0';
			HTMLElement.prototype.scrollIntoView = function (...args) {
				if (this.hasAttribute('data-visit-divider')) {
					document.documentElement.dataset.dividerScrolls = String(
						Number(document.documentElement.dataset.dividerScrolls) + 1,
					);
				}
				return original.apply(this, args);
			};
		});
		await page.getByLabel('Update since your last visit').click();
		await update.waitForRequest();
		await page.locator('.messages').dispatchEvent('wheel', { deltaY: -120 });
		update.release();

		await expect(page.locator(`[data-update-id="1"]`)).toBeVisible();
		await expect.poll(() => page.locator('html').evaluate((element) => (
			element.dataset.dividerScrolls
		))).toBe('0');
	});
});
