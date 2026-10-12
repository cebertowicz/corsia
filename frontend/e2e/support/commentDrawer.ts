import { expect, type Locator } from '@playwright/test';

/**
 * A drawer thread keeps its reply field closed until its person asks for it (the thread's Reply action, or the
 * thread being selected). Returns the open field, pressing Reply first when it is still closed.
 */
export async function replyField(thread: Locator): Promise<Locator> {
	const field = thread.getByPlaceholder('Reply…');
	if (!(await field.isVisible())) await thread.getByRole('button', { name: 'Reply', exact: true }).click();
	return field;
}

/** The drawer opens on Open, where a thread that is accepted or resolved leaves the list; All keeps it in view. */
export async function showAllThreads(drawer: Locator): Promise<void> {
	const filter = drawer.locator('[data-drawer-filter]');
	await filter.click();
	await drawer.getByRole('menu', { name: 'Filter comments', exact: true }).getByRole('menuitemradio', { name: 'All', exact: true }).click();
	await expect(filter).toHaveAttribute('aria-label', 'Filter comments: All');
	// The old list crossfades out beside the new one; wait until only one is left so a thread is never found twice.
	await expect(drawer.locator('.peers > .peer')).toHaveCount(1);
}
