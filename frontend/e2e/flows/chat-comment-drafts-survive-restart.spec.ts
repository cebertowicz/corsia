import { test } from '../support/test';
import { expect, type Locator, type Page } from '@playwright/test';
import { openAppPastLanding, sidebarThread, waitForWsConnected } from '../support/corsia';
import { commentedMessage, QUOTE } from '../support/chatCommentedMessage';
import { commentEditor, openCommentThread, settleHighlight } from '../support/chatComments';

// An unsent chat comment or reply is kept on this device, as a document's is: it is still there after a reload, and after the
// app is quit and opened again (a new page over the same storage stands in for that, and for an update restart). Taken at
// 1440 (chromium-desktop) and at 393 (chromium-mobile, by touch). Sign out, the one client id and the failure states are
// proven by the store's unit tests (chatCommentDrafts.device.test.ts).

const SIZES = { 'chromium-desktop': { width: 1440, height: 1000 }, 'chromium-mobile': { width: 393, height: 852 } } as const;
type Project = keyof typeof SIZES;
const phone = (page: Page) => page.viewportSize()!.width <= 768;

const PASSAGE = 'for a comment';
const PREFIX = 'corsia:commentDraft:v1:';
/** The kept drafts' bodies on this device, as the page wrote them. */
const stored = (page: Page) => page.evaluate((prefix) => Object.keys(localStorage).filter((name) => name.startsWith(prefix))
	.map((name) => (JSON.parse(localStorage.getItem(name)!) as { body: string }).body), PREFIX);

type WayOut = 'reload' | 'relaunch';
/**
 * The page is left and the app opened again on the same chat. `reload` reloads at once, with no wait for the draft to be
 * written. `relaunch` closes the page, once the draft is on the device, and opens a new one over the same storage.
 */
async function leave(page: Page, way: WayOut, title: string, rowId: string): Promise<{ page: Page; row: Locator }> {
	let next = page;
	if (way === 'reload') {
		await page.reload();
	} else {
		await expect.poll(() => stored(page), { message: 'the draft is written to the device' }).not.toEqual([]);
		next = await page.context().newPage();
		await next.setViewportSize(page.viewportSize()!);
		await page.close();
	}
	await openAppPastLanding(next);
	await next.addStyleTag({ content: '.version-overlay { display: none !important; }' });
	if (phone(next)) await next.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(next, title).click();
	await waitForWsConnected(next);
	const row = next.locator(`[data-message-id="${rowId}"]`);
	await expect(row).toBeVisible();
	await row.evaluate((el) => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
	return { page: next, row };
}

/** Select a passage of the message's text, as a person does before pressing Comment on the selection menu. */
async function select(row: Locator, wanted: string) {
	await row.evaluate((el, text) => {
		const walker = document.createTreeWalker(el.querySelector('.long-message-body')!, NodeFilter.SHOW_TEXT);
		let node = walker.nextNode() as Text | null;
		while (node && !node.data.includes(text)) node = walker.nextNode() as Text | null;
		if (!node) throw new Error('Selected text was not rendered');
		const start = node.data.indexOf(text);
		const range = document.createRange(); range.setStart(node, start); range.setEnd(node, start + text.length);
		const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range);
	}, wanted);
}

test.describe('@flow:chat-comments an unsent chat comment outlives the page', () => {
	test.beforeEach(async ({ page }, testInfo) => {
		test.skip(!(testInfo.project.name in SIZES), 'Taken at 1440 and 393 only');
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		test.setTimeout(150_000);
		await page.setViewportSize(SIZES[testInfo.project.name as Project]);
	});

	for (const way of ['reload', 'relaunch'] as const) {
		test(`a new chat comment being typed is there again after a ${way}`, async ({ page, request }) => {
			const { row, title } = await commentedMessage(page, request, page.viewportSize()!.width);
			const rowId = (await row.getAttribute('data-message-id'))!;
			await select(row, PASSAGE);
			await page.locator('.sel-comment').click();
			await commentEditor(page).fill('Which phrase did you mean?');
			const left = await leave(page, way, title, rowId);
			await select(left.row, PASSAGE);
			await left.page.locator('.sel-comment').click();
			await expect(commentEditor(left.page), 'the text typed before is there').toHaveValue('Which phrase did you mean?');
		});

		test(`a chat reply being typed is there again after a ${way}`, async ({ page, request }) => {
			const { row, title } = await commentedMessage(page, request, page.viewportSize()!.width);
			const rowId = (await row.getAttribute('data-message-id'))!;
			await row.evaluate((el) => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
			const thread = await openCommentThread(row, page, QUOTE, { tap: phone(page) });
			await thread.getByPlaceholder('Reply…').fill('Thanks, one more question');
			const left = await leave(page, way, title, rowId);
			await settleHighlight(left.page, left.row.locator('.cm-comment-anchor').filter({ hasText: QUOTE }));
			const reopened = await openCommentThread(left.row, left.page, QUOTE, { tap: phone(left.page) });
			await expect(reopened.getByPlaceholder('Reply…'), 'the reply typed before is there').toHaveValue('Thanks, one more question');
		});
	}
});
