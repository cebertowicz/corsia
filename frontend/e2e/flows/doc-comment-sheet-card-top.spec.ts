import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { createSession, TEST_TOKEN } from '../support/corsia';
import { installWorkspace, openThread, seedTextDocument, setDebugVersionOverlay, setMockCorsiaAgentModel } from '../support/heldComments';
import { settled } from '../support/reviewFixtures';

/** Nine threads on a short document: the sheet's list scrolls, and one card is small enough to fit it whole. */
const TEXT = '# Rollout plan\n\nThe team ships the commentable rollout plan in three rings, and each ring waits for the one before it.\n\n' +
	Array.from({ length: 6 }, (_, i) => `Step ${i + 1}: the rollout proceeds to the next ring once the checks pass.\n\n`).join('');
const ANCHORS = ['team ships', 'commentable', 'three rings', 'ring waits', 'Step 1', 'Step 2', 'Step 3', 'Step 5', 'Step 6'];

/** The open card and the scroller that owns it, as rendered. */
const geometry = (page: Page) => page.evaluate(() => {
	const box = (el: Element) => ({ top: Math.round(el.getBoundingClientRect().top), bottom: Math.round(el.getBoundingClientRect().bottom), height: Math.round(el.getBoundingClientRect().height) });
	const card = document.querySelector<HTMLElement>('[data-comment-id].focused');
	if (!card) throw new Error('no open comment card');
	let scroller: HTMLElement | null = card.parentElement;
	while (scroller && !/(auto|scroll)/.test(getComputedStyle(scroller).overflowY)) scroller = scroller.parentElement;
	if (!scroller) throw new Error('the card has no scrolling ancestor');
	return {
		card: box(card),
		scroller: { ...box(scroller), scrollTop: Math.round(scroller.scrollTop), clientHeight: scroller.clientHeight },
		focusInCard: card.contains(document.activeElement),
	};
});

test.use({ hasTouch: true, isMobile: true });

test('@flow:doc-comment-sheet keeps the whole card visible when its reply field takes focus', async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-mobile', 'the phone sheet is taken at 393 in the touch project');
	test.setTimeout(120_000);
	await setMockCorsiaAgentModel(request);
	await setDebugVersionOverlay(request, false);
	const title = `comment-sheet-card-top-${Date.now()}`;
	const owner = await createSession(request, title);
	const doc = await seedTextDocument(request, owner, title, 'plan', TEXT);
	const ids: Record<string, string> = {};
	for (const anchor of ANCHORS) {
		const from = TEXT.indexOf(anchor);
		expect(from, `${anchor} is in the document`).toBeGreaterThanOrEqual(0);
		const posted = await request.post(`/api/artifacts/${doc.artifactId}/comments`, {
			params: { token: TEST_TOKEN },
			data: { body: `Check ${anchor}.`, anchor_text: anchor, from_off: from, to_off: from + anchor.length, delivery_policy: 'immediate' },
		});
		expect(posted.ok(), await posted.text()).toBeTruthy();
		ids[anchor] = (await posted.json()).comment.id as string;
	}
	await page.setViewportSize({ width: 393, height: 852 });
	await installWorkspace(page, owner, [doc], 0, { visible: true, splitWidth: 700 });
	await openThread(page, owner, title);
	await expect(page.locator('.cm-content')).toContainText('commentable');

	// The person opens the comments: the sheet lists all nine cards, and its second card sits at its far edge.
	await page.locator('.comment-status').tap();
	await expect(page.locator('[data-comment-id]')).toHaveCount(ANCHORS.length);
	await settled(page);

	// They tap that card to open it; the sheet makes room for a reply, so the list scrolls to the Reply field.
	const card = page.locator(`[data-comment-id="${ids.commentable}"]`);
	await card.scrollIntoViewIfNeeded();
	await card.locator('.ct').tap();
	await expect(card.getByPlaceholder('Reply…')).toBeVisible();
	await settled(page);
	const opened = await geometry(page);
	expect(opened.card.height, 'the card is small enough to fit the sheet scroller').toBeLessThanOrEqual(opened.scroller.clientHeight);

	// Tapping the Reply field keeps it in view, and the card that holds it stays visible whole.
	await card.getByPlaceholder('Reply…').tap();
	await expect.poll(() => page.evaluate(() => document.activeElement?.tagName)).toBe('TEXTAREA');
	await settled(page);
	const after = await geometry(page);
	expect(after.card.height, 'the card still fits the sheet scroller after the sheet makes room').toBeLessThanOrEqual(after.scroller.clientHeight);
	expect(after.card.top, `the card's top was scrolled ${after.scroller.top - after.card.top}px above the scroller`).toBeGreaterThanOrEqual(after.scroller.top - 1);
	expect(after.card.bottom, `the card's bottom was scrolled ${after.card.bottom - after.scroller.bottom}px below the scroller`).toBeLessThanOrEqual(after.scroller.bottom + 1);
	expect(after.focusInCard, 'the Reply field of that card takes focus').toBe(true);
});
