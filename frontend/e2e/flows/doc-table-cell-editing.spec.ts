import path from 'node:path';
import { expect, type Locator, type Page } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { test } from '../support/test';

/**
 * Editing a rendered table cell where it is read. A click puts an editor inside the cell whose text is the document's own,
 * so what is typed lands in the saved file and the row keeps its shape; Tab, Enter and Escape move through the table; the
 * document's undo takes a cell edit back out; and the comment flows still see a cell's text. Frames for the review pack are
 * written to the folder in CORSIA_CELL_FRAMES (skipped when unset, so CI keeps its own artifacts).
 */

const LONG = 'A long note that keeps going so the table needs more room than a phone has, with more words after it and a few more after those.';
const DOC = [
	'# Editing table cells', '',
	'Intro paragraph above the tables.', '',
	'| Area | Review note | Preview |', '| --- | --- | --- |',
	'| Navigation | The trailhead sign points the wrong way. | done |',
	'| Signs | Plain words in a cell. | open |',
	`| Wide | ${LONG} | open |`, '',
	'Paragraph below the first table.', '',
	'| Shot | Note |', '| --- | --- |',
	'| ![Mock screen](shots/a.png) | An image inside a cell. |', '',
	'Paragraph below the second table.', '',
].join('\n');

const frames = process.env.CORSIA_CELL_FRAMES;
const frame = async (page: Page, name: string) => {
	if (!frames) return;
	await page.screenshot({ path: path.join(frames, `${name}.png`), animations: 'disabled' });
};

type Seeded = { title: string; docPath: string; params: { token: string }; commentId: string };

/** Seed the document and a comment on one table cell, then open the document in the canvas. */
async function openDocument(page: Page, size: { width: number; height: number }): Promise<Seeded> {
	await page.setViewportSize(size);
	await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' });
	const request = page.request, params = { token: TEST_TOKEN };
	const title = `cell-editing-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const threadId = await createSession(request, title);
	const roots: Array<{ kind: string; path: string }> = (await (await request.get('/api/files/tree', { params })).json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`, docPath = path.join(workspace!, fileName);
	const seeded = await request.put('/api/files/content', { params, data: { path: docPath, text: DOC, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId: string = (await seeded.json()).artifact.id;
	const from = DOC.indexOf('The trailhead sign points the wrong way.');
	const quoted = 'trailhead sign points the wrong way';
	const created = await request.post(`/api/artifacts/${artifactId}/comments`, {
		params,
		data: { body: 'Which way does it point now?', anchor_text: quoted, from_off: from + 4, to_off: from + 4 + quoted.length, delivery_policy: 'immediate' }
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	const commentId: string = (await created.json()).comment.id;
	await page.addInitScript(({ threadId, docPath, fileName, artifactId }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: { tabs: [{ instanceId: 'edit-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'edit-tab', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { threadId, docPath, fileName, artifactId });
	await loginByUrl(page, { waitForComposer: false });
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.locator('.cm-md-table table').first()).toBeVisible({ timeout: 20_000 });
	return { title, docPath, params, commentId };
}

const savedText = async (page: Page, { docPath, params }: Seeded) => {
	const stored = await page.request.get('/api/files/content', { params: { ...params, path: docPath } });
	expect(stored.ok(), await stored.text()).toBeTruthy();
	return (await stored.json()).text as string;
};

/** Wait for the editor's autosave to reach the file, so a read-back proves what the reader left on screen. */
async function savedContaining(page: Page, seeded: Seeded, needle: string) {
	await expect.poll(() => savedText(page, seeded), { message: `the saved file holds ${needle}`, timeout: 20_000 }).toContain(needle);
}

/** The modifier CodeMirror reads as `Mod`: Cmd on a Mac, Ctrl elsewhere — the same key a person would press. */
const MOD = process.platform === 'darwin' ? 'Meta' : 'Control';

const firstTable = (page: Page) => page.locator('.cm-md-table table').first();
const cell = (page: Page, row: number, col: number) => firstTable(page).locator('tr').nth(row).locator('td, th').nth(col);
const editorIn = (page: Page) => page.locator('.cm-md-cell-editor .cm-editor');

/** The open cell's editor box, and the box of the cell holding it: the editor must sit inside the cell it edits. */
async function editorInCell(page: Page, target: Locator) {
	await expect(editorIn(page)).toBeVisible();
	const [editor, box] = await Promise.all([editorIn(page).boundingBox(), target.boundingBox()]);
	expect(editor, 'the editor is drawn').not.toBeNull();
	expect(box, 'the cell is drawn').not.toBeNull();
	const [e, b] = [editor!, box!];
	expect(e.x, 'the editor starts inside the cell').toBeGreaterThanOrEqual(b.x - 1);
	expect(e.y).toBeGreaterThanOrEqual(b.y - 1);
	expect(e.x + e.width, 'and ends inside it').toBeLessThanOrEqual(b.x + b.width + 1);
	return { editor: e, cell: b };
}

test.describe('@flow:doc-table-cell-editing @surface:documents', () => {
	test.beforeEach(({}, testInfo) => { test.skip(testInfo.project.name !== 'chromium-desktop', 'desktop widths'); });

	test('a click edits the cell, the file keeps what was typed, and the row keeps its columns', async ({ page }, testInfo) => {
		test.setTimeout(120_000);
		const seeded = await openDocument(page, { width: 1440, height: 1000 });
		await frame(page, '01-plain-cell-1440-before');

		// A click at the start of the cell's text puts the caret there, the way a click in prose does, rather than at some
		// default end of the cell.
		await cell(page, 2, 1).click({ position: { x: 6, y: 6 } });
		await editorInCell(page, cell(page, 2, 1));
		await expect(page.locator('.cm-md-cell-editor .cm-content')).toBeFocused();
		await page.keyboard.type('Start');
		expect(await cell(page, 2, 1).innerText(), 'the caret is where the click landed').toMatch(/Start Plain words/);
		await page.keyboard.press(`${MOD}+End`);
		await page.keyboard.type(' — checked');
		await frame(page, '01-plain-cell-1440-typing');
		expect(await cell(page, 2, 1).innerText()).toContain('checked');
		await savedContaining(page, seeded, 'Plain words in a cell — checked');
		expect(await firstTable(page).locator('tr').nth(2).locator('td').count(), 'the row still has three columns').toBe(3);

		// Tab to the next cell and Enter down a column: the reader moves through the table without leaving it.
		await page.keyboard.press('Tab');
		await editorInCell(page, cell(page, 2, 2));
		await page.keyboard.type('!');
		await page.keyboard.press('Enter');
		await editorInCell(page, cell(page, 3, 2));
		await page.keyboard.press('Escape');
		await expect(page.locator('.cm-md-cell-editor')).toHaveCount(0);
		await savedContaining(page, seeded, '| Signs | Start Plain words in a cell. — checked | open! |');
		await frame(page, '01-plain-cell-1440-after');

		// The document's own undo takes the cell edit out again, file included. Mod, not Control: on a Mac the document's
		// undo is Cmd+z, in a cell exactly as it is in prose.
		await cell(page, 2, 1).click();
		await page.keyboard.press(`${MOD}+z`);
		await savedContaining(page, seeded, 'Plain words in a cell. | open');
		await page.keyboard.press(`${MOD}+Shift+z`);
		await savedContaining(page, seeded, 'Plain words in a cell — checked');
		await page.keyboard.press('Escape');

		await page.setViewportSize({ width: 900, height: 1000 });
		await expect(firstTable(page)).toBeVisible();
		await cell(page, 2, 1).click();
		await editorInCell(page, cell(page, 2, 1));
		await frame(page, '01-plain-cell-900-typing');
		await page.keyboard.press('Escape');
	});

	test('typed and pasted characters cannot break the row', async ({ page }) => {
		test.setTimeout(120_000);
		const seeded = await openDocument(page, { width: 1440, height: 1000 });
		await cell(page, 2, 1).click();
		// Mod+End, not End: this cell's text wraps, and End means the end of the line you are looking at, here as in prose.
		await page.keyboard.press(`${MOD}+End`);
		await page.keyboard.type(' a|b');
		expect(await firstTable(page).locator('tr').nth(2).locator('td').count(), 'the typed pipe stays inside the cell').toBe(3);
		await page.keyboard.press('Escape');
		await savedContaining(page, seeded, 'a\\|b');
		await frame(page, '03-typed-pipe-1440-after');

		await cell(page, 3, 1).click();
		await page.locator('.cm-md-cell-editor .cm-content').click({ position: { x: 40, y: 8 } });
		await page.keyboard.press('End');
		await page.evaluate(() => navigator.clipboard.writeText('pasted line one\npasted line two').catch(() => undefined));
		await page.keyboard.press(`${MOD}+v`);
		const line = (await savedText(page, seeded)).split('\n').find((row) => row.startsWith('| Wide |'));
		expect(line, 'the pasted lines stayed on the row').toBeDefined();
		expect(line!, 'the break became a space').toContain('pasted line one pasted line two');
		expect(await firstTable(page).locator('tr').count(), 'no row was added').toBe(4);
		await frame(page, '03-pasted-lines-1440-after');
	});

	test('the comment flows still see a cell: a word selected in the cell, a commented cell, an image in a cell', async ({ page }) => {
		test.setTimeout(120_000);
		const seeded = await openDocument(page, { width: 1440, height: 1000 });

		// A word selected inside the open cell, as in prose: the selection reaches the document, so the comment offer appears.
		await cell(page, 3, 1).dblclick();
		await expect(editorIn(page)).toBeVisible();
		await page.keyboard.down('Shift');
		for (let word = 0; word < 3; word++) await page.keyboard.press('ArrowRight');
		await page.keyboard.up('Shift');
		await expect(page.locator('.sel-comment'), 'the comment offer appears for text selected in a cell').toBeVisible();
		await frame(page, '04-selection-in-cell-1440');
		await page.keyboard.press('Escape');

		// A comment already drawn on a cell's text opens on a click, and its text is still editable afterwards.
		const marked = page.locator(`[data-comment-ids*="${seeded.commentId}"]`).first();
		await expect(marked, 'the comment is drawn inside the cell').toBeVisible();
		await marked.click();
		await expect(page.getByText('Which way does it point now?').first()).toBeVisible();
		await frame(page, '04-comment-in-cell-1440');

		// An image inside a cell keeps its own click: it opens rather than editing.
		const image = page.locator('.cm-md-cell-image').first();
		await expect(image).toBeVisible();
		await image.click();
		await expect(page.locator('.cm-md-cell-editor'), 'no cell editor opened over the image').toHaveCount(0);
		await frame(page, '04-image-in-cell-1440');
	});

	test('at 1280 with the Workspace panel open, a click in a cell types into the cell and the file', async ({ page }) => {
		test.setTimeout(150_000);
		const seeded = await openDocument(page, { width: 1280, height: 900 });
		// The Workspace panel, open as the person who reported this had it: at this width the pane lies over the right of the
		// document rather than pushing it aside, so a click has to reach the cell where it is drawn, under the pane's edge.
		const pane = page.locator('aside.pane');
		if (!(await pane.isVisible())) await page.getByRole('button', { name: 'Toggle panel' }).click();
		await expect(pane, 'the Workspace panel is open over the document').toBeVisible();
		await frame(page, '05-panel-1280-before');

		for (const [name, target] of [['plain', cell(page, 2, 1)], ['commented', cell(page, 1, 1)]] as const) {
			// The pane covers the right of the document at this width, so the click goes where the cell is still reachable.
			await target.click({ position: { x: 8, y: 8 } });
			await editorInCell(page, target);
			await expect(page.locator('.cm-md-cell-editor .cm-content'), `${name}: focus is inside the cell editor`).toBeFocused();
			await page.keyboard.type('ZZ');
			expect(await target.innerText(), `${name}: the cell shows what was typed`).toContain('ZZ');
			await page.keyboard.press('Escape');
			await savedContaining(page, seeded, 'ZZ');
			await frame(page, `05-panel-1280-${name}-after`);
		}
	});
});
