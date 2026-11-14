import { expect, test } from '../support/test';
import type { Page } from '@playwright/test';
import { commentedMessage, QUOTE } from '../support/chatCommentedMessage';
import { closeComments } from '../support/commentDrawer';
import { expectMarginColumnUp, open, pill, FIRST } from '../support/marginCards';

/**
 * A margin card first shows level with its text, in a chat or a document the reader has already scrolled. The column is
 * drawn on its canvas a moment after its cards mount; a card placed before that would be off by the scroll for the frame it
 * first shows, and then jump to its line.
 */

type Frame = Record<string, number | 'hidden' | 'noline'>;

/** Samples every frame, for each card: hidden, or how far its top is from its text's top. */
async function sampleFirstFrames(page: Page, cards: Record<string, { card: string; mark: string; text?: string }>) {
	await page.evaluate((all) => {
		const frames: Record<string, unknown>[] = [];
		const t0 = performance.now();
		const step = () => {
			const row: Record<string, unknown> = {};
			for (const [key, { card, mark, text }] of Object.entries(all)) {
				const wrap = document.querySelector<HTMLElement>(card);
				if (!wrap) { row[key] = 'hidden'; continue; }
				const line = [...document.querySelectorAll<HTMLElement>(mark)].find((node) => !text || node.textContent?.includes(text));
				const shown = getComputedStyle(wrap).visibility === 'visible' && wrap.getClientRects().length > 0;
				row[key] = !shown ? 'hidden' : line ? Math.round(wrap.getBoundingClientRect().top - line.getBoundingClientRect().top) : 'noline';
			}
			frames.push(row);
			if (performance.now() - t0 < 4000) requestAnimationFrame(step);
		};
		requestAnimationFrame(step);
		(window as unknown as { firstFrames: unknown }).firstFrames = frames;
	}, cards);
	return async () => page.evaluate(() => (window as unknown as { firstFrames: Frame[] }).firstFrames);
}

/** Each card's first shown frame has it where it settles beside its text. */
function expectFirstFrameSettled(frames: Frame[], keys: string[]) {
	for (const key of keys) {
		const shown = frames.map((frame) => frame[key]).filter((value) => value !== 'hidden');
		expect(shown.length, `${key} is shown`).toBeGreaterThan(0);
		expect(typeof shown[0], `${key} has a line`).toBe('number');
		const first = shown[0] as number, settled = shown[shown.length - 1] as number;
		expect(Math.abs(first - settled), `${key} first shows ${first}px from its line, settles at ${settled}px`).toBeLessThanOrEqual(2);
	}
}

test('a chat margin card first shows level with its text in a scrolled transcript', async ({ page, request }, info) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1' || info.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	// At 1920 the column comes up once the Workspace panel closes; the transcript is scrolled before it does.
	const { row } = await commentedMessage(page, request, 1920);
	await row.evaluate((el) => el.scrollIntoView({ block: 'center' }));
	await expect.poll(() => page.locator('.messages').evaluate((el) => el.scrollTop)).toBeGreaterThan(50);
	const frames = await sampleFirstFrames(page, { card: { card: '.chat-margin [data-margin-card]', mark: '.cm-comment-anchor', text: QUOTE } });
	await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
	await expect(page.locator('.chat-margin .chat-margin-card')).toBeVisible();
	await page.waitForTimeout(1000);
	expectFirstFrameSettled(await frames(), ['card']);
});

test('a document margin card first shows level with its line in a scrolled document', async ({ page, request }, info) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1' || info.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	// The reader closes the column, scrolls the document, then opens the column again from the comments control: it comes up
	// beside a document already scrolled, its first comment's line just below the top of the window. Only that card is
	// measured: the column's arrival narrows and rewraps the text, so the lines below the first paragraph move as it does.
	const doc = await open(page, request);
	await closeComments(page);
	await expect(page.locator('.canvas-comment-drawer.margin')).toHaveCount(0);
	const id = doc.ids[FIRST];
	const line = `.cm-comment-line[data-comment-ids*="${id}"]`;
	await page.locator('.cm-scroller').evaluate((el, selector) => {
		const mark = el.querySelector(selector)!.getBoundingClientRect().top;
		el.scrollTop = mark - el.getBoundingClientRect().top - 40;
	}, line);
	await expect.poll(() => page.locator('.cm-scroller').evaluate((el) => el.scrollTop), 'the document is scrolled').toBeGreaterThan(20);
	const frames = await sampleFirstFrames(page, { first: { card: `[data-margin-card="${id}"]`, mark: line } });
	await pill(page).click();
	await expectMarginColumnUp(page);
	await expect(page.locator(`[data-margin-card="${id}"]`)).toBeInViewport();
	await page.waitForTimeout(1000);
	expectFirstFrameSettled(await frames(), ['first']);
});
