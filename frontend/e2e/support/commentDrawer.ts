import { expect, type Locator } from '@playwright/test';

/** Returns the reply field that every writable open thread keeps visible. */
export async function replyField(thread: Locator): Promise<Locator> {
	const field = thread.getByPlaceholder('Reply…');
	await expect(field).toBeVisible();
	return field;
}

/**
 * Everything the drawer can show. An image's drawer opens on Open, where a thread that is accepted or resolved leaves the list;
 * its filter set to All keeps it in view. A text document's drawer has no filter: its open threads are always listed and its
 * resolved ones sit under the "Resolved" entry, which this opens.
 */
export async function showAllThreads(drawer: Locator): Promise<void> {
	const filter = drawer.locator('[data-drawer-filter]');
	if (!(await filter.count())) {
		await showResolved(drawer);
		return;
	}
	await filter.click();
	await drawer.getByRole('menu', { name: 'Filter comments', exact: true }).getByRole('menuitemradio', { name: 'All', exact: true }).click();
	await expect(filter).toHaveAttribute('aria-label', 'Filter comments: All');
	// The old list crossfades out beside the new one; wait until only one is left so a thread is never found twice.
	await expect(drawer.locator('.peers > .peer')).toHaveCount(1);
}

/** The "Resolved" entry opened, when the document has resolved threads. */
export async function showResolved(drawer: Locator): Promise<void> {
	const entry = drawer.locator('[data-aside-kind="resolved"]');
	if ((await entry.count()) && (await entry.getAttribute('aria-expanded')) !== 'true') await entry.click();
}

/**
 * Runs `check` with the "Resolved" list open, then closes it again. A decided suggestion and a resolved thread leave the open
 * cards for that list, which covers the margin while it is open, so a spec that goes on to press a card button must close it first.
 */
export async function inResolved<T>(drawer: Locator, check: () => Promise<T>): Promise<T> {
	const entry = drawer.locator('[data-aside-kind="resolved"]');
	await expect(entry).toBeVisible();
	const opened = (await entry.getAttribute('aria-expanded')) !== 'true';
	if (opened) await entry.click();
	try {
		return await check();
	} finally {
		if (opened && (await entry.getAttribute('aria-expanded')) === 'true') await entry.click();
	}
}

/** The "Resolved" list opened and left open, waiting for its entry to appear first (a thread has just been resolved). */
export async function openResolved(drawer: Locator): Promise<void> {
	const entry = drawer.locator('[data-aside-kind="resolved"]');
	await expect(entry).toBeVisible();
	if ((await entry.getAttribute('aria-expanded')) !== 'true') await entry.click();
	await expect(entry).toHaveAttribute('aria-expanded', 'true');
}
