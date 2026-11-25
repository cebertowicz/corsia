import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { createSession, TEST_TOKEN } from '../support/corsia';
import { installWorkspace, openThread, seedTextDocument, setDebugVersionOverlay, setMockCorsiaAgentModel } from '../support/heldComments';
import { settled } from '../support/reviewFixtures';

// Reply Send on a touch screen: the shared Button's tap area (its box grown 8px on every side) must be the thing a finger hits,
// across the whole area and not just the drawn square, and it must not run into the reply field's own area.
const TEXT = '# Rollout plan\n\nThe team ships the commentable rollout plan in three rings, and each ring waits for the one before it.\n';
const TAP_GROW = 8;
const MIN_TAP = { width: 44, height: 44 };

/** Where Send is, and what a finger lands on at points across and just outside its tap area. */
const probe = (page: Page) => page.evaluate((grow) => {
	const send = document.querySelector<HTMLElement>('[data-reply-composer] button[aria-label="Send reply"]');
	const field = document.querySelector<HTMLTextAreaElement>('[data-reply-composer] textarea');
	if (!send || !field) throw new Error('no Send or no reply field on screen');
	send.scrollIntoView({ block: 'center' });
	const drawn = send.getBoundingClientRect();
	const area = { left: drawn.left - grow, right: drawn.right + grow, top: drawn.top - grow, bottom: drawn.bottom + grow };
	const hits = (x: number, y: number) => document.elementFromPoint(x, y) === send;
	const edge = 1;
	const xs = [area.left + edge, (area.left + area.right) / 2, area.right - edge];
	const ys = [area.top + edge, (area.top + area.bottom) / 2, area.bottom - edge];
	const inside: Record<string, boolean> = {};
	for (const x of xs) for (const y of ys) inside[`${Math.round(x - area.left)},${Math.round(y - area.top)}`] = hits(x, y);
	const fieldBox = field.getBoundingClientRect();
	const justLeft = document.elementFromPoint(area.left - 2, (area.top + area.bottom) / 2);
	return {
		drawn: { width: drawn.width, height: drawn.height },
		area: { width: area.right - area.left, height: area.bottom - area.top },
		inside,
		fieldClearOfArea: fieldBox.right <= area.left + 0.5,
		leftOfAreaIsField: justLeft === field,
		viewport: { width: innerWidth, height: innerHeight },
	};
}, TAP_GROW);

test.use({ hasTouch: true, isMobile: true });

for (const view of [{ name: '393', width: 393, height: 852 }, { name: '320', width: 320, height: 640 }]) {
	test(`@flow:doc-reply-send-touch Send takes taps across its whole tap area at ${view.name}`, async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-mobile', 'taps are taken in the touch project');
		test.setTimeout(90_000);
		await setMockCorsiaAgentModel(request);
		await setDebugVersionOverlay(request, false);
		const title = `reply-send-touch-${view.name}-${Date.now()}`;
		const owner = await createSession(request, title);
		const doc = await seedTextDocument(request, owner, title, 'plan', TEXT);
		const anchor = 'commentable';
		const from = TEXT.indexOf(anchor);
		const posted = await request.post(`/api/artifacts/${doc.artifactId}/comments`, {
			params: { token: TEST_TOKEN },
			data: { body: `Check ${anchor}.`, anchor_text: anchor, from_off: from, to_off: from + anchor.length, delivery_policy: 'immediate' },
		});
		expect(posted.ok(), await posted.text()).toBeTruthy();
		const id = (await posted.json()).comment.id as string;

		await page.setViewportSize({ width: view.width, height: view.height });
		await installWorkspace(page, owner, [doc], 0, { visible: true, splitWidth: 700 });
		await openThread(page, owner, title);
		await expect(page.locator('.cm-content')).toContainText(anchor);
		await page.locator('[data-comments-entry]').tap();
		const card = page.locator(`[data-comment-id="${id}"]`);
		await card.scrollIntoViewIfNeeded();
		await card.locator('.ct').tap();
		await card.getByPlaceholder('Reply…').fill('A reply');
		await settled(page);

		const seen = await probe(page);
		expect(seen.fieldClearOfArea, 'the field ends where the tap area begins, so the two never overlap').toBe(true);
		expect(seen.leftOfAreaIsField, 'a point just left of the tap area belongs to the field').toBe(true);
		expect(seen.area.width, 'the tap area is at least 44 wide').toBeGreaterThanOrEqual(MIN_TAP.width);
		expect(seen.area.height, 'the tap area is at least 44 tall').toBeGreaterThanOrEqual(MIN_TAP.height);
		expect(seen.drawn.width === 28 && seen.drawn.height === 28, 'Send is not the old local 28x28 square').toBe(false);
		for (const [point, hit] of Object.entries(seen.inside)) expect(hit, `a finger at ${point} of the tap area lands on Send`).toBe(true);
	});
}
