import { expect, type Locator, type Page } from '@playwright/test';

/** Returns the reply field that every writable open thread keeps visible. */
export async function replyField(thread: Locator): Promise<Locator> {
	const field = thread.getByPlaceholder('Reply…');
	await expect(field).toBeVisible();
	return field;
}

/**
 * Every thread the list can show. An image's drawer opens on Open, where a thread that is accepted or resolved leaves the list;
 * its filter set to All keeps it in view. A text document's list has no filter and always lists its open threads, so this leaves
 * it as it is: its resolved threads open in their own list, which takes the open list's place (`inResolved`, `openResolved`).
 */
export async function showAllThreads(drawer: Locator): Promise<void> {
	const filter = drawer.locator('[data-drawer-filter]');
	if (!(await filter.count())) return;
	await filter.click();
	await drawer.getByRole('menu', { name: 'Filter comments', exact: true }).getByRole('menuitemradio', { name: 'All', exact: true }).click();
	await expect(filter).toHaveAttribute('aria-label', 'Filter comments: All');
	// The old list crossfades out beside the new one; wait until only one is left so a thread is never found twice.
	await expect(drawer.locator('.peers > .peer')).toHaveCount(1);
}

/**
 * Closes the comments as a person does. The sheet and the inline drawer have a Close button; the margin column beside a text
 * document has none and closes with the document header's comments control. A list that is already closed is left closed.
 */
export async function closeComments(scope: Page | Locator): Promise<void> {
	if (!('goto' in scope) && !(await scope.count())) return;
	const close = scope.getByRole('button', { name: 'Close comments', exact: true });
	if (await close.count()) return close.click();
	// The list's popover is drawn at the page's top level, outside its document, so it has no document above it to search.
	const viewer = 'goto' in scope ? scope : scope.locator('xpath=ancestor::*[.//*[contains(concat(" ", @class, " "), " comment-status ")]][1]');
	await ('goto' in scope || (await viewer.count()) ? viewer : scope.page()).locator('.comment-status').click();
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

/** A phone's comments list is a modal sheet over the document header, so it is closed first, as a person does, to reach "Resolved". */
async function leaveSheet(drawer: Locator): Promise<void> {
	if (!(await drawer.locator('xpath=ancestor::*[@data-adaptive-drawer]').count())) return;
	await closeComments(drawer);
	await expect(drawer).toHaveCount(0);
}

/** Closes the "Resolved" list: its sheet's Close button on a phone, otherwise its header control again. */
async function closeResolved(drawer: Locator): Promise<void> {
	const page = drawer.page();
	const sheet = page.locator('[data-adaptive-drawer]').filter({ has: page.locator('.resolved-drawer') });
	if (await sheet.count()) await sheet.getByRole('button', { name: 'Close', exact: true }).click();
	else {
		const entry = resolvedEntry(drawer);
		if ((await entry.count()) && (await entry.getAttribute('aria-expanded')) === 'true') await entry.click();
	}
	await expect(resolvedList(page)).toHaveCount(0);
}

/** The "Resolved" list opened, when the document has resolved threads. */
export async function showResolved(drawer: Locator): Promise<void> {
	const entry = resolvedEntry(drawer);
	if (!(await entry.count()) || (await entry.getAttribute('aria-expanded')) === 'true') return;
	await leaveSheet(drawer);
	await entry.click();
}

/**
 * Runs `check` with the "Resolved" list open, then closes it again. A decided suggestion and a resolved thread leave the open
 * cards for that list, which takes the margin's place while it is open, so a spec that goes on to press a card button must close
 * it first. Below the margin's width the open list is a popover, which pressing "Resolved" closes, and on a phone a sheet, which
 * is closed to reach "Resolved"; either is opened again afterwards, as a person would to go on with the open threads.
 */
export async function inResolved<T>(drawer: Locator, check: () => Promise<T>): Promise<T> {
	const page = drawer.page();
	const entry = resolvedEntry(drawer);
	await expect(entry).toBeVisible();
	const opened = (await entry.getAttribute('aria-expanded')) !== 'true';
	const listWasOpen = (await drawer.count()) > 0;
	if (opened) {
		await leaveSheet(drawer);
		await entry.click();
	}
	try {
		return await check();
	} finally {
		if (opened) {
			await closeResolved(drawer);
			if (listWasOpen && !(await drawer.count())) await page.locator('.comment-status').click();
			if (listWasOpen) await expect(drawer).toBeVisible();
		}
	}
}

/** The "Resolved" list opened and left open, waiting for its entry to appear first (a thread has just been resolved). */
export async function openResolved(drawer: Locator): Promise<void> {
	const entry = resolvedEntry(drawer);
	await expect(entry).toBeVisible();
	if ((await entry.getAttribute('aria-expanded')) !== 'true') {
		await leaveSheet(drawer);
		await entry.click();
	}
	await expect(entry).toHaveAttribute('aria-expanded', 'true');
}
