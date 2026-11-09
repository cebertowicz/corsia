import { expect, type Locator, type Page } from '@playwright/test';

/**
 * Chat comment surfaces: a comment or reply is written in a popover on a wide screen and a
 * drawer on a phone, and both run a close transition before the component clears its state.
 * These helpers wait for the observable end of that transition, so a spec never acts on the
 * transcript while the previous surface is still closing.
 */

/** The editor for a comment or reply, open on screen. */
export function commentEditor(page: Page, kind: 'Comment' | 'Reply' = 'Comment') {
	return page.getByPlaceholder(`${kind}…`);
}

/** The editor is gone: its close transition has finished and its state is cleared. */
export async function commentEditorClosed(page: Page, kind: 'Comment' | 'Reply' = 'Comment'): Promise<void> {
	await expect(commentEditor(page, kind)).toHaveCount(0);
}

/** The comment shows on the message as an unsent highlight carrying its quote. */
export async function commentPendingOnMessage(row: Locator, quote: string): Promise<void> {
	await expect(row.locator('.cm-comment-pending').filter({ hasText: quote })).toBeVisible();
}

/** The sent comment shows on the message as its highlight, and nothing is left unsent. */
export async function commentOnMessage(row: Locator, quote: string): Promise<void> {
	await expect(row.locator('.cm-comment-anchor').filter({ hasText: quote })).toBeVisible();
	await expect(row.locator('.cm-comment-pending')).toHaveCount(0);
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
	await mark.click();
	if (narrow) {
		const drawer = page.getByRole('dialog', { name: 'Comment thread' });
		await expect(drawer).toBeVisible();
		await expect(page.locator('[data-adaptive-drawer]')).toBeVisible();
		return drawer;
	}
	if (await page.locator('.chat-margin').count()) {
		const card = page.locator('.chat-margin .chat-margin-card');
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
