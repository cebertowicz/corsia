import { expect } from '@playwright/test';
import { test } from '../support/test';
import { openComments } from '../support/commentDrawer';
import { setDebugVersionOverlay, setMockCorsiaAgentModel } from '../support/heldComments';
import { openLoaded, seedLoaded } from '../support/loadedComments';
import { settled } from '../support/reviewFixtures';

/**
 * The comment drawer keeps one scroller, at full load. In the popover (900 and 1440) the comment list is the only thing that
 * scrolls: nothing positioned against the popover reaches past the list and widens the outer box. On a phone turned sideways with
 * the keyboard up, the reply field and the Deliver row move into that same list, so each is reachable and still only one thing
 * scrolls.
 */

test.use({ hasTouch: true });
test(`@flow:doc-comments-one-scroller a phone turned sideways with the keyboard up keeps the reply and delivery reachable through one list`, async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop');
	test.setTimeout(180_000);
	await setMockCorsiaAgentModel(request);
	await setDebugVersionOverlay(request, false);
	const doc = await seedLoaded(request, 'cramped sheet');
	await openLoaded(page, doc, { width: 852, height: 393, theme: 'light', touch: true, pane: 'closed' });
	await openComments(page);
	const drawer = page.locator('[data-doc-comments-drawer]');
	const row = drawer.locator(`[data-comment-id="${doc.longThreads[0]}"]`).first().getByRole('button', { name: /16 replies/ });
	await row.scrollIntoViewIfNeeded();
	await row.tap();
	await settled(page);
	const list = drawer.locator('.drawer-comments');
	await list.evaluate((el) => { el.scrollTop = el.scrollHeight; });
	await drawer.getByPlaceholder('Reply…').last().tap();
	// The keyboard takes most of the height: the fixed rows leave less than a 44 px reply target.
	await page.setViewportSize({ width: 852, height: 228 });
	await settled(page);
	await page.waitForTimeout(300);
	await expect.poll(() => list.evaluate((el) => el.clientHeight), 'the list keeps room for a 44 px reply target').toBeGreaterThanOrEqual(47);
	await expect(drawer.locator('.drawer-comments .drawer-foot'), 'the footer moved into the list').toHaveCount(1);
	const reach = async (selector: string) => list.evaluate((el, sel) => {
		const target = el.querySelector<HTMLElement>(sel)!;
		target.scrollIntoView({ block: 'nearest' });
		const r = target.getBoundingClientRect(), clip = el.getBoundingClientRect();
		return { fits: r.top >= clip.top - 1 && r.bottom <= clip.bottom + 1, height: r.height };
	}, selector);
	const input = await reach('.ct-input');
	const toggle = await reach('.drawer-foot .tg');
	expect(input.fits, 'the reply field can be scrolled wholly into the list').toBe(true);
	expect(input.height, 'the reply field is a 44 px target').toBeGreaterThanOrEqual(43.5);
	expect(toggle.fits, 'the Deliver row can be scrolled wholly into the list').toBe(true);
	expect(toggle.height, 'the Deliver row is a 44 px target').toBeGreaterThanOrEqual(43.5);
	const scrollers = await drawer.evaluate((root) => [root.closest<HTMLElement>('.drawer-panel')!, ...root.querySelectorAll<HTMLElement>('*')].filter((el) => /auto|scroll/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight + 1).map((el) => el.className));
	expect(scrollers, 'only the list scrolls').toHaveLength(1);
});

for (const width of [900, 1440]) test(`@flow:doc-comments-one-scroller the loaded popover scrolls only its list at ${width}`, async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop');
	test.setTimeout(180_000);
	await setMockCorsiaAgentModel(request);
	await setDebugVersionOverlay(request, false);
	const doc = await seedLoaded(request, `popover ${width}`);
	await openLoaded(page, doc, { width, height: 900, theme: 'light', touch: false, pane: width === 900 ? 'closed' : 'open' });
	await openComments(page);
	await page.locator('[data-doc-comments-drawer] [data-comment-id]').first().waitFor();
	await settled(page);
	const result = await page.locator('[data-doc-comments-drawer]').evaluate((drawer) => {
		const list = drawer.querySelector<HTMLElement>('.drawer-comments')!, outer = drawer.closest<HTMLElement>('[data-popover]')!;
		const clip = list.getBoundingClientRect();
		const escaped = [...list.querySelectorAll<HTMLElement>('*')].filter((el) => {
			const r = el.getBoundingClientRect();
			return getComputedStyle(el).position === 'absolute' && !list.contains(el.offsetParent) && (r.bottom > clip.bottom || r.top < clip.top);
		}).map((el) => `${el.tagName}.${el.className}`);
		return { outerClient: outer.clientHeight, outerScroll: outer.scrollHeight, listClient: list.clientHeight, listScroll: list.scrollHeight, escaped };
	});
	expect(result.outerScroll, `the popover does not scroll itself (escaped: ${result.escaped.join(', ')})`).toBe(result.outerClient);
	expect(result.listScroll, 'the list scrolls').toBeGreaterThan(result.listClient);
});
