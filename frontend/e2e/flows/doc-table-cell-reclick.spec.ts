import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { cell, cellContent, editorIn, frame, LAYOUTS, onlyIn, openTableDocument } from '../support/tableCells';

/**
 * Words of an open cell that a comment is drawn on: after a click on one of them the reader types, and the character lands
 * at the click with the cell still open and the caret still in it. Repeated, because the failure it guards was a race.
 */

const REPEATS = 20;

/** The nth space-separated word of the open cell's text: where its middle is on screen, and where it starts and ends in the text. */
const wordAt = (page: Page, index: number) => cellContent(page).evaluate((root, index) => {
	const nodes: Text[] = [];
	const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
	for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n as Text);
	const text = nodes.map((n) => n.data).join('');
	const words = [...text.matchAll(/\S+/g)];
	const word = words[index % words.length];
	const at = (offset: number) => {
		let left = offset;
		for (const n of nodes) { if (left <= n.data.length) return { node: n, offset: left }; left -= n.data.length; }
		return { node: nodes[nodes.length - 1], offset: nodes[nodes.length - 1].data.length };
	};
	const range = document.createRange();
	const start = at(word.index!), end = at(word.index! + word[0].length);
	range.setStart(start.node, start.offset);
	range.setEnd(end.node, end.offset);
	const box = range.getBoundingClientRect();
	return { x: box.left + box.width / 2, y: box.top + box.height / 2, text, from: word.index!, to: word.index! + word[0].length };
}, index);

const typedAt = (before: string, after: string) => {
	if (after.length !== before.length + 1) return -1;
	let i = 0;
	while (i < before.length && before[i] === after[i]) i++;
	return before.slice(i) === after.slice(i + 1) ? i : -1;
};

for (const layout of LAYOUTS) {
	if (layout.project !== 'chromium-desktop') continue;
	const at = layout.width;
	test.describe(`@flow:doc-table-cell-reclick @surface:documents at ${at}`, () => {
		test.beforeEach(({}, testInfo) => onlyIn(layout, testInfo));

		test(`a click on a commented word of the open cell, then typing, puts the character at the click ${REPEATS} times in a row`, async ({ page }) => {
			test.setTimeout(240_000);
			await openTableDocument(page, layout);
			const note = cell(page, 1, 1);
			await note.scrollIntoViewIfNeeded();
			const first = await note.evaluate((td) => { const r = document.createRange(); r.selectNodeContents(td); const b = r.getBoundingClientRect(); return { x: b.left + b.width / 2, y: b.top + b.height / 2 }; });
			await page.mouse.click(first.x, first.y);
			await expect(editorIn(page)).toBeVisible();
			await page.keyboard.type('Q');
			const failures: string[] = [];
			for (let i = 0; i < REPEATS; i++) {
				// Wait out the header's "write is still settling" notice: it shifts the document 78px for ~0.5s after typing, so a click aimed then lands a row off, in prose too.
				await page.waitForTimeout(2000);
				await expect(page.locator('.live-block')).toHaveCount(0);
				const before = await wordAt(page, 1 + (i % 4));
				await page.mouse.click(before.x, before.y);
				await page.keyboard.type('x');
				const after = await expect.poll(async () => (await wordAt(page, 0)).text, { timeout: 3000 }).not.toBe(before.text).then(async () => (await wordAt(page, 0)).text, async () => before.text);
				const where = typedAt(before.text, after), open = await editorIn(page).count(), focused = await cellContent(page).evaluate((el) => el === document.activeElement).catch(() => false);
				if (where < before.from || where > before.to || open !== 1 || !focused) {
					failures.push(`#${i}: typed at ${where} (word ${before.from}-${before.to}), editors ${open}, focused ${focused}`);
					await frame(page, `reclick-${at}-fail-${i}`);
					if (open !== 1) break;
				}
			}
			expect(failures, 'every click-and-type put the character at the click with the caret still in the cell').toEqual([]);
			await frame(page, `reclick-${at}-done`);
		});
	});
}
