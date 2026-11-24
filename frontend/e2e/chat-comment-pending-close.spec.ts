import { expect, type Page } from '@playwright/test';
import { test } from './support/test';
import { commentedMessage, QUOTE } from './support/chatCommentedMessage';
import { createSession, sidebarThread } from './support/corsia';
import { commentEditor, openCommentThread, settleHighlight } from './support/chatComments';

/**
 * A close that began on a thread popover must not outlive the popover. When the margin column comes up while the
 * popover is still leaving, the popover is removed without its exit ever finishing; the next surface must not run
 * that close and clear what the person is typing. Real browser only: jsdom ends every exit at once.
 */
const frames = (page: Page, name: string) => {
	const dir = process.env.CORSIA_CHAT_LEAVE_FRAMES;
	return dir ? page.screenshot({ path: `${dir}/${name}.png`, animations: 'disabled' }) : Promise.resolve(undefined);
};

async function selectText(page: Page, rowId: string, text: string) {
	await page.locator(`[data-message-id="${rowId}"]`).evaluate((el, wanted) => {
		const walker = document.createTreeWalker(el.querySelector('.long-message-body')!, NodeFilter.SHOW_TEXT);
		let node = walker.nextNode() as Text | null;
		while (node && !node.data.includes(wanted)) node = walker.nextNode() as Text | null;
		if (!node) throw new Error('text not found');
		const start = node.data.indexOf(wanted);
		const range = document.createRange(); range.setStart(node, start); range.setEnd(node, start + wanted.length);
		const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range);
	}, text);
}

type Way = 'panel' | 'escape-then-panel' | 'escape-then-resize' | 'switch-and-back';
for (const width of [1440, 1920]) for (const way of ['panel', 'escape-then-panel', 'escape-then-resize', 'switch-and-back'] as Way[]) {
	if (way === 'escape-then-resize' && width !== 1440) continue;
	test(`a pending close does not clear the next comment at ${width} (${way})`, async ({ page, request }, info) => {
		test.skip(info.project.name !== 'chromium-desktop');
		test.setTimeout(120_000);
		const other = `Other chat ${crypto.randomUUID()}`;
		await createSession(request, other);
		const { row, title } = await commentedMessage(page, request, width);
		const id = await row.getAttribute('data-message-id');
		await row.evaluate(el => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
		// The thread opens in a popover while the side panel narrows the transcript.
		const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
		// The resize leg starts with the panel closed on a canvas too narrow for the column, then widens the window mid-exit.
		if (way === 'escape-then-resize') await toggle.click();
		const hadColumn = await page.locator('.chat-margin').count();
		await openCommentThread(row, page, QUOTE);
		if (way === 'switch-and-back') {
			await sidebarThread(page, other).click();
			await expect(row).toHaveCount(0);
			await sidebarThread(page, title).click();
			await expect(row).toBeVisible();
		} else {
			if (way === 'escape-then-panel' || way === 'escape-then-resize') await page.keyboard.press('Escape');
			// Real press: pointerdown dismisses the popover, and the click lands while it is still leaving.
			if (way === 'escape-then-resize') await page.setViewportSize({ width: 1920, height: 1000 });
			else await toggle.click();
		}
		if ((width === 1920 || way === 'escape-then-resize') && !hadColumn && way !== 'switch-and-back') await expect(page.locator('.chat-margin')).toHaveCount(1);
		await row.evaluate(el => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
		const mark = row.locator('.cm-comment-anchor').filter({ hasText: QUOTE });
		await settleHighlight(page, mark);
		// A highlight press picks its card where the column is up, and opens its thread where it is not.
		const column = (await page.locator('.chat-margin').count()) > 0;
		const picked = page.locator('.chat-margin .chat-margin-card.selected');
		const thread2 = page.getByRole('dialog', { name: 'Comment', exact: true });
		if (column) {
			// Whatever handed the thread to its card, the card is let go first, so the press below is what picks it.
			await page.keyboard.press('Escape');
			await expect(picked).toHaveCount(0);
		}
		await mark.click();
		await expect(column ? picked : thread2).toBeVisible();
		await page.keyboard.press('Escape');
		// The surface is gone, exit included, before the next comment is begun.
		await expect(page.getByRole('dialog', { name: 'Comment', exact: true })).toHaveCount(0);
		await expect(page.locator('.chat-margin .chat-margin-card.selected')).toHaveCount(0);
		await settleHighlight(page, mark);
		// Write a new comment on other text and keep typing past the exit duration.
		await expect(async () => {
			await selectText(page, id!, 'for a comment');
			await page.locator('.sel-comment').click({ timeout: 3000 });
		}).toPass({ timeout: 30_000 });
		const editor = commentEditor(page);
		await editor.fill('A draft that must survive');
		await page.waitForTimeout(1500);
		try {
			await expect(editor).toHaveValue('A draft that must survive');
		} finally {
			await frames(page, `pending-close-${width}-${way}`);
		}
	});
}

test('a plain-text press after a pending close releases the picked card', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	const { row } = await commentedMessage(page, request, 1920);
	await row.evaluate(el => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
	await openCommentThread(row, page, QUOTE);
	expect(await page.locator('.chat-margin').count()).toBe(0);
	await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
	await row.evaluate(el => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
	const mark = row.locator('.cm-comment-anchor').filter({ hasText: QUOTE });
	await settleHighlight(page, mark);
	await mark.click();
	const card = page.locator('.chat-margin-card').filter({ hasText: 'A note on the phrase' });
	await expect(card).toHaveClass(/selected/);
	const box = await mark.boundingBox();
	if (!box) throw new Error('highlight missing');
	await page.mouse.click(box.x - 8, box.y + box.height / 2);
	await expect(card).not.toHaveClass(/selected/);
});
