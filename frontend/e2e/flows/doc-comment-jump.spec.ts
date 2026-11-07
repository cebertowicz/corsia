import { test } from '../support/test';
import { expect, type Locator, type Page } from '@playwright/test';
import { saveEvidenceScreenshot } from '../support/corsia';
import { openSuggestionDocument } from '../support/docSuggestions';

/**
 * Activating a comment in the Comments sheet takes the reader to its text, whether they press the quote line or the
 * card itself, for a comment far above the visible text and one far below it; the passage lands above the lowered
 * sheet. Beside a wide canvas each card sits in the margin next to its own text, so nothing is far to jump to;
 * there, a reader who scrolls right after the column opens stays where they scrolled.
 */
const SECTIONS = ['First', 'Second', 'Third', 'Fourth', 'Fifth', 'Sixth'];
const filler = (label: string) => Array.from({ length: 14 }, (_, i) => `Filler line ${i} of the ${label} section keeps the page tall enough to scroll.`).join('\n\n');
const TEXT = SECTIONS.map((name) => `## ${name} section\n\n${filler(name)}\n\nThe ${name} target is right here.\n\n${filler(name)}`).join('\n\n');
const quoteOf = (section: string) => `${section} target is right here`;
const lineOf = (page: Page, section: string) => page.locator('.cm-line', { hasText: quoteOf(section) });

/** The canvas's own scroller: the nearest scrolling ancestor of the editor. */
async function canvasScroller(page: Page) {
	return (await page.locator('.cm-scroller').evaluateHandle((node) => {
		for (let element: HTMLElement | null = node as HTMLElement; element; element = element.parentElement) {
			const { overflowY } = getComputedStyle(element);
			if ((overflowY === 'auto' || overflowY === 'scroll') && element.scrollHeight > element.clientHeight) return element;
		}
		throw new Error('the document has no scrolling viewport');
	})).asElement()!;
}

/**
 * Parks the canvas on the middle section, so one comment's text is far above it and another's far below. `wheel`
 * scrolls as a reader does, over the text; without it the scroller is set directly, since a sheet can cover the text.
 */
async function parkInMiddle(page: Page, { wheel = false } = {}) {
	const scrolling = await canvasScroller(page);
	if (wheel) {
		const box = (await scrolling.boundingBox())!;
		await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
		await page.mouse.wheel(0, await scrolling.evaluate((element) => (element.scrollHeight - element.clientHeight) * (2.5 / 6) - element.scrollTop));
	} else {
		await scrolling.evaluate((element) => { element.scrollTop = (element.scrollHeight - element.clientHeight) * (2.5 / 6); });
	}
	await expect(page.locator('.cm-line', { hasText: /of the (Second|Third|Fourth) section/ }).first()).toBeAttached();
	await expect(lineOf(page, 'First')).toHaveCount(0);
	await expect(lineOf(page, 'Sixth')).toHaveCount(0);
}

/** Where the passage ended up, measured against the visible canvas and, when a sheet covers it, the sheet's top edge. */
async function visibleClearance(page: Page, line: Locator, sheet: Locator | null) {
	if ((await line.count()) === 0) return 'the passage is not drawn';
	const text = await line.first().evaluate((node) => {
		const span = document.createRange();
		span.selectNodeContents(node);
		const { top, bottom } = span.getBoundingClientRect();
		return { top, bottom };
	});
	const canvas = (await page.locator('.doc').boundingBox())!;
	if (text.top < canvas.y) return `passage top ${text.top} is above the canvas top ${canvas.y}`;
	const floor = sheet ? (await sheet.boundingBox())!.y : canvas.y + canvas.height;
	if (text.bottom > floor) return `passage bottom ${text.bottom} is below ${sheet ? 'the sheet top' : 'the canvas bottom'} ${floor}`;
	return 'visible';
}

const phone = { name: '393 phone sheet', width: 393, height: 852, project: 'chromium-mobile' } as const;

test.describe(`comment jump at ${phone.name}`, () => {
	test.beforeEach(async ({ page }, testInfo) => {
		test.skip(testInfo.project.name !== phone.project, `${phone.name} runs on the ${phone.project} project.`);
		test.setTimeout(180_000);
		await page.setViewportSize({ width: phone.width, height: phone.height });
	});

	for (const via of ['quote', 'card'] as const) {
		for (const [where, section] of [['far below', 'Sixth'], ['far above', 'First']] as const) {
			test(`@flow:document-comments pressing a comment's ${via} jumps to its text ${where} the visible text at ${phone.name}`, async ({ page, request }, testInfo) => {
				const { ids: [above, below], drawer } = await openSuggestionDocument(page, request, TEXT, [
					{ quote: quoteOf('First'), body: 'Comment on the first target.' },
					{ quote: quoteOf('Sixth'), body: 'Comment on the sixth target.' },
				]);
				await expect(page.locator('.cm-scroller')).toBeVisible();
				await page.locator('.comment-status').click();
				await expect(drawer).toBeVisible();
				await parkInMiddle(page);
				const id = section === 'First' ? above : below;
				const row = drawer.locator(`[data-comment-id="${id}"]`);
				await row.scrollIntoViewIfNeeded();
				await saveEvidenceScreenshot(page, testInfo, `comment-jump-${via}-${where.replace(' ', '-')}-before-${phone.width}`, { fullPage: false });
				if (via === 'quote') await row.locator('[data-comment-jump]').click();
				else await row.getByText(section === 'First' ? 'Comment on the first target.' : 'Comment on the sixth target.').click();
				const sheet = page.locator('[data-doc-comments-drawer]');
				await expect.poll(() => visibleClearance(page, lineOf(page, section), sheet), { message: `${via} press ${where}: the passage comes into view`, timeout: 8_000 }).toBe('visible');
				await saveEvidenceScreenshot(page, testInfo, `comment-jump-${via}-${where.replace(' ', '-')}-after-${phone.width}`, { fullPage: false });
			});
		}
	}
});

test('@flow:document-comments scrolling right after the margin column opens stays where the reader scrolled at 1440', async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', '1440 runs on the chromium-desktop project.');
	test.setTimeout(180_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	await openSuggestionDocument(page, request, TEXT, [
		{ quote: quoteOf('First'), body: 'Comment on the first target.' },
		{ quote: quoteOf('Sixth'), body: 'Comment on the sixth target.' },
	], { splitWidth: 5000 });
	await expect(page.locator('.cm-scroller')).toBeVisible();
	// The margin column needs a wide canvas: tuck both side panels away.
	await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	if (!(await page.locator('.pane').evaluate((element) => element.classList.contains('collapsed')))) {
		await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
	}
	await expect.poll(async () => (await page.locator('.doc').boundingBox())!.width).toBeGreaterThanOrEqual(900);
	await page.locator('.comment-status').click();
	// Inside the column's opening hold, which keeps the reader's line steady while the text narrows.
	await parkInMiddle(page, { wheel: true });
	const scrolling = await canvasScroller(page);
	const parked = await scrolling.evaluate((element) => element.scrollTop);
	await page.waitForTimeout(2_000);
	expect(Math.abs((await scrolling.evaluate((element) => element.scrollTop)) - parked)).toBeLessThan(4);
	await expect(lineOf(page, 'First')).toHaveCount(0);
	await saveEvidenceScreenshot(page, testInfo, 'comment-margin-scroll-after-open-1440', { fullPage: false });
});
