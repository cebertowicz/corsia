import { expect, type Locator, type Page } from '@playwright/test';
import { test } from '../support/test';
import {
	cell, cellContent, DOC, editorIn, editorInCell, frame, keysOf, LAYOUTS, longPressOn, onlyIn, openTableDocument, savedContaining, savedText, settled,
	sheetAtRest
} from '../support/tableCells';

/**
 * The comment flows still see a cell once cells are editable: text selected in an open cell offers a comment, a comment
 * drawn on a cell's text opens on a click and stays drawn while that cell is edited, and an image in a cell keeps its own
 * click. A read-only document never opens a cell editor. At 1280 with the Workspace panel over the document, a click
 * still reaches the cell where it is drawn.
 */

type Box = { x: number; y: number; right: number; bottom: number };
/** Where `target` rests: read once the offer's pop and the sheet's slide have finished, since both move what they draw. */
const boxOf = async (target: Locator): Promise<Box> => {
	await settled(target.page());
	await sheetAtRest(target);
	return target.evaluate((el) => {
		const r = el.getBoundingClientRect();
		return { x: r.left, y: r.top, right: r.right, bottom: r.bottom };
	});
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
		return { x: r.left, y: r.top, right: r.right, bottom: r.bottom };
	}
	throw new Error(`no "${words}" drawn`);
}, words);
const commentBox = (page: Page) => page.locator('.composer').filter({ has: page.getByPlaceholder('Comment, or @ to tag an agent') });
/** Where the offer sits against the selected words: across from where they end, and its middle from their top. */
/** Where the composer opened: its left, and its top below the selected words; on the screen when it opened in the phone's sheet. */
const composerUnder = async (composer: Locator, words: Box) => {
	const inSheet = await composer.evaluate((el) => !!el.closest('[data-adaptive-drawer]'));
	const box = await boxOf(composer);
	return inSheet ? box : { x: box.x, y: box.y - words.bottom };
};
/** Within 2px: subpixel layout. */
const near = (a: number, b: number) => Math.abs(a - b) <= 2;

for (const layout of LAYOUTS) {
	const at = layout.width;
	test.describe(`@flow:doc-table-cell-comments @surface:documents at ${at}`, () => {
		test.beforeEach(({}, testInfo) => onlyIn(layout, testInfo));

		test('a word selected in a cell, a commented cell and an image in a cell', async ({ page }) => {
			test.setTimeout(120_000);
			const seeded = await openTableDocument(page, layout);
			const keys = await keysOf(page);

			// A word selected inside the open cell, as in prose: the selection reaches the document, so the comment offer appears.
			// On a phone the reader has no keyboard: a long press on the word selects it.
			if (layout.project === 'chromium-mobile') {
				await cell(page, 3, 1).tap({ position: { x: 6, y: 6 } });
				await editorInCell(page, cell(page, 3, 1));
				await longPressOn(cellContent(page), 'long');
				await expect.poll(() => page.evaluate(() => document.getSelection()?.toString()), { message: 'the long press selected the word' }).toBe('long');
			} else {
				await cell(page, 3, 1).click({ position: { x: 6, y: 6 } });
				await editorInCell(page, cell(page, 3, 1));
				await page.keyboard.press(keys.docStart);
				for (let char = 0; char < 'A long'.length; char++) await page.keyboard.press('Shift+ArrowRight');
			}
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

		test('text selected in an open cell is offered Comment beside or below the table, and its composer opens as prose\'s does', async ({ page }) => {
			test.setTimeout(120_000);
			await openTableDocument(page, layout);
			const keys = await keysOf(page);
			const target = cell(page, 3, 1), offer = page.locator('.sel-comment'), composer = commentBox(page);
			const press = (l: Locator) => (layout.project === 'chromium-mobile' ? l.tap() : l.click());

			// Prose first: where the offer and composer go for the first word of the paragraph under the table.
			const line = page.locator('.cm-line', { hasText: 'Paragraph below the first table.' });
			await line.scrollIntoViewIfNeeded();
			await press(line);
			await page.keyboard.press('Home');
			for (let char = 0; char < 'Paragraph'.length; char++) await page.keyboard.press('Shift+ArrowRight');
			await expect(offer, 'setup: the prose selection is offered Comment').toBeVisible();
			const proseWords = await wordsIn(line, 'Paragraph');
			await offer.click();
			await expect(composer, 'setup: its composer opens').toBeVisible();
			const proseComposer = await composerUnder(composer, proseWords);
			await composer.getByRole('button', { name: 'Cancel', exact: true }).click();
			await expect(composer).toBeHidden();

			await target.scrollIntoViewIfNeeded();
			await target.click({ position: { x: 6, y: 6 } });
			await editorInCell(page, target);
			await page.keyboard.press(keys.docStart);
			for (let char = 0; char < 'A long'.length; char++) await page.keyboard.press('Shift+ArrowRight');
			await expect(offer).toBeVisible();
			const words = await wordsIn(page.locator('.cm-md-cell-editor'), 'A long'), shown = await boxOf(offer);
			// Unlike prose, where it sits beside the words, a table's offer sits beside or below the table: it covers no cell's text.
			const cells = await page.locator('.cm-md-table td, .cm-md-table th').evaluateAll((all) => all.map((el) => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; }));
			const covered = cells.filter((c) => shown.x < c.x + c.width && shown.x + shown.width > c.x && shown.y < c.y + c.height && shown.y + shown.height > c.y);
			expect(covered.length, `the offer covers no cell of the table (${JSON.stringify({ shown, words, covered })})`).toBe(0);
			await frame(page, `05-selection-offer-${at}`);
			await offer.click();
			await expect(composer).toBeVisible();
			const opened = await composerUnder(composer, words);
			// The cell's text is set smaller than prose, so the line it is drawn on ends a pixel or two nearer its words.
			expect(near(opened.x, proseComposer.x) && Math.abs(opened.y - proseComposer.y) <= 4,
				`the composer opens against the selected words' line as in prose (${JSON.stringify({ opened, proseComposer })})`).toBe(true);
			await frame(page, `05-selection-composer-${at}`);
		});

		// The offer sits by the rule a prose selection's does (1c7205810): above the rows the selection is drawn on, or below them
		// near the window's top, so it never covers selected text. A selection that runs from the table down into prose is drawn from
		// the table's top, so the offer sits above the table; it does not sit where the selection ends.
		const selectUpIntoTable = async (page: Page) => {
			const line = page.locator('.cm-line', { hasText: 'Paragraph below the first table.' });
			await line.scrollIntoViewIfNeeded();
			await line.click();
			await page.keyboard.press('Home');
			await page.keyboard.press('Shift+ArrowUp');
			await page.keyboard.press('Shift+ArrowUp');
			const offer = page.locator('.sel-comment');
			await expect(offer, 'the selection running into the table is offered Comment').toBeVisible();
			await expect(page.locator('.cm-md-cell-editor'), 'setup: no cell opened').toHaveCount(0);
			return { line, offer };
		};
		/** The boxes the selection is drawn in, and the cells of the table, as the browser draws them now. */
		const selectedBoxes = (page: Page) => page.evaluate(() => [...document.querySelectorAll('.cm-selectionBackground')].map((el) => { const r = el.getBoundingClientRect(); return { x: r.left, y: r.top, right: r.right, bottom: r.bottom }; }).filter((b) => b.right > b.x && b.bottom > b.y));
		const overlaps = (a: Box, b: Box) => a.x < b.right && a.right > b.x && a.y < b.bottom && a.bottom > b.y;

		test('a selection from below a table up into it is offered Comment above the rows it is drawn on, as prose\'s is', async ({ page }) => {
			test.skip(layout.project === 'chromium-mobile', 'Shift+Up selects with a keyboard');
			test.setTimeout(120_000);
			await openTableDocument(page, layout);
			const { offer } = await selectUpIntoTable(page);
			const shown = await boxOf(offer), table = await boxOf(page.locator('.cm-md-table table').first());
			expect(await offer.evaluate((el) => el.classList.contains('above')), 'above the selected rows').toBe(true);
			expect(await offer.evaluate((el) => el.classList.contains('below')), 'not the table\'s place below it').toBe(false);
			expect(shown.bottom <= table.y, `above the table the selection starts in, not beside where it ends (${JSON.stringify({ shown, table })})`).toBe(true);
			const covered = (await selectedBoxes(page)).filter((b) => overlaps(shown, b));
			expect(covered.length, `the offer covers no selected text (${JSON.stringify({ shown, covered })})`).toBe(0);
			await frame(page, `05-selection-into-table-${at}`);
		});

		test('a selection into a table whose top is scrolled out of view still has its Comment offer on screen', async ({ page }) => {
			test.skip(layout.project === 'chromium-mobile', 'Shift+Up selects with a keyboard');
			test.setTimeout(120_000);
			await openTableDocument(page, layout);
			// A window short enough that the document scrolls (the whole fixture fits in a tall one).
			await page.setViewportSize({ width: layout.width, height: 480 });
			await settled(page);
			const { offer } = await selectUpIntoTable(page);
			// The selection's rows now start above the visible area: scroll the table's top out of view, keeping the prose line under it on screen.
			const table = page.locator('.cm-md-table table').first();
			const tableTop = (await boxOf(table)).y;
			await page.locator('.cm-scroller').first().evaluate((el, by) => { el.scrollTop += by; }, tableTop + 40);
			await settled(page);
			await expect(offer, 'the offer is still there').toBeVisible();
			expect((await boxOf(table)).y, 'setup: the table\'s top is out of view').toBeLessThan(0);
			const shown = await boxOf(offer), height = await page.evaluate(() => window.innerHeight);
			expect(shown.y >= 0 && shown.bottom <= height, `the offer is on screen (${JSON.stringify({ shown, height })})`).toBe(true);
			const covered = (await selectedBoxes(page)).filter((b) => overlaps(shown, b));
			expect(covered.length, `the offer covers no selected text (${JSON.stringify({ shown, covered })})`).toBe(0);
			await frame(page, `05-selection-into-table-scrolled-${at}`);
		});

		test('a click on a comment covering a whole cell opens the comment and the cell, and the comment stays open', async ({ page }) => {
			test.setTimeout(120_000);
			const seeded = await openTableDocument(page, layout, { quoted: 'Plain words in a cell.' });
			// A whole-cell comment is drawn on all of the cell's text, as a text comment is.
			const mark = page.locator(`td [data-comment-ids*="${seeded.commentId}"]`).first();
			const whole = page.locator('td', { has: mark });
			await expect(mark, 'the comment is drawn on all of the cell\'s text').toHaveText('Plain words in a cell.');
			const phone = layout.project === 'chromium-mobile';
			if (phone) await mark.tap(); else await mark.click({ delay: 80 });
			await expect(page.getByText('Which way does it point now?').first()).toBeVisible();
			// Whatever would close the comment (the cell editor taking focus, the document reading the press) has run by the next two frames.
			await settled(page);
			await page.evaluate(() => new Promise<void>((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
			await expect(page.getByText('Which way does it point now?').first(), 'the comment is still open').toBeVisible();
			if (!phone) {
				await expect(editorIn(page), 'the cell opened with the comment, and is still open').toBeVisible();
				return;
			}
			// On a phone the comment is a modal sheet, as over prose: the cell closes under it, and Close opens it again where
			// the tap left the caret, so typing goes on there.
			await expect(editorIn(page), 'the cell closed under the sheet').toHaveCount(0);
			const drawer = page.locator('[data-adaptive-drawer] .drawer-panel');
			await drawer.getByRole('button', { name: 'Close', exact: true }).click();
			await expect(drawer).toHaveCount(0);
			await editorInCell(page, whole);
			await expect(cellContent(page), 'Close gives the cell its focus back').toBeFocused();
			await page.keyboard.type('Q');
			// Where in the words the tap put the caret is the browser's to say: the Q is somewhere in that cell's text.
			const typedInCell = (text: string) => text.split('\n').some((line) => line.includes('Q') && line.replace('Q', '').includes('Plain words in a cell.'));
			await expect.poll(async () => typedInCell(await savedText(page, seeded)), { message: 'the Q reached the commented cell', timeout: 20_000 }).toBe(true);
		});

		for (const card of [false, true]) {
			const name = card ? 'with a comment card open, a click or tap in a cell types where it landed' : 'a click or tap on a wrapped line of a cell types where it landed';
			test(name, async ({ page }) => {
				test.skip(at === 900, 'proven at the desktop and phone widths');
				test.setTimeout(120_000);
				const phone = layout.project === 'chromium-mobile';
				const seeded = await openTableDocument(page, layout);
				if (card) {
					await page.locator(`[data-comment-ids*="${seeded.commentId}"]`).first().click({ delay: 80 });
					await expect(page.getByText('Which way does it point now?').first(), 'setup: the comment card is open').toBeVisible();
				}
				await settled(page);
				if (card && phone) {
					// The sheet is modal: the cell the same tap opened closes under it, and no caret or keyboard is left beneath it.
					await expect(editorIn(page), 'the cell under the sheet closed').toHaveCount(0);
					expect(await page.evaluate(() => !!document.activeElement?.closest('.cm-editor')), 'no editor under the sheet holds the focus').toBe(false);
				}
				// Inside the long cell, clear of the card wherever it opens: just inside the start of "few", in the middle of its
				// line, so the caret goes in front of it and nowhere else. The cell wraps over several lines, so its editor has to
				// wrap them at the same words for the click to land on the word that was drawn there.
				const target = cell(page, 3, 1);
				const pointAtWord = async () => { const word = await wordsIn(target, 'few'); return { x: word.x + 1, y: (word.y + word.bottom) / 2 }; };
				const press = async () => {
					const point = await pointAtWord();
					if (phone) await page.touchscreen.tap(point.x, point.y);
					else await page.mouse.click(point.x, point.y, { delay: 80 });
				};
				const tag = `${card ? 'card-open' : 'wrapped'}-${at}`;
				await frame(page, `05-${tag}-before`);
				await press();
				if (card && phone) {
					// On a phone the card is a modal sheet, as every sheet is: the first tap outside it closes it and reaches no
					// cell under it; the next tap places the caret where it lands.
					await expect(page.getByText('Which way does it point now?').first(), 'the first tap closes the card').toBeHidden();
					await settled(page);
					await expect(target.locator('.cm-md-cell-editor'), 'and does not open the tapped cell').toHaveCount(0);
					await frame(page, `05-${tag}-closed`);
					await press();
				}
				await editorInCell(page, target);
				await expect(cellContent(page), 'focus is inside the cell editor').toBeFocused();
				await page.keyboard.type('ZZ');
				await savedContaining(page, seeded, 'and a ZZfew more after those. | open |');
				await frame(page, `05-${tag}-typed`);
			});
		}

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
