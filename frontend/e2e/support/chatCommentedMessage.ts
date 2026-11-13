import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from './corsia';
import { injectMessage, seedMockResponses, waitForThreadReply } from './mock';
import { commentEditor, commentEditorClosed, commentOnMessage } from './chatComments';

export const QUOTE = 'selected phrase';

/** Opens a chat of the given canvas width holding one commented message, and returns its row. */
export async function commentedMessage(page: Page, request: APIRequestContext, width: number) {
	const title = `Margin comments ${width} ${crypto.randomUUID()}`;
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
	// The comment posts from its own box and starts the Turn that answers it; it never becomes a chat message.
	await seedMockResponses(request, [{ content: 'Received comment' }]);
	await page.getByRole('button', { name: 'Comment', exact: true }).click();
	await expect(page.getByRole('dialog', { name: 'New comment' })).toBeHidden();
	await commentEditorClosed(page);
	await commentOnMessage(row, QUOTE);
	await expect(page.locator('.messages .message.user')).toHaveCount(0);
	await waitForThreadReply(request, thread, 'Received comment');
	return { row, narrow };
}
