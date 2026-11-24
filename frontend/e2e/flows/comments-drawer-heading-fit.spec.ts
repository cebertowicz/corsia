import { type Page } from '@playwright/test';
import { expect, test } from '../support/test';
import { TEST_TOKEN } from '../support/corsia';
import { comment, open, seed } from '../support/reviewFixtures';
import { settleMotion, settleRect } from '../support/sheetSettle';

/**
 * The phone comment list keeps its Open and Resolved switch in the sheet heading, beside the title. The two labels never shrink
 * below their text: when the title, the switch and Close cannot share one row (320 wide with counts of two digits or more), the
 * switch wraps under the title, and nothing is clipped or runs into Close. One-digit counts at 320, and the counts at 393 (the
 * heading gap is the Plan sheet's 8 px), stay on one row.
 */

const TAG = '@flow:comments-drawer-heading-fit @surface:documents';
const SHEET = '[data-adaptive-drawer] .drawer-panel';
/** The gap between the two labels' text at 393: the buttons' side padding on each side of the join (4 px + 4 px). */
const LABEL_GAP_AT_393 = 8;

test.use({ viewport: { width: 393, height: 852 }, hasTouch: true, isMobile: true });
test.beforeEach(async ({}, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-mobile', 'The phone sheet is measured with touch in the mobile project');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	test.setTimeout(300_000);
});

/** A document with `openN` open and `resolvedN` resolved comments, opened on a phone `width` wide with the list sheet up. */
async function openList(page: Page, request: Parameters<typeof seed>[0], width: number, openN: number, resolvedN: number, theme: 'light' | 'dark' = 'light') {
	const doc = await seed(request, `Heading fit ${width} ${openN}`);
	const ids: string[] = [];
	for (let i = 0; i < openN + resolvedN; i += 1) ids.push(await comment(request, doc, `Note ${i + 1}: check this step.`, (i % 12) + 1));
	for (const id of ids.slice(openN)) {
		const resolved = await request.patch(`/api/comments/${id}`, { params: { token: TEST_TOKEN }, data: { resolved: true } });
		expect(resolved.ok(), await resolved.text()).toBeTruthy();
	}
	await open(page, doc, 'phone', theme);
	await page.setViewportSize({ width, height: 852 });
	await settleMotion(page);
	await page.locator('[data-comments-entry]').tap();
	await expect(page.locator(SHEET)).toHaveCount(1);
	await settleRect(page, SHEET);
	await page.waitForTimeout(500);
}

/** The heading's boxes: the title, each switch label (its box and its text), and Close. */
const read = (page: Page) => page.evaluate(() => {
	const head = document.querySelector<HTMLElement>('[data-adaptive-drawer] .drawer-panel .drawer-heading')!;
	const box = (el: Element) => { const r = el.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height }; };
	const labels = [...head.querySelectorAll<HTMLButtonElement>('.drawer-switch button')].map((button) => {
		const text = document.createRange(); text.selectNodeContents(button);
		const style = getComputedStyle(button);
		return { name: button.textContent, box: box(button), text: box(text as unknown as Element), clipped: button.scrollWidth > button.clientWidth, textFits: text.getBoundingClientRect().width <= button.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) + 0.5 };
	});
	const title = head.querySelector('h2')!;
	const close = head.querySelector('[aria-label="Close comments"]')!;
	return { heading: box(head), title: box(title), labels, close: box(close), sideways: document.documentElement.scrollWidth > innerWidth };
});
type Heading = Awaited<ReturnType<typeof read>>;

const intersects = (a: { left: number; right: number; top: number; bottom: number }, b: { left: number; right: number; top: number; bottom: number }) =>
	a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
/** One row: both labels and Close start at the title's top; wrapped, the switch starts under the title. */
const oneRow = (h: Heading) => h.labels.every((label) => Math.abs(label.box.top - h.title.top) < 1) && Math.abs(h.close.top - h.title.top) < 1;

for (const [openN, resolvedN] of [[103, 100], [12, 11]] as const) for (const theme of ['light', 'dark'] as const) {
	test(`${TAG} 320 wide with ${openN} open and ${resolvedN} resolved: the switch wraps under the title and no label is clipped (${theme})`, async ({ page, request }) => {
		await openList(page, request, 320, openN, resolvedN, theme);
		await expect(page.locator(SHEET).getByRole('button', { name: `Open · ${openN}` })).toBeVisible();
		await expect(page.locator(SHEET).getByRole('button', { name: `Resolved · ${resolvedN}` })).toBeVisible();
		const h = await read(page);
		expect(h.labels.map((label) => label.name)).toEqual([`Open · ${openN}`, `Resolved · ${resolvedN}`]);
		for (const label of h.labels) {
			expect(label.clipped, `${label.name} has scrollWidth > clientWidth`).toBe(false);
			expect(label.textFits, `${label.name}'s text is wider than its box`).toBe(true);
			expect(label.box.height, `${label.name} is 44 px tall`).toBeGreaterThanOrEqual(43.5);
			expect(intersects(label.box, h.close), `${label.name} overlaps Close`).toBe(false);
			expect(intersects(label.box, h.title), `${label.name} overlaps the title`).toBe(false);
		}
		expect(h.labels[1].text.left - h.labels[0].text.right, 'the labels keep the gap they have at 393').toBeGreaterThanOrEqual(LABEL_GAP_AT_393 - 0.5);
		expect(h.close.height).toBeGreaterThanOrEqual(43.5);
		expect(intersects(h.title, h.close)).toBe(false);
		expect(oneRow(h), 'the switch is under the title, not beside it').toBe(false);
		expect(h.labels[0].box.top, 'the switch sits under the title').toBeGreaterThanOrEqual(h.title.bottom - 0.5);
		expect(h.close.top, 'Close stays at the top right, level with the title').toBeLessThan(h.title.bottom);
		expect(h.sideways).toBe(false);
	});
}

test(`${TAG} 393 wide with 103 open and 100 resolved stays one row`, async ({ page, request }) => {
	await openList(page, request, 393, 103, 100);
	const h = await read(page);
	expect(h.labels.map((label) => label.name)).toEqual(['Open · 103', 'Resolved · 100']);
	expect(oneRow(h), 'the title, the switch and Close share one row').toBe(true);
	for (const label of h.labels) { expect(label.clipped).toBe(false); expect(label.textFits).toBe(true); expect(label.box.height).toBeGreaterThanOrEqual(43.5); }
	expect(h.labels[1].text.left - h.labels[0].text.right).toBeGreaterThanOrEqual(LABEL_GAP_AT_393 - 0.5);
	expect(h.heading.height, 'one 44 px row plus the heading padding').toBeLessThan(52);
});

test(`${TAG} 320 wide with 1 open and 1 resolved stays one row`, async ({ page, request }) => {
	await openList(page, request, 320, 1, 1);
	const h = await read(page);
	expect(h.labels.map((label) => label.name)).toEqual(['Open · 1', 'Resolved · 1']);
	expect(oneRow(h), 'the title, the switch and Close share one row').toBe(true);
	for (const label of h.labels) {
		expect(label.clipped).toBe(false); expect(label.textFits, `${label.name}'s text is wider than its box`).toBe(true);
		expect(label.box.height).toBeGreaterThanOrEqual(43.5);
		expect(intersects(label.box, h.close)).toBe(false);
	}
	expect(h.labels[1].text.left - h.labels[0].text.right).toBeGreaterThanOrEqual(LABEL_GAP_AT_393 - 0.5);
	expect(h.heading.height).toBeLessThan(52);
});
