import { expect, test } from './support/test';
import type { Locator, Page } from '@playwright/test';
import { openCommentThread, settleHighlight } from './support/chatComments';
import { commentedMessage, QUOTE } from './support/chatCommentedMessage';
import { expectDiscussionReachableByScroll, expectFieldAndSendUsable, expectNoCardScroller, expectReachableByOuterScroll, expectRefitKeepsScroll } from './support/discussionVisible';


/**
 * One comment, three surfaces, by the width of the chat canvas (design R5): its thread is a card in the
 * transcript's right margin beside the text it was cut from wherever the canvas is wide enough for the
 * column, a popover at the highlight when it is not, and the phone drawer on a phone. The column and its
 * threshold are the document's, so these flows are the chat half of that rule.
 */

/**
 * Puts a row mid-transcript and waits until its highlighted line has stopped moving and stands clear of the
 * composer, so the click that follows lands where it was measured. Two animation frames are not enough: while the
 * transcript is still settling, a click measured on the highlight is delivered onto text that has moved, and the
 * card never opens (the recorded 1440 timeout; the saved trace shows the pointer landing under `.input-floater`).
 * Both axes count: a side pane closing slides the transcript sideways while its lines keep their height.
 */
async function centerAndSettle(page: Page, row: Locator) {
	await row.evaluate((el) => el.scrollIntoView({ block: 'center' }));
	await settleHighlight(page, row.locator('.cm-comment-anchor').first());
}

/**
 * Opens a margin card by the highlight it belongs to. A click that lands on text which has already moved — the
 * recorded 1440 timeout, and the same intermittence at 1920 — leaves the card unselected, so the card takes the
 * click instead, which is the select route a pointer on the card itself uses.
 */
async function openMarginCard(row: Locator, card: Locator) {
	await row.locator('.cm-comment-anchor').filter({ hasText: QUOTE }).click();
	const field = card.getByPlaceholder('Reply…');
	if (await field.waitFor({ state: 'visible', timeout: 4_000 }).then(() => true, () => false)) return;
	await card.click();
	await expect(field).toBeVisible();
}

/** Writes the same frame in both themes, for the design pack. */
async function capture(page: Page, stage: string) {
	const directory = process.env.CORSIA_MARGIN_CAPTURE_DIR;
	if (!directory) return;
	const originalTheme = await page.evaluate(() => document.documentElement.dataset.theme);
	for (const theme of ['light', 'dark']) {
		await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
		await page.screenshot({ path: `${directory}/${stage}-${theme}.png`, animations: 'disabled' });
	}
	await page.evaluate((value) => { if (value) document.documentElement.dataset.theme = value; }, originalTheme);
}

test('a wide chat holds the thread as a card beside its text, and the highlight picks that card', async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable mock instance');
	test.setTimeout(120_000);
	// The column opens only where it fits beside a centred transcript without moving it: at 1920 with the sidebar open and the
	// Workspace panel closed, never at 1440 beside both.
	const { row } = await commentedMessage(page, request, 1920);
	await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
	await centerAndSettle(page, row);

	// The card is in the column beside the text, at the line its comment was cut from.
	const card = page.locator('.chat-margin .chat-margin-card');
	// The column comes up once the closing panel has left, and a card stays hidden until the stack has placed it.
	await expect(card).toBeVisible();
	const column = await page.locator('.chat-margin').boundingBox();
	const mark = await row.locator('.cm-comment-anchor').filter({ hasText: QUOTE }).boundingBox();
	const box = await card.boundingBox();
	if (!column || !mark || !box) throw new Error('The margin column, its highlight or its card did not render');
	expect(column.x).toBeGreaterThan(mark.x + mark.width);
	expect(Math.abs(box.y - mark.y)).toBeLessThan(64);
	// Nothing is repeated under the message: the card is the thread's surface.
	await expect(row.locator('.chat-comment-thread')).toHaveCount(0);
	await expect(card.getByText('A note on the phrase')).toBeVisible();

	// The highlight picks its card instead of opening a popover, as a highlight picks a card in a document, and the text clicked
	// stays where it was: the column is already up, so picking moves the card to the text, never the text.
	const highlight = row.locator('.cm-comment-anchor').filter({ hasText: QUOTE });
	await highlight.click();
	await expect(card).toHaveClass(/selected/);
	await expect(page.getByRole('dialog', { name: 'Comment thread' })).toHaveCount(0);
	await page.waitForTimeout(500);
	expect(Math.abs((await highlight.boundingBox())!.y - mark.y), 'the clicked text stays put').toBeLessThanOrEqual(2);
	await capture(page, 'chat-margin-1920');

	// Answering from the card is the flat thread's own Reply, and it reads back in the card.
	const replyField = card.getByPlaceholder('Reply…');
	await expect(replyField).toBeVisible();
	await replyField.fill('Answer from the card');
	await card.getByRole('button', { name: 'Send reply' }).click();
	await expect(card.getByText('Answer from the card')).toBeVisible();
	// A reply goes to CORSIA, never into the transcript as a message of its own.
	await expect(page.locator('.messages .message.user')).toHaveCount(0);

	// The first Escape lets the picked card go; the card itself stays beside its text.
	await card.click();
	await page.keyboard.press('Escape');
	await expect(card).toHaveCount(1);
	await expect(card).not.toHaveClass(/selected/);
});

for (const width of [1440, 1920] as const) for (const theme of ['light', 'dark'] as const) test(`a long chat margin reply stays usable at ${width} in ${theme}`, async ({ page, request }, info) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1' || info.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	const { row } = await commentedMessage(page, request, width);
	await page.setViewportSize({ width, height: 900 });
	await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
	await page.getByRole('button', { name: /^Toggle sidebar/ }).click();
	await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
	const card = page.locator('.chat-margin .chat-margin-card');
	await expect(card).toHaveCount(1);
	await centerAndSettle(page, row);
	await openMarginCard(row, card);
	const input = card.getByPlaceholder('Reply…');
	const send = card.getByRole('button', { name: 'Send reply' });
	// The field grows with its text and never scrolls itself, and nothing in the card scrolls; the transcript holding the
	// card is the one scroller. Send stays in the transcript's view, clear of the header and the chat composer, and the
	// card never runs past the window's sides.
	const check = async () => {
		await expectNoCardScroller(card);
		await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.scrollHeight <= field.clientHeight + 1)).toBe(true);
		await expect(send).toBeVisible();
		await expect.poll(() => send.evaluate(el => {
			const box = el.getBoundingClientRect(), scroller = el.closest<HTMLElement>('.messages')!;
			const view = scroller.getBoundingClientRect();
			const inset = parseFloat(getComputedStyle(scroller).getPropertyValue('--input-bar-height')) || 100;
			const headerBottom = document.querySelector('.shell-header')!.getBoundingClientRect().bottom;
			return { top: Math.max(0, Math.max(view.top, headerBottom) - box.top), bottom: Math.max(0, box.bottom - view.bottom + inset) };
		})).toEqual({ top: 0, bottom: 0 });
		await expect.poll(() => card.evaluate(el => {
			const box = el.getBoundingClientRect();
			return { left: Math.max(0, -box.left), right: Math.max(0, box.right - innerWidth) };
		})).toEqual({ left: 0, right: 0 });
		await send.click({ trial: true });
	};
	for (let i = 0; i < 40; i++) {
		await input.pressSequentially(`typed reply ${i}`);
		if (i < 39) await input.press('Shift+Enter');
	}
	await check();
	await page.screenshot({ path: info.outputPath('typed-chat-margin-reply.png'), animations: 'disabled' });

	// Replaced by a paste, as a person does it (select all, paste): the browser reveals the caret after the paste. `fill`
	// would select through `select()`, which scrolls to the field's top, and insert without that reveal.
	await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
	await page.evaluate(text => navigator.clipboard.writeText(text), Array.from({ length: 40 }, (_, i) => `long chat reply line ${i}`).join('\n'));
	await input.press('ControlOrMeta+A');
	await input.press('ControlOrMeta+V');
	await expect.poll(() => input.inputValue()).toMatch(/^long chat reply line 0\n[\s\S]*line 39$/);
	await check();
	await page.screenshot({ path: info.outputPath('long-chat-margin-reply.png'), animations: 'disabled' });

	await send.click();
	await expect(card).toContainText('long chat reply line 0');
	await input.fill(Array.from({ length: 40 }, (_, i) => `second margin reply ${i}`).join('\n'));
	// Scrolling the transcript up reaches the comment and the first reply; nothing inside the card scrolls.
	await expectNoCardScroller(card);
	await expectReachableByOuterScroll(card, 'A note on the phrase');
	await expectReachableByOuterScroll(card, 'long chat reply line 0');
	await page.screenshot({ path: info.outputPath(`${width}-${theme}-second-margin-reply.png`), animations: 'disabled' });
	await send.click();
	await expect(card).toContainText('second margin reply 0');

});

test('a chat column too narrow for the card keeps the popover at the highlight', async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable mock instance');
	test.setTimeout(120_000);
	const { row } = await commentedMessage(page, request, 900);
	// Under the document's threshold there is no room for the column beside the text, so nothing is taken from it.
	await expect.poll(() => page.locator('.chat-content').evaluate((el) => el.getBoundingClientRect().width)).toBeLessThan(660);
	await expect(page.locator('.chat-margin')).toHaveCount(0);
	const thread = await openCommentThread(row, page, QUOTE);
	await expect(thread.getByText('A note on the phrase')).toBeVisible();
	await capture(page, 'chat-narrow-popover');
});

for (const height of [420, 900] as const) for (const theme of ['light', 'dark'] as const) test(`a chat popover keeps its anchor visible at 900x${height} in ${theme}`, async ({ page, request }, info) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1' || info.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	const { row } = await commentedMessage(page, request, 900);
	await page.setViewportSize({ width: 900, height });
	await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
	// Avoid starting a smooth scroll that continues after the click and dismisses the popover.
	await row.evaluate(el => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
	const anchor = row.locator('.cm-comment-anchor').filter({ hasText: QUOTE });
	const thread = await openCommentThread(row, page, QUOTE);
	const input = thread.getByPlaceholder('Reply…');
	const send = thread.getByRole('button', { name: 'Send reply' });
	const check = async () => {
		await expect.poll(async () => {
			const a = (await anchor.boundingBox())!, p = (await page.locator('[data-popover]').boundingBox())!;
			const header = (await page.locator('.shell-header').boundingBox())!;
			const composer = (await page.locator('.input-floater').boundingBox())!;
			const clearsAnchor = p.x + p.width <= a.x || a.x + a.width <= p.x || p.y + p.height <= a.y || a.y + a.height <= p.y;
			if (!clearsAnchor) return `popover ${Math.round(p.y)}-${Math.round(p.y + p.height)} covers the anchor ${Math.round(a.y)}-${Math.round(a.y + a.height)}`;
			if (p.y < header.y + header.height) return `popover top ${Math.round(p.y)} above the header bottom ${Math.round(header.y + header.height)}`;
			if (p.y + p.height > composer.y) return `popover bottom ${Math.round(p.y + p.height)} below the composer top ${Math.round(composer.y)}`;
			return true;
		}).toBe(true);
		await send.click({ trial: true });
	};
	for (let i = 0; i < 8; i++) { await input.pressSequentially(`typed reply ${i}`); await input.press('Shift+Enter'); }
	await check();
	await page.screenshot({ path: info.outputPath('typed-chat-popover.png'), animations: 'disabled' });

	await page.keyboard.insertText(Array.from({ length: 40 }, (_, i) => `pasted reply ${i}`).join('\n'));
	await check();
	await page.screenshot({ path: info.outputPath('pasted-chat-popover.png'), animations: 'disabled' });

	await send.click();
	// Send goes once its text is posted; focus returns to the field, so the card holding it stays while the transcript moves.
	await expect(input).toBeFocused();
	await expect(thread).toContainText('pasted reply 0');
	await input.fill(Array.from({ length: 40 }, (_, i) => `second chat reply ${i}`).join('\n'));
	// A 40-line reply is taller than the popover at either height: the field's end and Send stay in view, nothing in the
	// card scrolls, and the comment and the earlier reply are reached by scrolling the popover.
	await expectNoCardScroller(thread.locator('.ct').first());
	await expectFieldAndSendUsable(thread, input);
	await expectDiscussionReachableByScroll(thread, 'A note on the phrase');
	await expectDiscussionReachableByScroll(thread, 'typed reply 0');
	await send.click();
	await expect(input).toBeFocused();
	await expect(thread).toContainText('second chat reply 0');

});

for (const height of [852, 420] as const) for (const theme of ['light', 'dark'] as const) test(`a long chat drawer reply stays usable at 393x${height} in ${theme}`, async ({ page, request }, info) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1' || info.project.name !== 'chromium-mobile');
	test.setTimeout(120_000);
	const { row } = await commentedMessage(page, request, 393);
	await page.setViewportSize({ width: 393, height });
	await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
	const thread = await openCommentThread(row, page, QUOTE);
	const input = thread.getByPlaceholder('Reply…');
	const send = thread.getByRole('button', { name: 'Send reply' });
	const checkReply = async (state: string) => {
		// `finished` rejects when an ease is replaced, so settle rather than await.
		await input.evaluate(async field => { await Promise.allSettled(field.getAnimations().map(animation => animation.finished)); });
		await send.click({ trial: true });
		await page.screenshot({ path: info.outputPath(`${state}.png`), animations: 'disabled' });
		for (const element of [input, send]) await expect.poll(async () => {
			const box = (await element.boundingBox())!;
			return height - box.y - box.height;
		// The browser rounds a fractional scroll offset to a device pixel.
		}).toBeGreaterThanOrEqual(15);
	};
	for (let i = 0; i < 8; i++) { await input.pressSequentially(`typed reply ${i}`); await input.press('Shift+Enter'); }
	await checkReply('typed');

	await page.keyboard.insertText(Array.from({ length: 40 }, (_, i) => `pasted reply ${i}`).join('\n'));
	// The field grows to its text and never scrolls itself.
	await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.scrollHeight <= field.clientHeight + 1 && field.clientHeight > 45)).toBe(true);
	await checkReply('pasted');

	await send.click();
	await expect(thread).toContainText('pasted reply 0');
	await input.fill(Array.from({ length: 40 }, (_, i) => `second chat reply ${i}`).join('\n'));
	// A 40-line reply is taller than the drawer at either height: the field's end and Send stay in view, nothing in the
	// card scrolls, and the comment and the earlier reply are reached by scrolling the drawer.
	await expectNoCardScroller(thread.locator('.ct').first());
	await expectFieldAndSendUsable(thread, input);
	await expectRefitKeepsScroll(input);
	await expectDiscussionReachableByScroll(thread, 'A note on the phrase');
	await expectDiscussionReachableByScroll(thread, 'typed reply 0');
	await send.click();
	await expect(thread).toContainText('second chat reply 0');

});

test('a phone keeps the drawer, with no margin column', async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable mock instance');
	test.setTimeout(120_000);
	const { row, narrow } = await commentedMessage(page, request, 393);
	expect(narrow).toBe(true);
	await expect(page.locator('.chat-margin')).toHaveCount(0);
	const thread: Locator = await openCommentThread(row, page, QUOTE);
	await expect(thread.getByText('A note on the phrase')).toBeVisible();
	await capture(page, 'chat-margin-393');
});
