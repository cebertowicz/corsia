import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { cell, cellContent, DOC, editorInCell, frame, keysOf, LAYOUTS, onlyIn, openTableDocument, savedContaining, savedRow, type Layout } from '../support/tableCells';

/**
 * Home, End, the arrows and their modifier forms mean the same thing in a cell as they do in prose, and they keep the
 * reader in the cell: leaving it is what Tab, Enter and Escape are for. Each key is followed by a typed mark, so where the
 * caret went is read off the cell and the saved file. The keys are the page's platform's: these projects emulate Windows
 * and Android, and one run per width fakes a Mac so its Cmd and Option keys are pressed too.
 */

/**
 * A table whose second column has room for its short text on one line at every width, so the line keys and the text keys
 * land in the same place there; the long note in the first table is where a wrapped line is tried.
 */
const KEYS_DOC = `${DOC}\n| Key | Short text |\n| --- | --- |\n| k | open |\n`;

async function caretKeys(page: Page, layout: Layout, name: string) {
	const seeded = await openTableDocument(page, layout, { text: KEYS_DOC });
	const keys = await keysOf(page);
	const short = page.locator('.cm-md-table table').nth(2).locator('tr').nth(1).locator('td').nth(1);
	let text = 'open', caret = 0;
	const typeMark = async (mark: string, why: string) => {
		await page.keyboard.type(mark);
		text = text.slice(0, caret) + mark + text.slice(caret);
		caret += mark.length;
		expect(await short.innerText(), why).toBe(text);
	};
	await short.click();
	await editorInCell(page, short);
	await page.keyboard.press(keys.docEnd);
	caret = text.length;
	await typeMark(' now', `${keys.docEnd} goes to the end of the cell's text, not past its padding`);
	await page.keyboard.press(keys.wordLeft);
	caret = text.lastIndexOf(' ') + 1;
	await typeMark('X', `${keys.wordLeft} goes back a word`);
	await page.keyboard.press(keys.docStart);
	caret = 0;
	await typeMark('1', `${keys.docStart} goes to the start of the cell's text`);
	for (const key of keys.lineEnd) {
		await page.keyboard.press(key);
		caret = text.length;
		await typeMark('2', `${key} goes to the end of the line`);
	}
	for (const key of keys.lineStart) {
		await page.keyboard.press(key);
		caret = 0;
		await typeMark('3', `${key} goes to the start of the line`);
	}
	await page.keyboard.press('ArrowRight');
	await page.keyboard.press('ArrowRight');
	caret += 2;
	await typeMark('4', 'an arrow moves one character');
	for (let step = 0; step < 30; step++) await page.keyboard.press('ArrowRight');
	caret = text.length;
	await typeMark('5', 'arrowing past the end stops at the end of the text');
	for (let step = 0; step < 40; step++) await page.keyboard.press('ArrowLeft');
	caret = 0;
	await typeMark('6', 'and arrowing past the start stops at its start');
	await expect(cellContent(page), 'the caret keys never take the reader out of the cell').toBeFocused();
	await savedContaining(page, seeded, `| ${text} |`);
	await frame(page, `04-caret-keys-${name}`);
	await page.keyboard.press('Escape');

	// A cell whose text wraps: End goes to the end of the line the caret is on, as in prose, and a second End to the end of
	// the text. At a width where the long note does not wrap, the first End is already the end of the text.
	const wrapped = cell(page, 3, 1);
	await wrapped.click({ position: { x: 6, y: 6 } });
	await editorInCell(page, wrapped);
	const lines = await cellContent(page).evaluate((el) => {
		const range = document.createRange();
		range.selectNodeContents(el);
		return new Set([...range.getClientRects()].map((rect) => Math.round(rect.top))).size;
	});
	if (layout.width >= 900) expect(lines, 'the long note wraps at this width').toBeGreaterThan(1);
	await page.keyboard.press('End');
	await page.keyboard.type('Q');
	await page.keyboard.press(keys.docEnd);
	await page.keyboard.type('W');
	const row = await savedRow(page, seeded, 'W |', '| Wide |');
	if (lines > 1) {
		expect(row, 'End stopped inside the text, at the end of its first line').toMatch(/^\| Wide \| A long .*Q.* those\.W \| open \|$/);
	} else {
		expect(row, 'one line: End is the end of the text').toContain('those.QW | open |');
	}
	await page.keyboard.press('Escape');
}

for (const layout of LAYOUTS) {
	test.describe(`@flow:doc-table-cell-keys @surface:documents at ${layout.width}`, () => {
		test.beforeEach(({}, testInfo) => onlyIn(layout, testInfo));

		test('the caret keys do their ordinary work inside a cell', async ({ page }) => {
			test.setTimeout(120_000);
			await caretKeys(page, layout, `${layout.width}`);
		});

		if (layout.width === 1440) {
			test('and a Mac\'s Cmd and Option forms of them do too', async ({ page }) => {
				test.setTimeout(120_000);
				// CodeMirror picks its Mac bindings from navigator.platform when it loads, so the page reports a Mac from the start.
				await page.addInitScript(() => Object.defineProperty(Navigator.prototype, 'platform', { get: () => 'MacIntel' }));
				await caretKeys(page, layout, 'mac-1440');
				expect((await keysOf(page)).mac, 'the page took the Mac bindings').toBe(true);
			});
		}
	});
}
