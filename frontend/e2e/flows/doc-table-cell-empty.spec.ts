import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { cell, cellContent, DOC, editorIn, frame, LAYOUTS, onlyIn, openTableDocument } from '../support/tableCells';

/**
 * An empty cell has no text to select for a comment, so while its editor is open and still empty the Comment offer is the
 * whole cell's, in the place a tap on an empty cell puts it. The first character typed takes the offer away; deleting it
 * brings the offer back. At 393 the offer sits clear of the cell editor's line and inside the visible page.
 */

const FILLED = '| Signs | Plain words in a cell. | open |';
const EMPTY_DOC = DOC.replace(FILLED, '| Signs | Plain words in a cell. |  |');

/** The offer's box against the editor's line and the visible page: what keeps it off the text and above a phone's keyboard. */
async function offerClear(page: Page) {
	const [offer, line, viewport] = await Promise.all([
		page.locator('.sel-comment').boundingBox(),
		page.locator('.cm-md-cell-editor .cm-line').first().boundingBox(),
		page.evaluate(() => ({ top: visualViewport?.offsetTop ?? 0, height: visualViewport?.height ?? innerHeight, width: innerWidth })),
	]);
	expect(offer, 'the offer is drawn').not.toBeNull();
	expect(line, 'the cell editor\'s line is drawn').not.toBeNull();
	const apart = offer!.y + offer!.height <= line!.y + 0.5 || offer!.y >= line!.y + line!.height - 0.5;
	const across = offer!.x + offer!.width <= line!.x + 0.5 || offer!.x >= line!.x + line!.width - 0.5;
	expect(apart || across, `the offer ${JSON.stringify(offer)} covers nothing of the editor's line ${JSON.stringify(line)}`).toBe(true);
	expect(offer!.y, 'the offer starts inside the visible page').toBeGreaterThanOrEqual(viewport.top);
	expect(offer!.y + offer!.height, 'and ends inside it').toBeLessThanOrEqual(viewport.top + viewport.height);
	expect(offer!.x + offer!.width, 'and is not cut off at the side').toBeLessThanOrEqual(viewport.width);
	return { offer: offer!, line: line!, viewport };
}

for (const layout of LAYOUTS) {
	const at = layout.width;
	test.describe(`@flow:doc-table-cell-empty @surface:documents at ${at}`, () => {
		test.beforeEach(({}, testInfo) => onlyIn(layout, testInfo));

		test('an empty cell being edited offers a comment on the whole cell until something is typed in it', async ({ page }, testInfo) => {
			test.setTimeout(120_000);
			await openTableDocument(page, layout, { text: EMPTY_DOC });
			const empty = cell(page, 2, 2);
			await empty.scrollIntoViewIfNeeded();
			if (layout.project === 'chromium-mobile') await empty.tap(); else await empty.click();
			await expect(editorIn(page)).toBeVisible();
			await expect(cellContent(page), 'the empty cell is open for typing').toBeFocused();

			const offer = page.locator('.sel-comment');
			await expect(offer, 'the empty cell offers a comment on the whole cell').toBeVisible();
			testInfo.annotations.push({ type: 'offer-boxes', description: JSON.stringify(await offerClear(page)) });
			await frame(page, `06-empty-cell-offer-${at}`);

			await page.keyboard.type('x');
			await expect(offer, 'the first character typed takes the offer away').toHaveCount(0);
			await frame(page, `06-empty-cell-typed-${at}`);

			await page.keyboard.press('Backspace');
			await expect(offer, 'emptying the cell brings the offer back').toBeVisible();
			await offerClear(page);

			// The offer still starts a comment: the press on it closes the cell editor, and the offer has to outlast that.
			if (layout.project === 'chromium-mobile') await offer.tap(); else await offer.click();
			await expect(page.getByPlaceholder('Comment, or @ to tag an agent').last(), 'the offer opens a comment on the cell').toBeVisible();
			await frame(page, `06-empty-cell-composer-${at}`);
		});
	});
}
