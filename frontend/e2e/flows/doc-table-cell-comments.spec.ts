import { expect } from '@playwright/test';
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
