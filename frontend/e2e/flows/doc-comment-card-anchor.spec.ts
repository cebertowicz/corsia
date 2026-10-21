import { test } from '../support/test';
import { expect, type Locator, type Page } from '@playwright/test';
import { saveEvidenceScreenshot } from '../support/corsia';
import { openSuggestionDocument } from '../support/docSuggestions';

const SECTIONS = ['First', 'Second', 'Third', 'Fourth', 'Fifth', 'Sixth'];
const SECTION = 'Third';
const filler = (label: string) => Array.from({ length: 14 }, (_, i) => `Filler line ${i} of the ${label} section keeps the page tall enough to scroll.`).join('\n\n');
const TEXT = SECTIONS.map((name) => `## ${name} section\n\n${filler(name)}\n\nThe ${name} note is right here.\n\nOne paragraph between the note and the target.\n\nThe ${name} target is right here.\n\n${filler(name)}`).join('\n\n');

type Rect = { x: number; y: number; width: number; height: number };
const strike = (page: Page, id: string) => page.locator(`.cm-content .cm-suggestion-del[data-suggestion-id="${id}"]`);
const marker = (page: Page, id: string) => page.locator(`.cm-comment-marker[data-comment-ids*="${id}"]`);
const popover = (page: Page) => page.locator('[data-popover]');
const lineOf = (page: Page, text: string) => page.locator('.cm-line', { hasText: text });
const intersects = (a: Rect, b: Rect) => a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;

/** The visible box of an element, in page coordinates. */
async function box(target: Locator) {
	const found = await target.boundingBox();
	expect(found, 'element has a box').not.toBeNull();
	return found!;
}

/** What a reader sees of an element: for a text line, the text it draws (the line's own box runs the editor's full width). */
async function drawnBox(target: Locator) {
	const found = await target.evaluate((node) => {
		if (!node.classList.contains('cm-line')) return null;
		const span = document.createRange();
		span.selectNodeContents(node);
		const { x, y, width, height } = span.getBoundingClientRect();
		return { x, y, width, height };
	});
	return found ?? box(target);
}

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

/** Scrolls the canvas so the section's target line sits `fraction` of the way down the visible canvas. */
async function placeTarget(page: Page, fraction: number) {
	const line = lineOf(page, `The ${SECTION} target is right here`);
	const scrolling = await canvasScroller(page);
	// CodeMirror draws only the lines near the viewport, so bring the section into the DOM first.
	await scrolling.evaluate((element, index) => { element.scrollTop = (element.scrollHeight - element.clientHeight) * (index / 5); }, SECTIONS.indexOf(SECTION));
	await expect(line).toHaveCount(1);
	await scrolling.evaluate((element, args) => {
		const area = element.getBoundingClientRect();
		const row = [...document.querySelectorAll<HTMLElement>('.cm-line')].find((candidate) => candidate.textContent?.includes(args.text))!.getBoundingClientRect();
		element.scrollTop += row.top - (area.top + area.height * args.fraction);
	}, { fraction, text: `The ${SECTION} target is right here` });
}

/** Where the text column ends: the rightmost text drawn on any line the canvas shows, not the editor's own width. */
function textColumnRight(page: Page) {
	return page.evaluate(() => {
		const area = document.querySelector('.cm-scroller')!.getBoundingClientRect();
		let right = 0;
		for (const line of document.querySelectorAll<HTMLElement>('.cm-content .cm-line')) {
			const span = document.createRange();
			span.selectNodeContents(line);
			const drawn = span.getBoundingClientRect();
			if (drawn.width > 0 && drawn.bottom > area.top && drawn.top < area.bottom) right = Math.max(right, drawn.right);
		}
		return right;
	});
}

/**
 * The card sits inside the canvas, never over the chat pane, next to its anchor (beside, below, or above it) and
 * never over the anchor's own line, so the reader can still see what the comment is about.
 * The canvas may still be scrolling the anchor into place when the card opens, so the card settles first.
 */
async function expectBesideAnchor(page: Page, anchor: Locator, line: Locator, label: string, margin = false) {
	const gap = 48;
	await expect.poll(async () => {
		const card = await box(popover(page));
		const canvas = await box(page.locator('.doc'));
		const at = await drawnBox(anchor);
		const row = await drawnBox(line);
		const slack = 1;
		if (card.x < canvas.x - slack) return `card (${card.x}) is left of the canvas's left edge (${canvas.x})`;
		if (card.x + card.width > canvas.x + canvas.width + slack) return `card's right edge (${card.x + card.width}) is past the canvas's right edge (${canvas.x + canvas.width})`;
		if (card.y < canvas.y - slack) return `card (${card.y}) is above the canvas top (${canvas.y})`;
		if (card.y + card.height > canvas.y + canvas.height + slack) return `card's bottom (${card.y + card.height}) is past the canvas bottom (${canvas.y + canvas.height})`;
		if (intersects(card, at)) return `card ${JSON.stringify(card)} covers its anchor ${JSON.stringify(at)}`;
		if (intersects(card, row)) return `card ${JSON.stringify(card)} covers the anchor's line ${JSON.stringify(row)}`;
		if (margin) {
			// The canvas has a free margin beside the text: the card belongs there, level with the anchor's row.
			const textRight = await textColumnRight(page);
			if (card.x < textRight) return `card's left edge (${card.x}) is inside the text column, which ends at ${textRight}`;
			const middle = row.y + row.height / 2;
			if (card.y > middle || card.y + card.height < middle) return `card ${JSON.stringify(card)} is not level with the anchor's row ${JSON.stringify(row)}`;
		}
		const near = card.y <= row.y + row.height + gap && card.y + card.height >= row.y - gap;
		return near ? 'beside its anchor' : `card ${JSON.stringify(card)} is away from its anchor's line ${JSON.stringify(row)}`;
	}, { message: `${label}: the card opens beside its anchor inside the canvas`, timeout: 5_000 }).toBe('beside its anchor');
}

/** The canvas at its widest: both side panels tucked away. */
async function expandCanvas(page: Page) {
	const narrow = (await box(page.locator('.doc'))).width;
	await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	if (!(await page.locator('.pane').evaluate((element) => element.classList.contains('collapsed')))) {
		await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
	}
	await expect.poll(async () => (await box(page.locator('.doc'))).width).toBeGreaterThan(narrow + 100);
}

const layouts = [
	{ name: '1440', width: 1440, height: 900, expanded: false },
	{ name: '2000', width: 2000, height: 1000, expanded: false },
	{ name: '1440 expanded', width: 1440, height: 900, expanded: true },
] as const;

for (const { name: layout, width, height, expanded } of layouts) {
	const tag = layout.replace(' ', '-');
	const options = { splitWidth: expanded ? 5000 : 540 };
	test.describe(`comment card anchor at ${layout}`, () => {
		test.beforeEach(async ({ page }, testInfo) => {
			test.skip(testInfo.project.name !== 'chromium-desktop', 'Desktop split layout only.');
			test.setTimeout(120_000);
			await page.setViewportSize({ width, height });
		});

		for (const [name, fraction] of [['top', 0.12], ['middle', 0.5], ['bottom', 0.85]] as const) {
			test(`@flow:document-comments a plain comment and a suggestion card open beside their anchor near the ${name} of the canvas at ${layout}`, async ({ page, request }, testInfo) => {
				const { ids: [plain, suggested] } = await openSuggestionDocument(page, request, TEXT, [
					{ quote: `The ${SECTION} note is`, body: `Plain note on the ${SECTION} note.` },
					{ quote: `${SECTION} target is right here`, replacement: `${SECTION} target is right there`, body: `Suggestion on the ${SECTION} target.` },
				], options);
				await expect(page.locator('.cm-scroller')).toBeVisible();
				if (expanded) await expandCanvas(page);
				await placeTarget(page, fraction);
				await expect(strike(page, suggested)).toBeVisible();
				await expect(marker(page, plain)).toBeVisible();

				await marker(page, plain).click();
				await expect(popover(page)).toBeVisible();
				await expectBesideAnchor(page, marker(page, plain), lineOf(page, `The ${SECTION} note is`), `${name} plain marker`, expanded);
				await saveEvidenceScreenshot(page, testInfo, `comment-card-plain-${name}-${tag}`, { fullPage: false });
				await page.keyboard.press('Escape');
				await expect(popover(page)).toHaveCount(0);

				await strike(page, suggested).click();
				await expect(popover(page)).toBeVisible();
				await expectBesideAnchor(page, strike(page, suggested), lineOf(page, `${SECTION} target is`), `${name} suggestion`, expanded);
				await saveEvidenceScreenshot(page, testInfo, `comment-card-suggestion-${name}-${tag}`, { fullPage: false });
			});
		}

		test(`@flow:document-comments the card hides while its text is scrolled out of view and returns beside it at ${layout}`, async ({ page, request }) => {
			const { ids: [plain] } = await openSuggestionDocument(page, request, TEXT, [{ quote: `The ${SECTION} note is`, body: `Plain note on the ${SECTION} note.` }], options);
			await expect(page.locator('.cm-scroller')).toBeVisible();
			if (expanded) await expandCanvas(page);
			await placeTarget(page, 0.5);
			await expect(marker(page, plain)).toBeVisible();
			await marker(page, plain).click();
			await expect(popover(page)).toBeVisible();
			// Opening a comment keeps its text in view for a few frames while the canvas settles; scroll away after that.
			await page.evaluate(() => new Promise<void>((done) => { let frames = 0; const step = () => (++frames >= 40 ? done() : requestAnimationFrame(step)); step(); }));
			const scroller = await canvasScroller(page);
			await scroller.evaluate((element) => { element.scrollTop = 0; });
			// Hidden, not closed: the comment is still open, only its text is elsewhere.
			await expect(popover(page)).toHaveCount(1);
			await expect(popover(page)).toBeHidden();
			await placeTarget(page, 0.5);
			await expect(popover(page)).toBeVisible();
			await expectBesideAnchor(page, marker(page, plain), lineOf(page, `The ${SECTION} note is`), 'after scrolling back', expanded);
		});

		test(`@flow:document-comments the new-comment card stays with the selection when the canvas scrolls at ${layout}`, async ({ page, request }, testInfo) => {
			await openSuggestionDocument(page, request, TEXT, [], options);
			await expect(page.locator('.cm-scroller')).toBeVisible();
			if (expanded) await expandCanvas(page);
			await placeTarget(page, 0.5);
			const line = lineOf(page, `The ${SECTION} target is right here`);
			await line.dblclick({ position: { x: 12, y: 8 } });
			await page.locator('.sel-comment').click();
			await expect(page.getByPlaceholder('Comment…')).toBeVisible();
			await expectBesideAnchor(page, line, line, 'composer before scrolling', expanded);
			await saveEvidenceScreenshot(page, testInfo, `comment-card-composer-${tag}`, { fullPage: false });
			const scroller = await canvasScroller(page);
			await scroller.evaluate((element) => { element.scrollTop -= 120; });
			await expectBesideAnchor(page, line, line, 'composer after scrolling the canvas', expanded);
			await saveEvidenceScreenshot(page, testInfo, `comment-card-composer-scrolled-${tag}`, { fullPage: false });
		});
	});
}
