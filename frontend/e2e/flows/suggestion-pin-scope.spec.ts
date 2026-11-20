import { test } from '../support/test';
import { expect } from '@playwright/test';
import { reviewerSession, suggest } from '../support/liveConcurrency';
import { openLiveDocument, seedLiveDocument } from '../support/liveDocument';
import { setDebugVersionOverlay } from '../support/heldComments';

const quotes = ['first target line', 'second target line'];
const text = Array.from({ length: 30 }, (_, i) => i === 8 ? quotes[0] : i === 12 ? quotes[1] : `document line ${i}`).join('\n\n');

/** A suggestion's Accept and Reject row sticks to the bottom of a popover that holds that one thread; in the list popover, where every suggestion is its own card in one scroller, no row sticks over the next card. */
test('the suggestion row is pinned in the one-thread popover and in no card of the list popover', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop');
	await page.setViewportSize({ width: 1440, height: 900 });
	await setDebugVersionOverlay(request, false);
	const doc = await seedLiveDocument(request, 'suggestion-pin-scope', text);
	const reviewer = await reviewerSession(request, 'suggestion-pin-scope');
	for (const quote of quotes) await suggest(request, doc, reviewer.id, quote, `${quote} reworded`, 'Prefer this wording. '.repeat(8));
	await openLiveDocument(page, doc, 'document line 0');
	const positions = (within: string) => page.evaluate((root) => [...document.querySelectorAll<HTMLElement>(`${root} .ct-suggestion-pin`)].map((row) => getComputedStyle(row).position), within);

	await page.locator('[data-comments-entry]').click();
	await expect(page.locator('[data-popover] .list-pop')).toBeVisible();
	await expect.poll(async () => (await positions('.list-pop')).length, { message: 'both suggestions show a row in the list popover' }).toBe(2);
	expect(await positions('.list-pop'), 'no row sticks in the list popover').toEqual(['static', 'static']);

	await page.keyboard.press('Escape');
	await expect(page.locator('[data-popover] .list-pop')).toHaveCount(0);
	await page.locator('.cm-suggestion-del').filter({ hasText: quotes[0] }).click();
	await expect.poll(async () => await positions('[data-popover]'), { message: 'the one-thread popover pins its row' }).toEqual(['sticky']);
});
