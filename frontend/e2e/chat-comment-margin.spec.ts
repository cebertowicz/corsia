import { expect, test } from './support/test';
import type { APIRequestContext, Locator, Page } from '@playwright/test';
import { chatComposer, createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from './support/corsia';
import { injectMessage, seedMockResponses } from './support/mock';
import { commentEditor, commentEditorClosed, commentOnMessage, openCommentThread } from './support/chatComments';

/**
 * One comment, three surfaces, by the width of the chat canvas (design R5): its thread is a card in the
 * transcript's right margin beside the text it was cut from wherever the canvas is wide enough for the
 * column, a popover at the highlight when it is not, and the phone drawer on a phone. The column and its
 * threshold are the document's, so these flows are the chat half of that rule.
 */

const QUOTE = 'selected phrase';

/** Opens a chat of the given canvas width holding one commented message, and returns its row. */
async function commentedMessage(page: Page, request: APIRequestContext, width: number) {
	const title = `Margin comments ${width}`;
	const thread = await createSession(request, title);
	await injectMessage(request, thread, { role: 'assistant', content: 'Earlier context.\n\n'.repeat(24), metadata: { for_user: true } }, TEST_TOKEN);
	const id = await injectMessage(request, thread, { role: 'assistant', content: 'A selected phrase for a comment.', metadata: { for_user: true } }, TEST_TOKEN);
	await openAppPastLanding(page);
	await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
	await page.setViewportSize({ width: width <= 768 ? 393 : width, height: width <= 768 ? 852 : 1000 });
	const narrow = width <= 768;
	if (narrow) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, title).click();
	await waitForWsConnected(page);
	const row = page.locator(`[data-message-id="${id}"]`);
	await expect(row).toBeVisible();
	// Comment on the phrase the way a person does: select its text, then write on the selection menu.
	await row.evaluate((el) => {
		const body = el.querySelector('.long-message-body')!;
		const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
		let node = walker.nextNode() as Text | null;
		while (node && !node.data.includes('selected phrase')) node = walker.nextNode() as Text | null;
		if (!node) throw new Error('Selected text was not rendered');
		const start = node.data.indexOf('selected phrase');
		const range = document.createRange(); range.setStart(node, start); range.setEnd(node, start + 'selected phrase'.length);
		const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range);
	});
	await page.locator('.sel-comment').click();
	await commentEditor(page).fill('A note on the phrase');
	await page.getByRole('button', { name: 'Comment', exact: true }).click();
	await expect(page.getByRole('dialog', { name: 'New comment' })).toBeHidden();
	await commentEditorClosed(page);
	// The comment belongs to the message only once the message carrying it is delivered.
	await seedMockResponses(request, [{ content: 'Received comment' }]);
	await chatComposer(page).fill('Please address this');
	await page.getByRole('button', { name: 'Send message' }).click();
	await commentOnMessage(row, QUOTE);
	return { row, narrow };
}

/** Puts a row mid-transcript and waits for the margin stack to place its cards, so a card is measured against its own line. */
async function centerAndSettle(page: Page, row: Locator) {
	await row.evaluate((el) => el.scrollIntoView({ block: 'center' }));
	await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
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
	await expect(page.locator('.messages .message.user')).toHaveCount(1);

	// The first Escape lets the picked card go; the card itself stays beside its text.
	await card.click();
	await page.keyboard.press('Escape');
	await expect(card).toHaveCount(1);
	await expect(card).not.toHaveClass(/selected/);
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
