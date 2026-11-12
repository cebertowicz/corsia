import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import {
	cell, cellContent, editorInCell, firstTable, frame, keysOf, LAYOUTS, onlyIn, openTableDocument, paste, savedContaining,
	savedRow
} from '../support/tableCells';

/**
 * Editing a rendered table cell where it is read, at each width: a click puts the caret in the cell where it landed, and
 * typing, deleting, pasting, undo and redo work as in prose, with what is typed landing in the saved file; Tab, Shift+Tab
 * and Enter move through the table and out of it, and Escape leaves it; nothing typed or pasted breaks a row.
 */

/** The document's own editor and where its caret is: the line it is on, and the line above. */
const documentCaret = (page: Page) => page.evaluate(() => {
	const content = document.querySelector('.cm-content:not(.cm-md-cell-editor .cm-content)') as (HTMLElement & { cmTile?: { root: { view: any } } }) | null;
	const view = content?.cmTile?.root.view;
	if (!view) return null;
	const line = view.state.doc.lineAt(view.state.selection.main.head);
	return { line: line.text as string, above: line.number > 1 ? (view.state.doc.line(line.number - 1).text as string) : '', focused: view.hasFocus as boolean };
});

for (const layout of LAYOUTS) {
	const at = layout.width;
	test.describe(`@flow:doc-table-cell-editing @surface:documents at ${at}`, () => {
		test.beforeEach(({}, testInfo) => onlyIn(layout, testInfo));

		test('a click edits the cell as prose is edited, and the file keeps it', async ({ page }) => {
			test.setTimeout(120_000);
			const seeded = await openTableDocument(page, layout);
			const keys = await keysOf(page);
			const plain = cell(page, 2, 1);
			await frame(page, `01-plain-cell-${at}-before`);

			// A click at the start of the cell's text puts the caret there, the way a click in prose does.
			await plain.click({ position: { x: 6, y: 6 } });
			await editorInCell(page, plain);
			await page.keyboard.type('Startx');
			await page.keyboard.press('Backspace');
			await page.keyboard.type(' ');
			expect(await plain.innerText(), 'the caret was where the click landed, and Backspace took one character').toBe('Start Plain words in a cell.');
			await page.keyboard.press(keys.docEnd);
			await page.keyboard.type(' checked');
			await frame(page, `01-plain-cell-${at}-typing`);
			await page.keyboard.press(keys.docStart);
			for (let char = 0; char < 'Start '.length; char++) await page.keyboard.press('Delete');
			const edited = 'Plain words in a cell. checked';
			expect(await plain.innerText(), 'Delete takes the character after the caret').toBe(edited);
			await savedContaining(page, seeded, `| Signs | ${edited} | open |`);
			expect(await firstTable(page).locator('tr').nth(2).locator('td').count(), 'the row still has three columns').toBe(3);

			// The document's own undo and redo, pressed in the cell.
			await page.keyboard.press(`${keys.mod}+z`);
			await expect.poll(() => plain.innerText(), { message: 'undo takes the cell edit back' }).not.toBe(edited);
			await page.keyboard.press(keys.redo);
			await expect.poll(() => plain.innerText(), { message: 'and redo puts it back' }).toBe(edited);
			await savedContaining(page, seeded, `| Signs | ${edited} | open |`);
			await editorInCell(page, plain);

			// Escape leaves the table with the caret in the document, on the line after the table.
			await page.keyboard.press('Escape');
			await expect(page.locator('.cm-md-cell-editor')).toHaveCount(0);
			const caret = await documentCaret(page);
			expect(caret?.focused, 'the document has the caret again').toBe(true);
			expect(caret?.above, 'on the line after the table').toMatch(/^\| Wide \|/);
			await frame(page, `01-plain-cell-${at}-after`);
		});

		test('Tab, Shift+Tab and Enter move through the table and out of it', async ({ page }) => {
			test.setTimeout(120_000);
			const seeded = await openTableDocument(page, layout);
			await cell(page, 1, 1).click({ position: { x: 6, y: 6 } });
			await editorInCell(page, cell(page, 1, 1));
			// The focused textbox is named for the cell it edits, and renamed when Tab moves it.
			await expect(page.getByRole('textbox', { name: 'Review note, row 1', exact: true })).toBeFocused();
			await page.keyboard.press('Tab');
			await editorInCell(page, cell(page, 1, 2));
			await expect(page.getByRole('textbox', { name: 'Preview, row 1', exact: true })).toBeFocused();
			await page.keyboard.press('Tab');
			await editorInCell(page, cell(page, 2, 0));
			await frame(page, `02-tab-wraps-row-${at}`);
			await page.keyboard.press('Shift+Tab');
			await editorInCell(page, cell(page, 1, 2));
			await page.keyboard.type('1');
			await page.keyboard.press('Enter');
			await editorInCell(page, cell(page, 2, 2));
			await page.keyboard.type('2');
			await page.keyboard.press('Enter');
			await editorInCell(page, cell(page, 3, 2));
			await page.keyboard.type('3');
			await frame(page, `02-enter-down-column-${at}`);
			// Enter on the last row leaves the table below it.
			await page.keyboard.press('Enter');
			await expect(page.locator('.cm-md-cell-editor')).toHaveCount(0);
			let caret = await documentCaret(page);
			expect(caret?.focused, 'Enter on the last row hands the caret to the document').toBe(true);
			expect(caret?.above, 'below the table').toMatch(/^\| Wide \|/);
			await savedContaining(page, seeded, '| done1 |');
			await savedContaining(page, seeded, '| open2 |');
			await savedContaining(page, seeded, `${'those.'} | open3 |`);

			// Tab from the last cell leaves the table the same way.
			await cell(page, 3, 2).click();
			await editorInCell(page, cell(page, 3, 2));
			await page.keyboard.press('Tab');
			await expect(page.locator('.cm-md-cell-editor')).toHaveCount(0);
			caret = await documentCaret(page);
			expect(caret?.focused, 'Tab from the last cell hands the caret to the document').toBe(true);
			expect(caret?.above).toMatch(/^\| Wide \|/);
			expect(await firstTable(page).locator('tr').count(), 'no row was added').toBe(4);
		});

		test('nothing typed or pasted breaks the row', async ({ page }) => {
			test.setTimeout(120_000);
			const seeded = await openTableDocument(page, layout);
			const keys = await keysOf(page);
			const row = () => firstTable(page).locator('tr').nth(2).locator('td');
			await cell(page, 2, 1).click();
			await editorInCell(page, cell(page, 2, 1));
			await page.keyboard.press(keys.docEnd);
			await page.keyboard.type(' a|b');
			expect(await row().count(), 'a typed pipe stays inside the cell').toBe(3);
			expect(await cell(page, 2, 1).innerText(), 'and shows as a pipe, its escape hidden').toBe('Plain words in a cell. a|b');
			// A backslash typed in front of a pipe is that pipe's escape, as in markdown, so the pipe shows alone; a pasted pipe
			// is escaped like a typed one.
			await page.keyboard.type(' c\\');
			await page.keyboard.type('|d');
			await paste(page, ' e|f');
			expect(await row().count(), 'the row still has three columns').toBe(3);
			await frame(page, `03-typed-pipe-${at}`);
			await page.keyboard.press('Escape');
			expect(await cell(page, 2, 1).innerText(), 'each pipe shows as a pipe').toBe('Plain words in a cell. a|b c|d e|f');
			await savedContaining(page, seeded, '| Plain words in a cell. a\\|b c\\|d e\\|f | open |');

			await cell(page, 3, 1).click();
			await editorInCell(page, cell(page, 3, 1));
			await page.keyboard.press(keys.docEnd);
			await paste(page, ' pasted line one\npasted line two');
			const wide = await savedRow(page, seeded, 'line two', '| Wide |');
			expect(wide, 'the pasted lines stayed on the row, the break a space').toContain('those. pasted line one pasted line two | open |');
			await page.keyboard.press('Enter');
			expect(await firstTable(page).locator('tr').count(), 'no row was added').toBe(4);
			await frame(page, `03-pasted-lines-${at}`);
		});

		test('a short cell in a tall row opens with its text where it was', async ({ page }) => {
			test.setTimeout(120_000);
			await openTableDocument(page, layout);
			// The Wide row is as tall as its long note; the cell beside it holds one word, drawn at the middle of the row.
			const short = cell(page, 3, 2);
			const wordTop = () => page.evaluate(() => {
				const td = document.querySelector('.cm-md-table table')!.querySelectorAll('tr')[3].querySelectorAll('td')[2];
				const editor = document.querySelector('.cm-md-cell-editor .cm-content');
				// The word's own text, not the boxes around it: the editor's line box is taller than the glyphs it holds.
				const walker = document.createTreeWalker(editor ?? td, NodeFilter.SHOW_TEXT);
				let text = walker.nextNode();
				while (text && !text.textContent!.trim()) text = walker.nextNode();
				const range = document.createRange();
				range.selectNodeContents(text!);
				return Math.round(range.getBoundingClientRect().top);
			});
			const atRest = await wordTop();
			const rowTop = (await short.boundingBox())!.y;
			expect(atRest - rowTop, 'setup: the word sits well below the top of its tall row').toBeGreaterThan(20);
			await frame(page, `07-tall-row-${at}-rest`);
			if (layout.project === 'chromium-mobile') await short.tap(); else await short.click();
			await editorInCell(page, short);
			expect(Math.abs((await wordTop()) - atRest), 'opening the cell leaves its word at the same height').toBeLessThanOrEqual(1);
			await frame(page, `07-tall-row-${at}-open`);
		});
	});
}
