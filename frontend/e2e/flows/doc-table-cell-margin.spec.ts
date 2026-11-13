import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { cell, frame, LAYOUTS, onlyIn, openTableDocument, savedText, type Seeded } from '../support/tableCells';

/**
 * A press just outside a table's left or right edge, then typing, must never leave a caret that joins the table: the press is
 * the nearest cell's, so what is typed lands in that cell, and every other line of the saved file is as it was.
 */

const TEXT = [
	'# Margin table', '', 'Intro paragraph above the table.', '',
	'| Area | Review note | State |', '| --- | --- | --- |',
	'| Navigation | The trailhead sign points the wrong way. | done |',
	'| Empty |  | open |',
	'| Short | Only two |',
	'| Plain | Some words here. | open |', '',
	'Paragraph after the table.', '',
].join('\n');
const LINES = TEXT.split('\n');

/** The saved file once it holds the typed text. */
async function savedWith(page: Page, seeded: Seeded, typed: string) {
	await expect.poll(async () => (await savedText(page, seeded)).includes(typed), { message: `the saved file holds ${typed}`, timeout: 20_000 }).toBe(true);
	return (await savedText(page, seeded)).split('\n');
}

for (const layout of LAYOUTS) {
	const at = layout.width;
	test.describe(`@flow:doc-table-cell-margin @surface:documents at ${at}`, () => {
		test.beforeEach(({}, testInfo) => onlyIn(layout, testInfo));

		for (const side of ['left', 'right'] as const) {
			test(`a press just outside the table's ${side} edge and typing leave the table's rows and the paragraph after it as they were`, async ({ page }) => {
				test.setTimeout(120_000);
				const seeded = await openTableDocument(page, layout, { text: TEXT });
				await cell(page, 1, 1).scrollIntoViewIfNeeded();
				// The table's visible box: on a phone a wide table scrolls inside its own box, which is what ends at the margin.
				const table = (await page.locator('.cm-md-table .table-wrap').first().boundingBox())!;
				const x = side === 'left' ? table.x - 3 : table.x + table.width + 3, y = table.y + table.height / 2;
				await frame(page, `margin-${side}-${at}-before`);
				if (layout.project === 'chromium-mobile') await page.touchscreen.tap(x, y);
				else await page.mouse.click(x, y);
				await page.keyboard.type('QQ', { delay: 80 });
				await frame(page, `margin-${side}-${at}-typed`);
				const saved = await savedWith(page, seeded, 'QQ');
				const typed = saved.filter((line) => line.includes('QQ'));
				expect(typed, 'the typed text is in one place').toHaveLength(1);
				expect(typed[0], 'which is a cell of the table').toMatch(/^\|.*QQ.*\|$/);
				expect(saved.map((line) => line.replace('QQ', '')), 'every other character of the file is as it was').toEqual(LINES);
			});
		}
	});
}
