import { expect, type Locator, type Page } from '@playwright/test';

/** Returns the reply field that every writable open thread keeps visible. */
export async function replyField(thread: Locator): Promise<Locator> {
	const field = thread.getByPlaceholder('Reply…');
	await expect(field).toBeVisible();
	return field;
}

/**
 * Everything the drawer can show. An image's drawer opens on Open, where a thread that is accepted or resolved leaves the list;
 * its filter set to All keeps it in view. A text document's drawer has no filter: its open threads are always listed and its
 * resolved ones sit behind the "Resolved" control, which this opens.
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

/**
 * Closes the comments as a person does. The sheet and the inline drawer have a Close button; the margin column beside a text
 * document has none and closes with the document header's comments control.
 */
export async function closeComments(scope: Page | Locator): Promise<void> {
	const close = scope.getByRole('button', { name: 'Close comments', exact: true });
	if (await close.count()) return close.click();
	const viewer = 'goto' in scope ? scope : scope.locator('xpath=ancestor::*[.//*[contains(concat(" ", @class, " "), " comment-status ")]][1]');
	await viewer.locator('.comment-status').click();
}

/**
 * The control that opens a text document's resolved threads: the document header's "Resolved" button, at every width. Found
 * on the page, not above the list: the list's popover is drawn at the page's top level, outside the document it belongs to.
 */
export function resolvedEntry(drawer: Locator): Locator {
	return drawer.page().locator('[data-resolved-entry]');
}

/** Where the opened resolved threads are listed: in the margin column's place, a popover from its control, or the phone's sheet. */
export function resolvedList(page: Page): Locator {
	return page.locator('[data-resolved-column], .resolved-pop, .resolved-drawer');
}

/** The "Resolved" list opened, when the document has resolved threads. */
export async function showResolved(drawer: Locator): Promise<void> {
	const entry = resolvedEntry(drawer);
	if ((await entry.count()) && (await entry.getAttribute('aria-expanded')) !== 'true') await entry.click();
}

/**
 * Runs `check` with the "Resolved" list open, then closes it again. A decided suggestion and a resolved thread leave the open
 * cards for that list, which covers the margin while it is open, so a spec that goes on to press a card button must close it first.
 */
export async function inResolved<T>(drawer: Locator, check: () => Promise<T>): Promise<T> {
	const entry = resolvedEntry(drawer);
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
	const entry = resolvedEntry(drawer);
	await expect(entry).toBeVisible();
	if ((await entry.getAttribute('aria-expanded')) !== 'true') await entry.click();
	await expect(entry).toHaveAttribute('aria-expanded', 'true');
}
