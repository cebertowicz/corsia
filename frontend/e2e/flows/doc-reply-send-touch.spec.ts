import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { createSession, TEST_TOKEN } from '../support/corsia';
import { installWorkspace, openThread, seedTextDocument, setDebugVersionOverlay, setMockCorsiaAgentModel } from '../support/heldComments';
import { settled } from '../support/reviewFixtures';

// Reply Send on a touch screen: the shared Button's tap area (the invisible area the primitive adds on touch screens) must be the thing a finger hits,
// across the whole area and not just the drawn square, and it must not run into the reply field's own area.
const TEXT = '# Rollout plan\n\nThe team ships the commentable rollout plan in three rings, and each ring waits for the one before it.\n';
const MIN_TAP = { width: 44, height: 44 };

/** Where Send is, and what a finger lands on at points across and just outside its tap area. */
const probe = (page: Page) => page.evaluate(() => {
	const send = document.querySelector<HTMLElement>('[data-reply-composer] button[aria-label="Send reply"]');
	const field = document.querySelector<HTMLTextAreaElement>('[data-reply-composer] textarea');
	if (!send || !field) throw new Error('no Send or no reply field on screen');
	send.scrollIntoView({ block: 'center' });
	const drawn = send.getBoundingClientRect();
	// The tap area is what a finger can hit: the primitive's invisible ::after where it draws one (centred on the button), else the button itself.
	const after = getComputedStyle(send, '::after');
	const grown = after.content !== 'none' && after.position === 'absolute';
	const width = grown ? parseFloat(after.width) : drawn.width, height = grown ? parseFloat(after.height) : drawn.height;
	const cx = (drawn.left + drawn.right) / 2, cy = (drawn.top + drawn.bottom) / 2;
	const area = { left: cx - width / 2, right: cx + width / 2, top: cy - height / 2, bottom: cy + height / 2 };
	// The tap area has the button's rounded corners (8px), so a corner point sits 4px in along both sides; the sides are taken 1px in.
	const midX = (area.left + area.right) / 2, midY = (area.top + area.bottom) / 2;
	const points: Array<[string, number, number]> = [
		['left edge', area.left + 1, midY], ['right edge', area.right - 1, midY], ['top edge', midX, area.top + 1], ['bottom edge', midX, area.bottom - 1],
		['top-left', area.left + 4, area.top + 4], ['top-right', area.right - 4, area.top + 4], ['bottom-left', area.left + 4, area.bottom - 4], ['bottom-right', area.right - 4, area.bottom - 4], ['centre', midX, midY],
	];
	const inside: Record<string, string> = {};
	for (const [name, x, y] of points) { const el = document.elementFromPoint(x, y); inside[name] = el && send.contains(el) ? 'Send' : `${el?.tagName.toLowerCase() ?? 'nothing'}${el?.className ? '.' + String(el.className).split(' ')[0] : ''}`; }
	const fieldBox = field.getBoundingClientRect();
	const justLeft = document.elementFromPoint(area.left - 2, (area.top + area.bottom) / 2);
	return {
		drawn: { width: drawn.width, height: drawn.height },
		area: { width: area.right - area.left, height: area.bottom - area.top },
		inside,
		fieldClearOfArea: fieldBox.right <= area.left + 0.5,
		leftOfAreaIsField: justLeft === field,
		isPrimitive: send.classList.contains('btn'),
	};
});

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
		// A finger anywhere in the tap area lands on Send.
		for (const [point, landed] of Object.entries(seen.inside)) expect(landed, `a finger at the tap area's ${point} lands on Send`).toBe('Send');
		expect(seen.area.width, 'the tap area is at least 44 wide').toBeGreaterThanOrEqual(MIN_TAP.width);
		expect(seen.area.height, 'the tap area is at least 44 tall').toBeGreaterThanOrEqual(MIN_TAP.height);
		// And the area does not run into the field's: the field ends where it begins, and a point just outside belongs to the field.
		expect(seen.fieldClearOfArea, 'the field ends where the tap area begins, so the two never overlap').toBe(true);
		expect(seen.leftOfAreaIsField, 'a point just left of the tap area belongs to the field').toBe(true);
		expect(seen.isPrimitive, 'Send is the shared Button, not a local style').toBe(true);
	});
}
