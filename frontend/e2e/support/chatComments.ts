import { expect, type Locator, type Page } from '@playwright/test';

/**
 * Chat comment surfaces: a comment or reply is written in a popover on a wide screen and a
 * drawer on a phone, and both run a close transition before the component clears its state.
 * These helpers wait for the observable end of that transition, so a spec never acts on the
 * transcript while the previous surface is still closing.
 */

/**
 * Every comment the person posted, and every reply they wrote in a comment thread, as the server's page read
 * carries them: on the commented rows' threads, since a comment's own input row never reaches chat.
 * Each has the server's `cid` and the `user_message_id` of the input that carried it.
 */
export function postedComments(rows: any[]): { cid: string; user_message_id: string; note?: string; text?: string }[] {
	return rows.flatMap((row) => (row.chat_comments ?? []).flatMap((comment: any) => [
		comment, ...(comment.answers ?? []).filter((answer: any) => answer.author_kind === 'user'),
	]));
}

/** The editor for a comment or reply, open on screen. */
export function commentEditor(page: Page, kind: 'Comment' | 'Reply' = 'Comment') {
	return page.getByPlaceholder(`${kind}…`);
}

/** The editor is gone: its close transition has finished and its state is cleared. */
export async function commentEditorClosed(page: Page, kind: 'Comment' | 'Reply' = 'Comment'): Promise<void> {
	await expect(commentEditor(page, kind)).toHaveCount(0);
}

/** The posted comment shows on the message as its highlight, and nothing is left unposted. */
export async function commentOnMessage(row: Locator, quote: string): Promise<void> {
	await expect(row.locator('.cm-comment-anchor').filter({ hasText: quote })).toBeVisible();
	await expect(row.locator('.cm-comment-pending')).toHaveCount(0);
}

/**
 * Wait until a highlight can take a click: it has stopped moving on both axes, is hit at its centre, and sits
 * above the composer. Opening a chat scrolls its transcript to the latest message, and a side pane closing
 * slides it sideways, so a click aimed at the highlight before then lands on the moved transcript instead.
 * A covered highlight is scrolled back to the middle of the transcript and read again.
 */
export async function settleHighlight(page: Page, mark: Locator): Promise<void> {
	const read = () => mark.evaluate((anchor) => {
		const box = anchor.getBoundingClientRect();
		const floater = document.querySelector('.input-floater')?.getBoundingClientRect();
		const hits = anchor.getClientRects().length ? document.elementsFromPoint(box.left + box.width / 2, box.top + box.height / 2) : [];
		const clear = hits.some((node) => node === anchor || anchor.contains(node)) && (!floater || box.bottom <= floater.top);
		if (!clear) anchor.scrollIntoView({ block: 'center' });
		return clear ? `settled:${Math.round(box.x)},${Math.round(box.y)}` : 'covered';
	});
	await expect.poll(async () => {
		const before = await read();
		await page.waitForTimeout(80);
		const after = await read();
		return before === after ? before : `moved:${before}>${after}`;
	}, { message: 'the highlight stopped moving and cleared the composer' }).toMatch(/^settled:-?\d+,-?\d+$/);
}

/**
 * Open a comment's thread from its highlight: the card in the transcript's margin column wherever the
 * chat canvas is wide enough for it, a popover on a narrower column, a drawer on a phone.
 * Returns the thread surface, so the caller can read what the thread holds.
 */
export async function openCommentThread(row: Locator, page: Page, quote: string): Promise<Locator> {
	const narrow = (page.viewportSize()?.width ?? 0) <= 768;
	const mark = row.locator('.cm-comment-anchor').filter({ hasText: quote });
	await expect(mark).toBeVisible();
	await settleHighlight(page, mark);
	await mark.click();
	if (narrow) {
		const drawer = page.getByRole('dialog', { name: 'Comment thread' });
		await expect(drawer).toBeVisible();
		await expect(page.locator('[data-adaptive-drawer]')).toBeVisible();
		return drawer;
	}
	if (await page.locator('.chat-margin').count()) {
		// The clicked highlight picks its own card, so a message with several comments returns the right one.
		const card = page.locator('.chat-margin .chat-margin-card.selected');
		await expect(card).toBeVisible();
		return card;
	}
	const thread = page.getByRole('dialog', { name: 'Comment thread', exact: true });
	await expect(thread).toBeVisible();
	return thread;
}

/**
 * Dismiss an open comment thread. On a phone the drawer is modal and keeps its
 * scrim over the composer, so a spec that goes on to write a message closes it
 * first; the wide-screen popover dismisses on its own when the next control is used,
 * and the first Escape lets a picked margin card go while the card stays with its text.
 */
export async function closeCommentThread(page: Page, thread: Locator): Promise<void> {
	if (await page.locator('[data-adaptive-drawer]').count()) await thread.getByRole('button', { name: 'Close', exact: true }).click();
	else await page.keyboard.press('Escape');
	await expect(page.getByRole('dialog', { name: 'Comment thread' })).toHaveCount(0);
	await expect(page.locator('.chat-margin .chat-margin-card.selected')).toHaveCount(0);
}
