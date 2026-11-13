import { test } from '../support/test';
import { expect } from '@playwright/test';
import { agentComment, reviewerSession } from '../support/liveConcurrency';
import { openLiveDocument, seedLiveDocument } from '../support/liveDocument';
import { expectDiscussionVisible, expectDiscussionReachableByScroll, expectRegionsApart, expectNewestReplyWhole } from '../support/discussionVisible';
import { commentedMessage, QUOTE } from '../support/chatCommentedMessage';
import { closeCommentThread, openCommentThread } from '../support/chatComments';

const quote = 'target line for a new reply';
const text = Array.from({ length: 45 }, (_, i) => i === 25 ? quote : `document line ${i}`).join('\n\n');
const original = 'Existing thread. The narrow layout keeps the toolbar, but the comment column moves below the text and the reader loses the line each thread refers to.';
const newest = 'The newest reply must be read without scrolling the card.';
/** Two short paragraphs: the gap between them is not a line, so a cap counted in lines can fall inside the second paragraph's first line. */
const twoParagraphs = 'Short first paragraph.\n\nA second short paragraph that wraps onto a second line in a narrow card.';

/** A card that has just opened shows its newest reply whole; one taller than its room scrolls inside itself with that reply reachable. */
for (const [width, height, project, body] of [[1440, 900, 'desktop', 'one'], [900, 900, 'desktop', 'one'], [393, 852, 'mobile', 'one'], [1440, 420, 'desktop', 'one'], [393, 420, 'mobile', 'one'], [1440, 420, 'desktop', 'two'], [393, 420, 'mobile', 'two']] as const) for (const theme of ['light', 'dark'] as const) {
	test(`a just-opened thread shows its newest reply at ${width}x${height} ${theme}${body === 'two' ? ' with a two-paragraph comment' : ''}`, async ({ page, request }, info) => {
		test.skip(!info.project.name.endsWith(project));
		test.setTimeout(120_000);
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
		const tall = height === 420;
		const replies = tall ? Array.from({ length: 12 }, (_, i) => `Reply ${i} with a sentence long enough to wrap onto a second line in a narrow card.`) : ['First reply to the note.', newest];
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
		const last = replies[replies.length - 1];
		// The state as it opened, before the reachability check scrolls anything.
		await page.screenshot({ path: info.outputPath(`as-opened-${width}x${height}-${theme}${body === 'two' ? '-two-paragraphs' : ''}.png`), animations: 'disabled' });
		// Both checks run on the as-opened state, before the reachability check scrolls anything.
		await expectRegionsApart(card);
		await expectNewestReplyWhole(card);
		if (tall) await expectDiscussionReachableByScroll(card, last);
		else await expectDiscussionVisible(card, last);
		const clipped = await card.evaluate(el => [...el.querySelectorAll<HTMLElement>('*')].filter(n => /(auto|scroll)/.test(getComputedStyle(n).overflowY) && n.scrollHeight > n.clientHeight).map(n => `${n.className} sh=${n.scrollHeight} ch=${n.clientHeight}`));
		await info.attach('scrollers', { body: JSON.stringify({ surface: await card.evaluate(el => el.closest('[data-popover]') ? 'popover' : 'drawer'), clipped }), contentType: 'application/json' });
		await page.screenshot({ path: info.outputPath(`just-opened-${width}x${height}-${theme}${body === 'two' ? '-two-paragraphs' : ''}.png`), animations: 'disabled' });
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
		if (tall) await expectDiscussionReachableByScroll(thread, last);
		else await expectDiscussionVisible(thread, last);
		await page.screenshot({ path: info.outputPath(`chat-just-opened-${width}x${height}-${theme}.png`), animations: 'disabled' });
	});
}
