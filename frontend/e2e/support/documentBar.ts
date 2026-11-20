import { expect, type Locator, type Page } from '@playwright/test';

/**
 * The open document's one row of controls: Comments and Details at the end of the bar that holds the canvas (the app bar, or the
 * tab strip while the canvas is expanded). Everything else the document header used to show (status, owner, Send, Resolved,
 * History, Request review) sits in the Details panel, so a spec opens it as a person does before it presses or reads one.
 */
export const detailsEntry = (page: Page): Locator => page.locator('[data-document-details]');
export const detailsPanel = (page: Page): Locator => page.locator('.document-header .doc-panel');

/**
 * On a phone the open-files sheet opens from the document's name in the app bar (it took the old "Files (N)" button's place).
 * Its accessible name ends "Open files, N": `count` checks N, and any count matches when it is left out.
 */
export const openFilesButton = (page: Page, count?: number): Locator =>
	page.getByRole('button', { name: new RegExp(`· Open files, ${count ?? '\\d+'}$`) });

/** The app bar's Back to the chat while a document has the canvas. Its name adds " · Needs you" while something waits on the person. */
export const backToChat = (page: Page): Locator => page.getByRole('button', { name: /^Chat(?: · Needs you)?$/ });

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

/**
 * Presses Toggle panel as a person does. On a phone with a document open and the panel closed, the app bar's Toggle panel is in
 * Details instead, as Show panel; once the panel is open it is back in the app bar to close it.
 */
export async function togglePanel(page: Page): Promise<void> {
	const inBar = page.getByRole('button', { name: 'Toggle panel', exact: true });
	if (await inBar.isVisible()) return inBar.click();
	await (await openDetails(page)).getByRole('button', { name: 'Show panel', exact: true }).click();
}

export type Box = { left: number; top: number; right: number; bottom: number };
/**
 * A control as a person meets it, in two boxes because their two questions differ. `left/top/right/bottom` is the box they can
 * tap: the border box, cut to every ancestor that clips its overflow, so a tab scrolled under the strip's edge counts only where
 * it shows. `ink` is everything the control actually paints, which is wider than the tap box whenever a label overflows it.
 */
export type ControlBox = { name: string; left: number; top: number; right: number; bottom: number; ink: Box };

/**
 * The controls matching `controls` in `scope` (`mine`) and every other visible control in that scope (`others`, targets included,
 * so a spec can compare targets with each other too).
 *
 * Clearance has to be measured on the painted box: a button whose box is narrower than its label still paints the label past its
 * edge. The 1920 document bar wore File changed in a 44px box whose words ran past 115px over the comment count, and a check on
 * border boxes alone called that clear. Text under a clip is not painted, so it counts only where it shows.
 */
function measureControls(scope: Locator, controls: string): Promise<{ mine: ControlBox[]; others: ControlBox[] }> {
	return scope.evaluate((root, selector) => {
		const clamp = (rect: DOMRect | DOMRectReadOnly, owner: Element): Box => {
			let { left, top, right, bottom } = rect;
			for (let parent = owner.parentElement; parent; parent = parent.parentElement) {
				const style = getComputedStyle(parent);
				if (style.overflowX === 'visible' && style.overflowY === 'visible') continue;
				const clip = parent.getBoundingClientRect();
				if (style.overflowX !== 'visible') { left = Math.max(left, clip.left); right = Math.min(right, clip.right); }
				if (style.overflowY !== 'visible') { top = Math.max(top, clip.top); bottom = Math.min(bottom, clip.bottom); }
			}
			return { left, top, right: Math.max(left, right), bottom: Math.max(top, bottom) };
		};
		// An invisible subtree paints nothing, so a hidden measuring copy (the receipt's `width: max-content` twin) never reads
		// as ink the visible control never shows. REJECT on an element prunes its whole subtree.
		const paints = (element: Element) => {
			const style = getComputedStyle(element);
			return style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity) > 0;
		};
		const inkOf = (element: Element): Box => {
			let union = clamp(element.getBoundingClientRect(), element);
			const add = (rect: DOMRect | DOMRectReadOnly, owner: Element) => {
				if (rect.width <= 0 || rect.height <= 0) return;
				const box = clamp(rect, owner);
				union = {
					left: Math.min(union.left, box.left), top: Math.min(union.top, box.top),
					right: Math.max(union.right, box.right), bottom: Math.max(union.bottom, box.bottom),
				};
			};
			const walker = document.createTreeWalker(element, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, (node) =>
				node.nodeType === Node.TEXT_NODE
					? node.parentElement && paints(node.parentElement) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT
					: paints(node as Element) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT);
			for (let node = walker.nextNode(); node; node = walker.nextNode()) {
				if (node.nodeType === Node.TEXT_NODE) {
					const range = document.createRange();
					range.selectNodeContents(node);
					for (const rect of range.getClientRects()) add(rect, node.parentElement!);
				} else {
					add((node as Element).getBoundingClientRect(), node as Element);
				}
			}
			return union;
		};
		const box = (element: Element): ControlBox => {
			const tap = clamp(element.getBoundingClientRect(), element);
			return {
				name: element.getAttribute('aria-label') || element.textContent?.trim() || element.tagName,
				...tap,
				ink: inkOf(element),
			};
		};
		const shown = (element: Element) => { const rect = element.getBoundingClientRect(); return rect.width > 0 && rect.height > 0 && getComputedStyle(element).visibility !== 'hidden'; };
		const targets = [...root.querySelectorAll(selector)].filter(shown);
		const all = [...root.querySelectorAll('button, a[href], input, select, textarea, [role="button"], [role="switch"]')].filter(shown);
		const neighbours = all.filter((element) => !targets.some((target) => target === element || target.contains(element) || element.contains(target)));
		return { mine: targets.map(box), others: [...neighbours, ...targets].map(box) };
	}, controls);
}

/** What each control in `controls` taps and paints, in document order, for a spec that measures clearance itself and writes the numbers into frame JSON. */
export async function paintedBoxes(scope: Locator, controls: string): Promise<ControlBox[]> {
	return (await measureControls(scope, controls)).mine;
}

/**
 * Each control in `controls` is at least `min` px each way (the tap box) and keeps at least `gap` px from every other visible
 * control in the same `scope` (0: they may touch, never overlap), measured on what each paints. Returns the boxes so a spec can
 * attach them.
 */
export async function expectTouchTargets(scope: Locator, controls: string, { min = 44, gap = 0 }: { min?: number; gap?: number } = {}): Promise<ControlBox[]> {
	const { mine, others } = await measureControls(scope, controls);
	expect(mine.length, `${controls} is on screen`).toBeGreaterThan(0);
	for (const control of mine) {
		expect(control.right - control.left, `${control.name} is at least ${min}px wide`).toBeGreaterThanOrEqual(min - 0.5);
		expect(control.bottom - control.top, `${control.name} is at least ${min}px tall`).toBeGreaterThanOrEqual(min - 0.5);
		for (const other of others) {
			if (other.ink.right - other.ink.left <= 0 || other.ink.bottom - other.ink.top <= 0) continue; // scrolled wholly out of sight
			if (other.left === control.left && other.top === control.top && other.right === control.right && other.bottom === control.bottom) continue; // the same control, measured twice
			// The painted gap between two controls: positive when apart on either axis, negative when what they paint overlaps.
			const apart = Math.max(other.ink.left - control.ink.right, control.ink.left - other.ink.right, other.ink.top - control.ink.bottom, control.ink.top - other.ink.bottom);
			expect(apart, `${control.name} keeps ${gap}px of painted room from ${other.name}`).toBeGreaterThanOrEqual(gap - 0.5);
		}
	}
	return mine;
}

/** The bar that draws the open document's controls: the app bar while the canvas is compact, the canvas's tab strip while expanded. */
export const documentBarHost = (page: Page): Locator => page.locator('.shell-header, .strip').filter({ has: page.locator('[data-comments-entry]') });

/** Comments and Details are 44px touch targets on a phone and overlap nothing else in their bar. */
export async function expectBarHitAreas(page: Page, { min = 44 }: { min?: number } = {}): Promise<ControlBox[]> {
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
