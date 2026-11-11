import { expect, type Locator, type Page } from '@playwright/test';
import { test } from '../support/test';
import { routeDocumentSaves } from '../support/docSaveCue';
import {
	cell, cellContent, editorIn, editorInCell, firstTable, frame, keysOf, LAYOUTS, onlyIn, openTableDocument, savedContaining, savedText, settled, sheetAtRest
} from '../support/tableCells';

/**
 * An open table cell reads as the cell does at rest, a click on commented words opens that cell with the caret at the click, the
 * Comment offer covers no text, a press on a table's padding opens the nearest cell, cells are a finger tall, and a failed or
 * pending save, a long table and a wide one all show what the reader typed.
 */

const NOTE = 'The trailhead sign points the wrong way.';
const MARKS = '**bold** *italic* ~~struck~~ [link](https://example.com) `code` end';
const DOC = [
	'# Review table', '',
	'Intro paragraph above the table.', '',
	'| Area | Review note | State |', '| --- | --- | --- |',
	`| Navigation | ${NOTE} | done |`,
	`| Marks | ${MARKS} | open |`,
	'| Empty |  | open |',
	'| Plain | Some words here. | open |', '',
	'Paragraph after the table.', '',
].join('\n');
const QUOTED = 'points the wrong way';

const LONGTABLE = [
	'# Long and wide', '',
	'Intro paragraph above the tables.', '',
	'| Row | Note |', '| --- | --- |',
	...Array.from({ length: 40 }, (_, i) => `| ${i + 1} | Row number ${i + 1} of the long table. |`), '',
	'Between the tables.', '',
	`| ${Array.from({ length: 9 }, (_, i) => `Column ${i + 1}`).join(' | ')} |`, `| ${Array.from({ length: 9 }, () => '---').join(' | ')} |`,
	`| ${Array.from({ length: 9 }, (_, i) => `A wide cell ${i + 1} with some words in it`).join(' | ')} |`, '',
	'After the tables.', '',
].join('\n');

type Box = { x: number; y: number; width: number; height: number };
const boxOf = async (target: Locator): Promise<Box> => {
	await settled(target.page());
	await sheetAtRest(target);
	const box = await target.boundingBox();
	expect(box, 'drawn').not.toBeNull();
	return box!;
};
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
		return { x: r.left, y: r.top, width: r.width, height: r.height };
	}
	throw new Error(`no "${words}" drawn`);
}, words);
const touch = (page: Page) => page.evaluate(() => matchMedia('(pointer: coarse)').matches);
/** A press at a point, as a finger or a mouse makes one. */
const pressAt = async (page: Page, x: number, y: number) => {
	if (await touch(page)) await page.touchscreen.tap(x, y);
	else await page.mouse.click(x, y, { delay: 80 });
};
const intersects = (a: Box, b: Box) => a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
/** Every drawn line of text on the page's document outside the open cell's editor. */
const textBoxes = (page: Page): Promise<Box[]> => page.evaluate(() => {
	const out: Box[] = [];
	const walk = document.createTreeWalker(document.querySelector('.cm-content')!, NodeFilter.SHOW_TEXT);
	for (let node = walk.nextNode(); node; node = walk.nextNode()) {
		if (!node.textContent!.trim() || node.parentElement?.closest('.cm-md-cell-editor')) continue;
		const range = document.createRange();
		range.selectNodeContents(node);
		for (const r of range.getClientRects()) if (r.width > 0 && r.height > 0) out.push({ x: r.left, y: r.top, width: r.width, height: r.height });
	}
	return out;
});
/** Two frames of the page's own painting, so a state that follows the last keystroke has been drawn. */
const painted = (page: Page) => page.evaluate(() => new Promise<void>((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
const rectOf = (el: Locator): Promise<Box> => boxOf(el);
const noCaret = (page: Page) => page.addStyleTag({ content: '.cm-cursor, .cm-cursorLayer { visibility: hidden !important } .cm-md-cell-editor .cm-content { caret-color: transparent !important }' });

for (const layout of LAYOUTS) {
	const at = layout.width;
	test.describe(`@flow:doc-table-cell-review @surface:documents at ${at}`, () => {
		test.beforeEach(({}, testInfo) => onlyIn(layout, testInfo));

		test('an open cell is pixel-identical to the cell at rest, marks included, and a padded cell takes typing with no stray space', async ({ page }) => {
			test.setTimeout(120_000);
			const seeded = await openTableDocument(page, layout, { text: DOC, quoted: QUOTED });
			const marks = cell(page, 2, 1);
			await marks.scrollIntoViewIfNeeded();
			await noCaret(page);
			await page.mouse.move(2, 2);
			const box = await boxOf(marks);
			const rest = await page.screenshot({ clip: box, animations: 'disabled' });
			await frame(page, `f1-rest-${at}`);
			await marks.click({ position: { x: 4, y: 6 } });
			await expect(editorIn(page)).toBeVisible();
			await expect(cellContent(page)).toBeFocused();
			await page.mouse.move(2, 2);
			await settled(page);
			const open = await page.screenshot({ clip: box, animations: 'disabled' });
			await frame(page, `f1-open-${at}`);
			if (process.env.CORSIA_CELL_FRAMES) {
				const fs = await import('node:fs');
				fs.writeFileSync(`${process.env.CORSIA_CELL_FRAMES}/f1-cell-rest-${at}.png`, rest);
				fs.writeFileSync(`${process.env.CORSIA_CELL_FRAMES}/f1-cell-open-${at}.png`, open);
			}
			const differing = await page.evaluate(async ([a, b]) => {
				const load = (data: string) => new Promise<HTMLImageElement>((resolve) => { const img = new Image(); img.onload = () => resolve(img); img.src = `data:image/png;base64,${data}`; });
				const pixels = async (data: string) => {
					const img = await load(data), canvas = document.createElement('canvas');
					canvas.width = img.width; canvas.height = img.height;
					const context = canvas.getContext('2d')!;
					context.drawImage(img, 0, 0);
					return context.getImageData(0, 0, img.width, img.height);
				};
				const [x, y] = await Promise.all([pixels(a), pixels(b)]);
				// Compared through a 7px box blur: a page on a fractional pixel draws the same text half a pixel apart in the
				// editor's own layer, which a blur absorbs; a line that moved, a ring, a tint or a changed weight it does not.
				const blur = (image: ImageData) => {
					const { width, height, data } = image, reach = 3;
					let from = Float32Array.from(data);
					for (const horizontal of [true, false]) {
						const to = new Float32Array(from.length);
						for (let py = 0; py < height; py++) for (let px = 0; px < width; px++) for (let c = 0; c < 3; c++) {
							let sum = 0;
							for (let k = -reach; k <= reach; k++) {
								const qx = horizontal ? Math.min(width - 1, Math.max(0, px + k)) : px;
								const qy = horizontal ? py : Math.min(height - 1, Math.max(0, py + k));
								sum += from[(qy * width + qx) * 4 + c];
							}
							to[(py * width + px) * 4 + c] = sum / (2 * reach + 1);
						}
						from = to;
					}
					return from;
				};
				const [bx, by] = [blur(x), blur(y)];
				let count = 0, max = 0, strong = 0, left = Infinity, top = Infinity, right = -1, bottom = -1;
				for (let i = 0; i < bx.length; i += 4) {
					const delta = Math.max(Math.abs(bx[i] - by[i]), Math.abs(bx[i + 1] - by[i + 1]), Math.abs(bx[i + 2] - by[i + 2]));
					max = Math.max(max, delta);
					if (delta <= 24) continue;
					strong++; count++;
					const at = i / 4, px = at % x.width, py = Math.floor(at / x.width);
					left = Math.min(left, px); right = Math.max(right, px); top = Math.min(top, py); bottom = Math.max(bottom, py);
				}
				return { count, strong, max, left, top, right, bottom, width: x.width, height: x.height };
			}, [rest.toString('base64'), open.toString('base64')]);
			expect(differing.count, `the open cell draws the same pixels as the cell at rest, in its bold, italic, struck, link and code marks (${JSON.stringify(differing)})`).toBe(0);
			await page.keyboard.press('Escape');

			// A padded (empty) cell saves what is typed with no space after the first character.
			const empty = cell(page, 3, 1);
			await empty.click();
			await editorInCell(page, empty);
			await page.keyboard.type('world');
			await page.keyboard.press('Escape');
			await savedContaining(page, seeded, '| Empty | world | open |');
			await frame(page, `f3-padded-typed-${at}`);
		});

		test('a click on commented words opens the cell with the caret at the click, keeps the card, and typed text lands there', async ({ page }) => {
			test.setTimeout(120_000);
			const seeded = await openTableDocument(page, layout, { text: DOC, quoted: QUOTED });
			const target = cell(page, 1, 1);
			await target.scrollIntoViewIfNeeded();
			const wrong = await wordsIn(target, 'wrong');
			await pressAt(page, wrong.x + 1, wrong.y + wrong.height / 2);
			await expect(page.getByText('Which way does it point now?').first()).toBeVisible();
			await settled(page);
			await page.evaluate(() => new Promise<void>((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
			await expect(page.getByText('Which way does it point now?').first(), 'the card is still open').toBeVisible();
			await editorInCell(page, target);
			await frame(page, `f4-opened-at-click-${at}`);
			await page.keyboard.type('Q');
			await savedContaining(page, seeded, 'points the Qwrong way');
			expect(await savedText(page, seeded), 'nothing reached the start of the document').toMatch(/^# Review table/);
			await frame(page, `f4-typed-at-click-${at}`);
		});

		test('the Comment offer covers no text: inside an empty cell, and beside or below the table for selected text', async ({ page }) => {
			test.setTimeout(120_000);
			await openTableDocument(page, layout, { text: DOC, quoted: QUOTED });
			const keys = await keysOf(page);
			const offer = page.locator('.sel-comment');
			const empty = cell(page, 3, 1);
			await empty.scrollIntoViewIfNeeded();
			await empty.click();
			await editorInCell(page, empty);
			await expect(offer).toBeVisible();
			const inside = await boxOf(offer), cellBox = await rectOf(empty);
			expect(inside.x >= cellBox.x - 1 && inside.y >= cellBox.y - 1 && inside.x + inside.width <= cellBox.x + cellBox.width + 1 && inside.y + inside.height <= cellBox.y + cellBox.height + 1,
				`the offer sits inside the empty cell's box (${JSON.stringify({ inside, cellBox })})`).toBe(true);
			await frame(page, `f5-empty-offer-${at}`);
			await page.keyboard.press('Escape');

			const plain = cell(page, 4, 1);
			await plain.click();
			await editorInCell(page, plain);
			await page.keyboard.press(keys.docStart);
			for (let char = 0; char < 'Some words'.length; char++) await page.keyboard.press('Shift+ArrowRight');
			await expect(offer).toBeVisible();
			const shown = await boxOf(offer), table = await rectOf(firstTable(page));
			const covered = (await textBoxes(page)).filter((text) => intersects(text, shown));
			expect(covered, `the offer covers no text (${JSON.stringify({ shown, table, covered })})`).toEqual([]);
			expect(!intersects(shown, table), `and lies outside the table (${JSON.stringify({ shown, table })})`).toBe(true);
			await frame(page, `f5-selected-offer-${at}`);
		});

		test('a press on the table padding opens the nearest cell, and typing lands there', async ({ page }) => {
			test.setTimeout(120_000);
			const seeded = await openTableDocument(page, layout, { text: DOC, quoted: QUOTED });
			const table = firstTable(page);
			await table.scrollIntoViewIfNeeded();
			const plain = cell(page, 4, 1);
			await plain.click();
			await editorInCell(page, plain);
			const box = await rectOf(table);
			// Below the last row, in the table's own padding.
			await pressAt(page, box.x + box.width * 0.5, box.y + box.height + 4);
			await expect(cellContent(page), 'a cell is open and holds the focus').toBeFocused();
			await page.keyboard.type('QQQ');
			await savedContaining(page, seeded, 'QQQ');
			const below = await savedText(page, seeded);
			expect(below.split('\n').filter((line) => line.includes('QQQ')).length, 'in one table row').toBe(1);
			expect(below.split('\n').filter((line) => line.startsWith('|')).length, 'no row was added').toBe(DOC.split('\n').filter((line) => line.startsWith('|')).length);
			await frame(page, `f6-padding-below-${at}`);
			await page.keyboard.press('Escape');
			// On the table's left border.
			const left = await rectOf(table);
			await pressAt(page, left.x + 1, left.y + left.height * 0.5);
			await expect(cellContent(page), 'a cell is open and holds the focus').toBeFocused();
			await page.keyboard.type('LLL');
			await savedContaining(page, seeded, 'LLL');
			await frame(page, `f6-padding-left-${at}`);
		});

		test('table cells are a finger tall on a touch screen', async ({ page }) => {
			test.setTimeout(120_000);
			await openTableDocument(page, layout, { text: DOC, quoted: QUOTED });
			await firstTable(page).scrollIntoViewIfNeeded();
			const heights = await page.locator('.cm-md-table td, .cm-md-table th').evaluateAll((cells) => cells.map((el) => Math.round(el.getBoundingClientRect().height * 10) / 10));
			if (await touch(page)) expect(Math.min(...heights), `every cell is at least 44 px tall (${heights})`).toBeGreaterThanOrEqual(44);
			await frame(page, `f7-cells-${at}`);
		});

		test('a failed save, a pending save, a long table and a wide table', async ({ page }) => {
			test.setTimeout(180_000);
			const link = await routeDocumentSaves(page.context());
			const seeded = await openTableDocument(page, layout, { text: LONGTABLE, quoted: 'Intro paragraph above the tables.' });
			const keys = await keysOf(page);
			// A long table: a cell in the middle opens in place and takes typing.
			const middle = cell(page, 20, 1);
			await middle.scrollIntoViewIfNeeded();
			await middle.click();
			await editorInCell(page, middle);
			await page.keyboard.press(keys.docEnd);
			await page.keyboard.type(' mid');
			await savedContaining(page, seeded, 'Row number 20 of the long table. mid');
			await frame(page, `u3-many-rows-${at}`);
			await page.keyboard.press('Escape');

			// A pending save: the document's save reply is held until released.
			const first = cell(page, 1, 1);
			await first.scrollIntoViewIfNeeded();
			await first.click();
			await editorInCell(page, first);
			await link.hold();
			await page.keyboard.press(keys.docEnd);
			await page.keyboard.type(' pending');
			await expect(page.getByText('Saving…', { exact: true })).toBeVisible();
			await expect(first, 'the typed words stay in the cell').toContainText('pending');
			await frame(page, `u2-pending-save-${at}`);
			link.release();
			await savedContaining(page, seeded, 'Row number 1 of the long table. pending');
			await page.keyboard.press('Escape');

			// A save that cannot reach the server: the connection is cut. What was typed stays in the cell, and the document says so.
			const second = cell(page, 2, 1);
			await second.click();
			await editorInCell(page, second);
			await link.cut();
			await page.keyboard.press(keys.docEnd);
			await page.keyboard.type(' failed');
			await expect(second, 'the typed words stay in the cell').toContainText('failed');
			await expect(page.getByText(/Disconnected/).first(), 'the document says the save has not gone through').toBeVisible();
			await frame(page, `u1-failed-save-${at}`);
			link.restore();

			// A table wider than the pane: its last cell opens in place.
			const wide = page.locator('.cm-md-table table').nth(1);
			await wide.scrollIntoViewIfNeeded();
			const last = wide.locator('tr').nth(1).locator('td').nth(8);
			await last.scrollIntoViewIfNeeded();
			await last.click();
			await expect(cellContent(page)).toBeFocused();
			await frame(page, `u3-wide-table-${at}`);
		});
	});
}
