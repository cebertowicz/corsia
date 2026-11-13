import { test } from '../support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { agentComment, reviewerSession } from '../support/liveConcurrency';
import { openLiveDocument, seedLiveDocument } from '../support/liveDocument';
import { expectDiscussionVisible, expectDiscussionReachableByScroll, expectNewestReplyWhole, expectIdleFieldUsable, expectNoCardScroller } from '../support/discussionVisible';
import { commentedMessage, QUOTE } from '../support/chatCommentedMessage';
import { closeCommentThread, openCommentThread } from '../support/chatComments';

const quote = 'target line for a new reply';
const text = Array.from({ length: 45 }, (_, i) => i === 25 ? quote : `document line ${i}`).join('\n\n');
const original = 'Existing thread. The narrow layout keeps the toolbar, but the comment column moves below the text and the reader loses the line each thread refers to.';
const newest = 'The newest reply must be read without scrolling.';
/** Two short paragraphs, so a comment of more than one block is covered too. */
const twoParagraphs = 'Short first paragraph.\n\nA second short paragraph that wraps onto a second line in a narrow card.';

/** Opens the seeded thread at a size and theme, replies until it holds a history, closes it and opens it again: new, nothing focused inside. */
async function openJustOpened(page: Page, request: APIRequestContext, width: number, height: number, theme: string, body: string) {
	await page.setViewportSize({ width, height });
	const doc = await seedLiveDocument(request, `new-reply-${width}-${height}-${theme}-${body}`, text);
	const reviewer = await reviewerSession(request, `new-reply-${width}-${height}-${theme}-${body}`);
	await agentComment(request, doc, reviewer.id, quote, body === 'two' ? twoParagraphs : original);
	await openLiveDocument(page, doc, 'document line 0');
	await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
	const scroller = page.locator('.cm-scroller');
	await scroller.evaluate(e => { e.scrollTop = e.scrollHeight * 0.4; });
	await expect(page.locator('.cm-line').filter({ hasText: quote })).toBeVisible();
	await scroller.evaluate(e => {
		const target = [...document.querySelectorAll<HTMLElement>('.cm-line')].find(n => n.textContent?.includes('target line for a new reply'))!;
		e.scrollTop += target.getBoundingClientRect().top - (e.getBoundingClientRect().bottom - 120);
	});
	const anchor = page.locator('.cm-comment-anchor').filter({ hasText: quote });
	await anchor.click();
	const input = page.getByPlaceholder('Reply…');
	const card = input.locator('xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), " ct ")][1]');
	const send = page.getByRole('button', { name: 'Send reply' });
	const replies = height <= 420 ? Array.from({ length: 12 }, (_, i) => `Reply ${i} with a sentence long enough to wrap onto a second line in a narrow card.`) : ['First reply to the note.', newest];
	for (const reply of replies) {
		await input.fill(reply);
		await send.click();
		await expect(card).toContainText(reply);
	}
	// Close the card, then open it again: it is new, with nothing focused inside it.
	await page.keyboard.press('Escape');
	await expect(page.getByRole('dialog', { name: 'Comment thread' })).toHaveCount(0);
	await anchor.click();
	await expect(card).toBeVisible();
	await expect(input).toBeVisible();
	await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
	return { card, input, last: replies[replies.length - 1] };
}

/** A card that has just opened shows its newest reply whole; one taller than its room is read by scrolling what holds it, never the card itself. */
for (const [width, height, project, body] of [[1920, 1080, 'desktop', 'one'], [1440, 900, 'desktop', 'one'], [900, 900, 'desktop', 'one'], [393, 852, 'mobile', 'one'], [320, 640, 'mobile', 'one'], [852, 393, 'mobile', 'one'], [1440, 420, 'desktop', 'one'], [393, 420, 'mobile', 'one'], [1440, 420, 'desktop', 'two'], [393, 420, 'mobile', 'two']] as const) for (const theme of ['light', 'dark'] as const) {
	test(`a just-opened thread shows its newest reply at ${width}x${height} ${theme}${body === 'two' ? ' with a two-paragraph comment' : ''}`, async ({ page, request }, info) => {
		test.skip(!info.project.name.endsWith(project));
		test.setTimeout(120_000);
		const { card, input, last } = await openJustOpened(page, request, width, height, theme, body);
		const tall = height <= 420;
		// The state as it opened, before the reachability check scrolls anything.
		await page.screenshot({ path: info.outputPath(`as-opened-${width}x${height}-${theme}${body === 'two' ? '-two-paragraphs' : ''}.png`), animations: 'disabled' });
		// Both checks run on the as-opened state, before the reachability check scrolls anything.
		await expectNoCardScroller(card);
		await expectNewestReplyWhole(card);
		if (tall) await expectIdleFieldUsable(input);
		if (tall) await expectDiscussionReachableByScroll(card, last);
		else await expectDiscussionVisible(card, last);
		await page.screenshot({ path: info.outputPath(`just-opened-${width}x${height}-${theme}${body === 'two' ? '-two-paragraphs' : ''}.png`), animations: 'disabled' });
	});
}

/**
 * Typing in a just-opened thread grows the field a line at a time without the card ever scrolling inside itself, and clearing then leaving the
 * field puts it back to its one idle line without a jump.
 */
for (const body of ['one', 'two'] as const) for (const theme of ['light', 'dark'] as const) {
	test(`typing in a just-opened thread at 1440x420 ${theme}${body === 'two' ? ' with a two-paragraph comment' : ''} grows the field without a card scroller and returns it to one line on clear and blur`, async ({ page, request }, info) => {
		test.skip(!info.project.name.endsWith('desktop'));
		test.setTimeout(180_000);
		const { card, input } = await openJustOpened(page, request, 1440, 420, theme, body);
		const suffix = `${theme}${body === 'two' ? '-two-paragraphs' : ''}`;
		// The field eases to each new height. Under load, frames are far enough apart that two reads 120 ms apart can return the
		// same height partway through the ease, so the height counts as settled only once the ease has also finished.
		const settled = async () => {
			let last = -1;
			await expect.poll(async () => {
				const { height, easing } = await input.evaluate(node => ({ height: node.getBoundingClientRect().height, easing: node.getAnimations().some(animation => animation.playState === 'running') }));
				const same = !easing && height === last; last = height; return same;
			}, { intervals: [120] }).toBe(true);
			return (await input.boundingBox())!.height;
		};
		const checkFrame = async (name: string) => {
			await expectNoCardScroller(card);
			await page.screenshot({ path: info.outputPath(`typing-${name}-${suffix}.png`), animations: 'disabled' });
		};
		const idle = await settled();
		// A click on the one-line field's centre focuses it.
		await input.click();
		await expect(input).toBeFocused();
		await checkFrame('focused-empty');
		const heights = [await settled()];
		const words = 'and a draft that keeps going so that the field has to wrap onto another line and then another one after that '.repeat(3).trim().split(' ');
		for (let i = 0; i < words.length; i++) {
			await input.pressSequentially(`${words[i]} `);
			const height = await settled();
			if (height !== heights[heights.length - 1]) { heights.push(height); await checkFrame(`lines-${heights.length}`); }
		}
		await checkFrame('long');
		await info.attach('field-heights', { body: JSON.stringify({ idle, heights }), contentType: 'application/json' });
		expect(heights.length, 'the field grew to a second line').toBeGreaterThan(1);
		// Cleared while focused: no jump; blurred: the field stays the height it was.
		await input.fill('');
		const cleared = await settled();
		await checkFrame('cleared-focused');
		await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
		const blurred = await settled();
		expect(blurred, 'the field height between clear and blur').toBe(cleared);
		expect(blurred, 'the field is back to its one line').toBe(idle);
		await expectNoCardScroller(card);
		await expectIdleFieldUsable(input);
		await page.screenshot({ path: info.outputPath(`typing-blurred-${suffix}.png`), animations: 'disabled' });
	});
}

/** The same rule for a chat comment: reopened after two replies, its card (margin card or popover) or sheet shows the newest reply whole. */
for (const [width, height, project] of [[1440, 900, 'desktop'], [900, 900, 'desktop'], [393, 852, 'mobile'], [1440, 420, 'desktop']] as const) for (const theme of ['light', 'dark'] as const) {
	test(`a just-opened chat thread shows its newest reply at ${width}x${height} ${theme}`, async ({ page, request }, info) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1' || !info.project.name.endsWith(project));
		// Known defect, cebertowicz/corsia#2565: in WebKit at phone width a click on a chat highlight opens no sheet at all (the open step fails, not the fit). Expected to fail until it is fixed; once it passes, this marker fails the run and comes out.
		test.fail(info.project.name === 'webkit-mobile' && width === 393, '#2565: a chat highlight does not open its sheet in WebKit mobile');
		test.setTimeout(120_000);
		const { row } = await commentedMessage(page, request, width);
		await page.setViewportSize({ width, height });
		await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
		await row.evaluate(el => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
		let thread = await openCommentThread(row, page, QUOTE);
		const send = thread.getByRole('button', { name: 'Send reply' });
		const tall = height === 420;
		const replies = tall ? Array.from({ length: 12 }, (_, i) => `Chat reply ${i} with a sentence long enough to wrap onto a second line in a card.`) : ['First chat reply to the note.', 'The newest chat reply must be read without scrolling.'];
		for (const reply of replies) {
			await thread.getByPlaceholder('Reply…').fill(reply);
			await send.click();
			await expect(thread).toContainText(reply);
		}
		await closeCommentThread(page, thread);
		thread = await openCommentThread(row, page, QUOTE);
		await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
		const last = replies[replies.length - 1];
		await expectNoCardScroller(thread);
		if (tall) await expectDiscussionReachableByScroll(thread, last);
		else await expectDiscussionVisible(thread, last);
		await page.screenshot({ path: info.outputPath(`chat-just-opened-${width}x${height}-${theme}.png`), animations: 'disabled' });
	});
}
