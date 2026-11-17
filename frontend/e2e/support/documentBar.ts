import { expect, type Locator, type Page } from '@playwright/test';

/**
 * The open document's one row of controls: Comments and Details at the end of the bar that holds the canvas (the app bar, or the
 * tab strip while the canvas is expanded). Everything else the document header used to show (status, owner, Send, Resolved,
 * History, Request review) sits in the Details panel, so a spec opens it as a person does before it presses or reads one.
 */
export const detailsEntry = (page: Page): Locator => page.locator('[data-document-details]');
export const detailsPanel = (page: Page): Locator => page.locator('.document-header .doc-panel');

/**
 * Opens Details and waits for its panel. Pressed only while it reports closed, and read again, so a panel that closes itself
 * after the read (a press elsewhere landing late) is opened again.
 */
export async function openDetails(page: Page): Promise<Locator> {
	const entry = detailsEntry(page);
	await expect(async () => {
		if ((await entry.getAttribute('aria-expanded')) !== 'true') await entry.click({ timeout: 5_000 });
		await expect(entry).toHaveAttribute('aria-expanded', 'true', { timeout: 1_000 });
	}).toPass({ timeout: 15_000 });
	const panel = detailsPanel(page);
	await expect(panel).toBeVisible();
	return panel;
}

/** Closes Details with its own button, as a person does; a panel already closed is left closed. */
export async function closeDetails(page: Page): Promise<void> {
	const entry = detailsEntry(page);
	if (!(await entry.count())) return;
	await expect(async () => {
		if ((await entry.getAttribute('aria-expanded')) === 'true') await entry.click({ timeout: 5_000 });
		await expect(entry).toHaveAttribute('aria-expanded', 'false', { timeout: 1_000 });
	}).toPass({ timeout: 15_000 });
}

type Box = { name: string; left: number; top: number; right: number; bottom: number };

/**
 * Each control in `controls` is at least `min` px each way and keeps at least `gap` px from every other visible control in the
 * same `scope` (0: they may touch, never overlap). Returns the boxes so a spec can attach them.
 */
export async function expectTouchTargets(scope: Locator, controls: string, { min = 44, gap = 0 }: { min?: number; gap?: number } = {}): Promise<Box[]> {
	const { mine, others } = await scope.evaluate((root, selector) => {
		const box = (element: Element) => {
			const rect = element.getBoundingClientRect();
			return { name: element.getAttribute('aria-label') || element.textContent?.trim() || element.tagName, left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom };
		};
		const shown = (element: Element) => { const rect = element.getBoundingClientRect(); return rect.width > 0 && rect.height > 0 && getComputedStyle(element).visibility !== 'hidden'; };
		const targets = [...root.querySelectorAll(selector)].filter(shown);
		const all = [...root.querySelectorAll('button, a[href], input, select, textarea, [role="button"], [role="switch"]')].filter(shown);
		return { mine: targets.map(box), others: all.filter((element) => !targets.some((target) => target === element || target.contains(element) || element.contains(target))).map(box).concat(targets.map(box)) };
	}, controls);
	expect(mine.length, `${controls} is on screen`).toBeGreaterThan(0);
	for (const control of mine) {
		expect(control.right - control.left, `${control.name} is at least ${min}px wide`).toBeGreaterThanOrEqual(min - 0.5);
		expect(control.bottom - control.top, `${control.name} is at least ${min}px tall`).toBeGreaterThanOrEqual(min - 0.5);
		for (const other of others) {
			if (other === control || (other.left === control.left && other.top === control.top && other.right === control.right && other.bottom === control.bottom)) continue;
			// The space between two boxes: positive when apart on either axis, negative when they overlap on both.
			const apart = Math.max(other.left - control.right, control.left - other.right, other.top - control.bottom, control.top - other.bottom);
			expect(apart, `${control.name} keeps ${gap}px from ${other.name}`).toBeGreaterThanOrEqual(gap - 0.5);
		}
	}
	return mine;
}

/** The bar that draws the open document's controls: the app bar while the canvas is compact, the canvas's tab strip while expanded. */
export const documentBarHost = (page: Page): Locator => page.locator('.shell-header, .strip').filter({ has: page.locator('[data-comments-entry]') });

/** Comments and Details are 44px touch targets on a phone and overlap nothing else in their bar. */
export async function expectBarHitAreas(page: Page, { min = 44 }: { min?: number } = {}): Promise<Box[]> {
	return expectTouchTargets(documentBarHost(page), '[data-comments-entry], [data-document-details]', { min });
}

/**
 * The document's controls add exactly one row: Comments, Details and (in the app bar) the document's name share one line, and
 * the bar is no taller than that one row.
 */
export async function expectOneRow(page: Page): Promise<{ bar: number; row: number; centres: number[] }> {
	const measured = await documentBarHost(page).evaluate((bar) => {
		const items = [...bar.querySelectorAll('[data-comments-entry], [data-document-details], [data-file-changed], [data-testid="header-document-title"]')]
			.map((element) => element.getBoundingClientRect()).filter((rect) => rect.width > 0 && rect.height > 0);
		const style = getComputedStyle(bar);
		const inner = bar.getBoundingClientRect().height - Number.parseFloat(style.paddingTop) - Number.parseFloat(style.paddingBottom);
		return { bar: inner, row: Math.max(...items.map((rect) => rect.height)), centres: items.map((rect) => rect.top + rect.height / 2) };
	});
	expect(measured.centres.length).toBeGreaterThanOrEqual(2);
	expect(Math.max(...measured.centres) - Math.min(...measured.centres), 'the document controls share one line').toBeLessThanOrEqual(1);
	expect(measured.bar, 'the bar is one row tall').toBeLessThan(measured.row * 2);
	return measured;
}
