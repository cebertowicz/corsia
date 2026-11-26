import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { cell, cellContent, editorInCell, firstTable, keysOf, longPressThenSelect, openTableDocument, settled } from '../support/tableCells';

const ABOVE = 'A short table of trail work.';
const BELOW = 'The next paragraph follows the table.';
const DOC = [
	'# Trail notes', '', ABOVE, '',
	'| Place | Status |', '| --- | --- |', '| North path | Open |', '| Creek bridge | Ready |', '', BELOW, ''
].join('\n');
const WIDE_DOC = [
	'# Trail notes', '', ABOVE, '',
	'| Place name | Route status | Trail surface | Safety notes | Access details | Work planned for the months ahead |',
	'| --- | --- | --- | --- | --- | --- |',
	'| North path | Open | Paved | Clear | Public | Routine |', '', BELOW, ''
].join('\n');

/** Gap between the table border and the actual text above and below it, in CSS pixels. */
async function gaps(page: Page) {
	return page.evaluate(({ above, below }) => {
		const line = (text: string) => [...document.querySelectorAll('.cm-line')].find((el) => el.textContent?.includes(text));
		const rect = (el: Element | undefined) => {
			if (!el) throw new Error('Expected paragraph was not drawn');
			const range = document.createRange();
			range.selectNodeContents(el);
			return range.getBoundingClientRect();
		};
		const table = document.querySelector('.cm-md-table table');
		if (!table) throw new Error('Expected table was not drawn');
		const box = table.getBoundingClientRect();
		return { above: box.top - rect(line(above)).bottom, below: rect(line(below)).top - box.bottom };
	}, { above: ABOVE, below: BELOW });
}

for (const width of [393, 320]) {
	test.describe(`@flow:doc-table-band @surface:documents touch ${width}`, () => {
		test.beforeEach(({}, testInfo) => { if (testInfo.project.name !== 'chromium-mobile') testInfo.skip(); });
		const layout = { width, height: 851 };

		test('ordinary spacing follows the table in Reading', async ({ page }) => {
			test.setTimeout(120_000);
			await openTableDocument(page, layout, { text: DOC, quoted: 'trail work', editing: false });
			await settled(page);
			expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true);
			const space = await gaps(page);
			expect(space.below, JSON.stringify(space)).toBeLessThanOrEqual(space.above + 8);
		});

		test('a selected cell word opens Comment from the bottom bar in Edit', async ({ page }) => {
			test.setTimeout(120_000);
			await openTableDocument(page, layout, { text: DOC, quoted: 'trail work' });
			const target = cell(page, 1, 0);
			await target.tap();
			await editorInCell(page, target);
			await longPressThenSelect(cellContent(page), 'North');
			await expect.poll(() => page.evaluate(() => document.getSelection()?.toString())).toBe('North');
			const bar = page.locator('.touch-comment-bar.offered');
			const floating = page.locator('.sel-comment:not(.touch-comment-bar .sel-comment)');
			const comment = bar.getByRole('button', { name: 'Comment', exact: true });
			await expect(comment).toBeVisible();
			await expect(floating).toHaveCount(0);
			await comment.click();
			await expect(page.getByPlaceholder('Comment, or @ to tag an agent')).toBeVisible();
			await expect(floating).toHaveCount(0);
		});
	});
}

test.describe('@flow:doc-table-band @surface:documents fine pointer 760', () => {
	test.beforeEach(({}, testInfo) => { if (testInfo.project.name !== 'chromium-desktop') testInfo.skip(); });
	test('the existing below-table space and floating offer remain', async ({ page }) => {
		test.setTimeout(120_000);
		await openTableDocument(page, { width: 760, height: 851 }, { text: WIDE_DOC, quoted: 'trail work' });
		await settled(page);
		expect(await page.evaluate(() => matchMedia('(pointer: fine)').matches)).toBe(true);
		const target = cell(page, 1, 0);
		await target.click();
		await editorInCell(page, target);
		const keys = await keysOf(page);
		await page.keyboard.press(keys.docStart);
		for (let index = 0; index < 'North'.length; index++) await page.keyboard.press('Shift+ArrowRight');
		const offer = page.locator('.sel-comment');
		await expect(offer).toBeVisible();
		const tableBox = await firstTable(page).boundingBox();
		const offerBox = await offer.boundingBox();
		expect(tableBox).not.toBeNull();
		expect(offerBox).not.toBeNull();
		const paragraphTop = await page.locator('.cm-line').filter({ hasText: BELOW }).first().evaluate((line) => {
			const range = document.createRange();
			range.selectNodeContents(line);
			return range.getBoundingClientRect().top;
		});
		expect(offerBox!.y).toBeGreaterThanOrEqual(tableBox!.y + tableBox!.height);
		expect(offerBox!.y + offerBox!.height).toBeLessThanOrEqual(paragraphTop);
	});
});
