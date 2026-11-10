import { expect, type Locator, type Page } from '@playwright/test';
import { test } from '../support/test';
import {
	cell, cellContent, DOC, editorIn, editorInCell, frame, keysOf, LAYOUTS, onlyIn, openTableDocument, savedContaining, savedText
} from '../support/tableCells';

/**
 * The comment flows still see a cell once cells are editable: text selected in an open cell offers a comment, a comment
 * drawn on a cell's text opens on a click and stays drawn while that cell is edited, and an image in a cell keeps its own
 * click. A read-only document never opens a cell editor. At 1280 with the Workspace panel over the document, a click
 * still reaches the cell where it is drawn.
 */

type Box = { x: number; y: number; right: number; bottom: number };
/** Where `target` rests: read once the offer's pop and the sheet's slide have finished, since both move what they draw. */
const boxOf = (target: Locator): Promise<Box> => target.evaluate(async (el) => {
	await Promise.all(document.getAnimations().filter((a) => a.effect?.getComputedTiming().iterations !== Infinity).map((a) => a.finished.catch(() => null)));
	const r = el.getBoundingClientRect();
	return { x: r.left, y: r.top, right: r.right, bottom: r.bottom };
});
/** The box `words` are laid out in inside `root`, as the browser draws them. */
const wordsIn = (root: Locator, words: string): Promise<Box> => root.evaluate((el, words) => {
	const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
	for (let node = walk.nextNode(); node; node = walk.nextNode()) {
		const at = node.textContent!.indexOf(words);
		if (at < 0) continue;
		const range = document.createRange();
		range.setStart(node, at);
		range.setEnd(node, at + words.length);
		const r = range.getBoundingClientRect();
		return { x: r.left, y: r.top, right: r.right, bottom: r.bottom };
	}
	throw new Error(`no "${words}" drawn`);
}, words);
const commentBox = (page: Page) => page.locator('.composer').filter({ has: page.getByPlaceholder('Comment, or @ to tag an agent') });
/** `box` measured from the top-left of `origin`. */
const from = (box: Box, origin: Box): Box => ({ x: box.x - origin.x, y: box.y - origin.y, right: box.right - origin.x, bottom: box.bottom - origin.y });
/** Where the composer opened: against the cell beside it, or on the screen when it opened in the phone's sheet. */
const composerAt = async (composer: Locator, target: Locator) => {
	const inSheet = await composer.evaluate((el) => !!el.closest('[data-adaptive-drawer]'));
	return inSheet ? await boxOf(composer) : from(await boxOf(composer), await boxOf(target));
};
/** Within 2px: the same words lay out a pixel apart in the drawn cell and in its editor. */
const near = (a: number, b: number) => Math.abs(a - b) <= 2;

for (const layout of LAYOUTS) {
	const at = layout.width;
	test.describe(`@flow:doc-table-cell-comments @surface:documents at ${at}`, () => {
		test.beforeEach(({}, testInfo) => onlyIn(layout, testInfo));

		test('a word selected in a cell, a commented cell and an image in a cell', async ({ page }) => {
			test.setTimeout(120_000);
			const seeded = await openTableDocument(page, layout);
			const keys = await keysOf(page);

			// A word selected inside the open cell, as in prose: the selection reaches the document, so the comment offer appears.
			await cell(page, 3, 1).click({ position: { x: 6, y: 6 } });
			await editorInCell(page, cell(page, 3, 1));
			await page.keyboard.press(keys.docStart);
			for (let char = 0; char < 'A long'.length; char++) await page.keyboard.press('Shift+ArrowRight');
			await expect(page.locator('.sel-comment'), 'the comment offer appears for text selected in a cell').toBeVisible();
			await frame(page, `05-selection-in-cell-${at}`);
			await page.keyboard.press('Escape');

			// A comment already drawn on a cell's text opens on a click.
			const marked = page.locator(`[data-comment-ids*="${seeded.commentId}"]`).first();
			await expect(marked, 'the comment is drawn inside the cell').toBeVisible();
			// Pressed and released as a person clicks, not in one instant: with the document focused, an instant click can have
			// the editor read the press's selection after the click opened the comment, which closes it again. The same holds
			// with no cell ever edited, so it is the comment click's own timing, not cell editing.
			await marked.click({ delay: 80 });
			await expect(page.getByText('Which way does it point now?').first()).toBeVisible();
			await frame(page, `05-comment-in-cell-${at}`);
			// The comment is put away the way a reader does: on a phone by tapping outside the drawer it opened in, elsewhere by
			// clicking back into the document's text.
			const drawer = page.locator('[data-adaptive-drawer].drawer-backdrop');
			if (await drawer.isVisible()) await drawer.click({ position: { x: 8, y: 8 } });
			else await page.getByText('Intro paragraph above the tables.').click();
			await expect(drawer).toBeHidden();

			// Editing the commented cell outside the quoted words keeps the comment drawn on them.
			const commented = cell(page, 1, 1);
			await commented.click({ position: { x: 4, y: 6 } });
			await editorInCell(page, commented);
			await page.keyboard.press(keys.docEnd);
			await page.keyboard.type(' Fixed');
			await page.keyboard.press('Escape');
			await savedContaining(page, seeded, '| The trailhead sign points the wrong way. Fixed |');
			await expect(page.locator(`[data-comment-ids*="${seeded.commentId}"]`).first(), 'the comment is still drawn on its words').toHaveText(/trailhead sign points the wrong way/);
			await frame(page, `05-commented-cell-edited-${at}`);

			// An image inside a cell keeps its own click: it opens rather than editing.
			const image = page.locator('.cm-md-cell-image').first();
			await image.scrollIntoViewIfNeeded();
			await expect(image).toBeVisible();
			await image.click();
			await expect(page.locator('.cm-md-cell-editor'), 'no cell editor opened over the image').toHaveCount(0);
			await frame(page, `05-image-in-cell-${at}`);
		});

		test('text selected in an open cell is offered Comment, and its composer opens, where a drawn cell\'s selection does', async ({ page }) => {
			test.setTimeout(120_000);
			await openTableDocument(page, layout);
			const keys = await keysOf(page);
			const target = cell(page, 3, 1), offer = page.locator('.sel-comment'), composer = commentBox(page);
			await target.scrollIntoViewIfNeeded();

			// The same words dragged over in the drawn cell, before any cell is opened: where the offer and composer go for them.
			const drawn = await wordsIn(target, 'A long'), middle = (drawn.y + drawn.bottom) / 2;
			await page.mouse.move(drawn.x + 1, middle);
			await page.mouse.down();
			await page.mouse.move(drawn.right - 1, middle, { steps: 8 });
			await page.mouse.up();
			await expect(offer, 'setup: the drawn cell\'s selection is offered Comment').toBeVisible();
			// Read against the cell's own box, so a scroll between the two selections moves nothing compared.
			const drawnCell = await boxOf(target), drawnOffer = from(await boxOf(offer), drawnCell);
			await offer.click();
			await expect(composer, 'setup: its composer opens').toBeVisible();
			const drawnComposer = await composerAt(composer, target);
			await composer.getByRole('button', { name: 'Cancel', exact: true }).click();
			await expect(composer).toBeHidden();

			await target.click({ position: { x: 6, y: 6 } });
			await editorInCell(page, target);
			await page.keyboard.press(keys.docStart);
			for (let char = 0; char < 'A long'.length; char++) await page.keyboard.press('Shift+ArrowRight');
			await expect(offer).toBeVisible();
			const words = await wordsIn(page.locator('.cm-md-cell-editor'), 'A long'), box = await boxOf(target), shown = await boxOf(offer);
			const placed = from(shown, box);
			expect(shown.bottom <= words.y && shown.x >= box.x && shown.x <= box.right && near(placed.y, drawnOffer.y) && near(placed.x, drawnOffer.x),
				`the offer sits above the selected words, from inside the cell, as for the drawn cell (${JSON.stringify({ shown, words, box, placed, drawnOffer })})`).toBe(true);
			await offer.click();
			await expect(composer).toBeVisible();
			const opened = await composerAt(composer, target);
			expect(near(opened.y, drawnComposer.y) && near(opened.x, drawnComposer.x),
				`the composer opens where it does for the drawn cell (${JSON.stringify({ opened, drawnComposer })})`).toBe(true);
		});

		test('a read-only document never opens a cell editor', async ({ page }) => {
			test.setTimeout(120_000);
			const seeded = await openTableDocument(page, layout, { readOnly: true });
			if (layout.project === 'chromium-mobile') {
				// With nothing to edit, a tap on a cell still selects it for a comment, as it did before cells could be edited.
				await cell(page, 2, 1).tap();
				await expect(page.locator('.sel-comment'), 'a tap selects the read-only cell for a comment').toBeVisible();
			} else await cell(page, 2, 1).click();
			await page.keyboard.type('typed');
			await expect(editorIn(page), 'no editor opens in a read-only document').toHaveCount(0);
			expect(await cell(page, 2, 1).innerText(), 'and the cell is unchanged').toBe('Plain words in a cell.');
			expect(await savedText(page, seeded), 'and so is the file').toBe(DOC);
			await frame(page, `05-read-only-${at}`);
		});
	});
}

test('@flow:doc-table-cell-comments @surface:documents at 1280 with the Workspace panel open, a click in a cell types into the cell and the file', async ({ page }, testInfo) => {
	onlyIn(LAYOUTS[0], testInfo);
	test.setTimeout(150_000);
	const seeded = await openTableDocument(page, { width: 1280, height: 900 });
	// The Workspace panel, open as the person who reported this had it: at this width the pane lies over the right of the
	// document rather than pushing it aside, so a click has to reach the cell where it is drawn, under the pane's edge.
	const pane = page.locator('aside.pane');
	if (!(await pane.isVisible())) await page.getByRole('button', { name: 'Toggle panel' }).click();
	await expect(pane, 'the Workspace panel is open over the document').toBeVisible();
	await frame(page, '05-panel-1280-before');
	for (const [name, target] of [['plain', cell(page, 2, 1)], ['commented', cell(page, 1, 1)]] as const) {
		// The pane covers the right of the document at this width, so the click goes where the cell is still reachable.
		await target.click({ position: { x: 4, y: 6 } });
		await editorInCell(page, target);
		await expect(cellContent(page), `${name}: focus is inside the cell editor`).toBeFocused();
		await page.keyboard.type('ZZ');
		expect(await target.innerText(), `${name}: the cell shows what was typed`).toContain('ZZ');
		await page.keyboard.press('Escape');
		await savedContaining(page, seeded, 'ZZ');
		await frame(page, `05-panel-1280-${name}-after`);
	}
});
