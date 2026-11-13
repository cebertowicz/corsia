import { test } from '../support/test';
import { expect } from '@playwright/test';
import { TEST_TOKEN } from '../support/corsia';
import { openSuggestionDocument } from '../support/docSuggestions';

/**
 * A thread whose text was deleted is signed by a dashed disc just past the end of the line nearest where its text was. When an
 * unread highlight ends that same line, nothing is drawn beside the highlight for its own sign (the highlight is the sign), so the
 * disc is clear and its centre opens its card, with a click on a desktop and a tap on a phone.
 */

const GONE = 'This text isn’t in the document';
const LOST = 'removed words ';
const UNREAD = 'unread ending';
const BEFORE = ['# Lost text', '', `The line holds ${LOST}and then closes on the ${UNREAD}`, '', 'A closing paragraph keeps the document going.'].join('\n');
const AFTER = BEFORE.replace(LOST, '');

test('@flow:document-comments the dashed disc of a deleted passage opens its card from its centre beside an unread highlight', async ({ page, request }, info) => {
	test.setTimeout(120_000);
	const phone = info.project.name.includes('mobile');
	await page.setViewportSize(phone ? { width: 393, height: 852 } : { width: 1440, height: 1000 });
	await page.route('**/comments/*/viewed', (route) => route.abort());
	await openSuggestionDocument(page, request, BEFORE, [
		{ quote: LOST, body: 'A question about the words that were removed.', author: 'person' },
		{ quote: UNREAD, body: 'A note from another thread on the line ending.' },
	], {
		// The words are deleted from the saved file before the canvas opens, so their comment has no text to stand on.
		beforeOpen: async ({ docPath, ownerId }) => {
			const saved = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: docPath, text: AFTER, thread_id: ownerId } });
			expect(saved.ok(), await saved.text()).toBeTruthy();
		},
	});

	const disc = page.locator('.cm-image-pin.removed .disc');
	await expect(disc, 'the deleted passage is signed by a dashed disc').toHaveCount(1);
	await expect(page.locator('.cm-comment-anchor[data-unread]').first(), 'the line ends in an unread highlight').toBeVisible();
	const box = (await disc.boundingBox())!;
	const centre = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
	// What is drawn at the disc's centre is the disc's own pin, not a sign of the highlight beside it.
	const topmost = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('.cm-image-pin') !== null, centre);
	expect(topmost, 'the disc is the topmost thing at its centre').toBe(true);

	if (phone) await page.touchscreen.tap(centre.x, centre.y); else await page.mouse.click(centre.x, centre.y);
	const card = phone ? page.locator('[data-adaptive-drawer] .drawer-panel') : page.locator('[data-popover]');
	await expect(card, 'the disc opens its card').toBeVisible();
	await expect(card).toContainText(GONE);
});
