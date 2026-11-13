import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { cell, DOC, editorInCell, LAYOUTS, LONG, onlyIn, openTableDocument, savedText, type Layout } from '../support/tableCells';

/**
 * Text typed on the line directly under a table in the middle of a document is its own paragraph, whichever way the reader
 * got there: Escape, Tab from the last cell, Enter from the last row, a click or tap on the line, or ArrowDown from above.
 * The saved markdown keeps the table and the paragraph after it as they were, with a blank line on each side of the text.
 */

const LAST_ROW = `| Wide | ${LONG} | open |`;
const WANT = DOC.replace(`${LAST_ROW}\n\n`, `${LAST_ROW}\n\nx\n\n`);

/** The document's own caret: the line above the one it is on, and whether the document has the focus. */
const documentCaret = (page: Page) => page.evaluate(() => {
	const content = document.querySelector('.cm-content:not(.cm-md-cell-editor .cm-content)') as (HTMLElement & { cmTile?: { root: { view: any } } }) | null;
	const view = content?.cmTile?.root.view;
	if (!view) return null;
	const line = view.state.doc.lineAt(view.state.selection.main.head);
	return { line: line.text as string, above: line.number > 1 ? (view.state.doc.line(line.number - 1).text as string) : '', focused: view.hasFocus as boolean };
});

const underTable = async (page: Page) => {
	await expect(page.locator('.cm-md-cell-editor')).toHaveCount(0);
	await expect.poll(() => documentCaret(page), { message: 'the caret is on the line directly under the table' }).toEqual({ line: '', above: LAST_ROW, focused: true });
};

const ROUTES: Array<[string, (page: Page, layout: Layout) => Promise<void>]> = [
	['Escape', async (page) => {
		await cell(page, 2, 1).click({ position: { x: 6, y: 6 } });
		await editorInCell(page, cell(page, 2, 1));
		await page.keyboard.press('Escape');
	}],
	['Tab from the last cell', async (page) => {
		await cell(page, 3, 2).click({ position: { x: 6, y: 6 } });
		await editorInCell(page, cell(page, 3, 2));
		await page.keyboard.press('Tab');
	}],
	['Enter from the last row', async (page) => {
		await cell(page, 3, 0).click({ position: { x: 6, y: 6 } });
		await editorInCell(page, cell(page, 3, 0));
		await page.keyboard.press('Enter');
	}],
	['a click on the line', async (page, layout) => {
		// The blank line drawn right after the first table.
		const line = page.locator('.cm-md-table').first().locator('xpath=following-sibling::*[contains(@class, "cm-line")][1]');
		if (layout.project === 'chromium-mobile') await line.tap(); else await line.click();
	}],
	['ArrowDown from the line above', async (page) => {
		await page.locator('.cm-line', { hasText: 'Intro paragraph above the tables.' }).click();
		await page.keyboard.press('End');
		for (let press = 0; press < 6; press++) {
			if ((await documentCaret(page))?.above === LAST_ROW) break;
			await page.keyboard.press('ArrowDown');
		}
	}],
];

for (const layout of LAYOUTS.filter((l) => l.width !== 900)) {
	test.describe(`@flow:doc-table-cell-below @surface:documents at ${layout.width}`, () => {
		test.beforeEach(({}, testInfo) => onlyIn(layout, testInfo));
		for (const [name, arrive] of ROUTES) {
			test(`after ${name}, text typed under a mid-document table is its own paragraph`, async ({ page }) => {
				test.setTimeout(120_000);
				const seeded = await openTableDocument(page, layout);
				await arrive(page, layout);
				await underTable(page);
				expect(await savedText(page, seeded), 'arriving changes nothing').toBe(DOC);
				await page.keyboard.type('x');
				await expect.poll(() => savedText(page, seeded), { message: 'the saved markdown', timeout: 20_000 }).toBe(WANT);
				await expect(page.locator('.cm-md-table').first().locator('tr'), 'the table keeps its rows').toHaveCount(4);
				await expect(page.locator('.cm-line', { hasText: 'Paragraph below the first table.' }), 'the next paragraph is still drawn as one').toHaveCount(1);
			});
		}
	});
}
