import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { openLiveDocument } from '../support/liveDocument';
import { cell, cellContent, DOC, editorInCell, frame, keysOf, LAYOUTS, onlyIn, openTableDocument, savedContaining } from '../support/tableCells';

/**
 * Another writer's live edit while the reader types in a cell behaves as it does in prose: the reader's caret stays where
 * they were typing, in the same cell, in another cell of the row, in another row and above the table. Each edit is a real
 * remote update from a second browser on the same live document. Every remote insert lands strictly before or away from
 * the reader's own place, never at the same offset, so the outcome never rests on how Yjs breaks a tie between clients.
 */

/** Inserts `text` in the writer's own editor, before the first occurrence of `before`. */
const writeBefore = (writer: Page, before: string, text: string) => writer.evaluate(({ before, text }) => {
	const content = document.querySelector('.cm-content') as (HTMLElement & { cmTile?: { root: { view: any } } }) | null;
	const view = content!.cmTile!.root.view;
	const at = view.state.doc.toString().indexOf(before);
	if (at < 0) throw new Error(`the writer's document has no ${before}`);
	view.dispatch({ changes: { from: at, insert: text } });
}, { before, text });

for (const layout of LAYOUTS) {
	const at = layout.width;
	test.describe(`@flow:doc-table-cell-live @surface:documents at ${at}`, () => {
		test.beforeEach(({}, testInfo) => onlyIn(layout, testInfo));

		test('a live edit from another writer while the reader types in a cell leaves their caret where it was', async ({ page, browser }) => {
			test.setTimeout(150_000);
			const seeded = await openTableDocument(page, layout);
			const writerContext = await browser.newContext({ viewport: { width: layout.width, height: layout.height } });
			try {
				const writer = await writerContext.newPage();
				await openLiveDocument(writer, {
					artifactId: seeded.artifactId, body: DOC, docPath: seeded.docPath, fileName: `${seeded.title}.md`, threadId: seeded.threadId, title: seeded.title
				}, 'Intro paragraph above the tables.');
				const keys = await keysOf(page);
				const plain = cell(page, 2, 1);
				await plain.click();
				await editorInCell(page, plain);
				await page.keyboard.press(keys.docEnd);
				await page.keyboard.type(' mine');

				// The same cell, before the reader's place, while they are still typing.
				await Promise.all([
					page.keyboard.type(' more', { delay: 80 }),
					(async () => { await page.waitForTimeout(120); await writeBefore(writer, 'Plain words in a cell.', 'W1 '); })(),
				]);
				await expect(plain, 'the writer\'s words reach the open cell').toContainText('W1 ');
				await page.keyboard.type(' end');
				await expect(plain, 'and the reader kept typing where they were').toHaveText('W1 Plain words in a cell. mine more end');

				// Another cell of the same row, another row, and a paragraph above the table, which moves the whole table down.
				await writeBefore(writer, 'open |\n| Wide', 'W2 ');
				await writeBefore(writer, 'The trailhead sign', 'W3 ');
				await writeBefore(writer, 'Intro paragraph', 'A new paragraph from the writer.\n\n');
				await expect(cell(page, 2, 2)).toHaveText('W2 open');
				await expect(cell(page, 1, 1)).toContainText('W3 The trailhead');
				await expect(page.locator('.cm-content').first()).toContainText('A new paragraph from the writer.');
				await page.keyboard.type('!');
				await expect(cellContent(page), 'the reader is still in the cell').toBeFocused();
				await expect(plain, 'and still typing at their own place').toHaveText('W1 Plain words in a cell. mine more end!');
				await editorInCell(page, plain);
				await frame(page, `06-live-edit-${at}`);
				await savedContaining(page, seeded, '| Signs | W1 Plain words in a cell. mine more end! | W2 open |');
			} finally {
				await writerContext.close();
			}
		});
	});
}
