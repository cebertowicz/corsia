import fs from 'node:fs';
import path from 'node:path';
import { expect, type Locator, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from './corsia';

/**
 * The document, layouts and helpers the table-cell editing specs share (`flows/doc-table-cell-*.spec.ts`). Frames for the
 * review pack are written to the folder in CORSIA_CELL_FRAMES, and skipped when it is unset so CI keeps its own artifacts.
 */

export const LONG = 'A long note that keeps going so the table needs more room than a phone has, with more words after it and a few more after those.';
export const DOC = [
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

/** The widths each criterion is proven at, each in the browser mode that matches it. */
export const LAYOUTS = [
	{ width: 1440, height: 1000, project: 'chromium-desktop' },
	{ width: 900, height: 1000, project: 'chromium-desktop' },
	{ width: 393, height: 851, project: 'chromium-mobile' },
] as const;
export type Layout = (typeof LAYOUTS)[number];

/** Skips a layout in the browser mode that does not match it, so each width runs once, with or without touch. */
export const onlyIn = (layout: Layout, testInfo: TestInfo) => {
	if (testInfo.project.name !== layout.project) testInfo.skip(true, `${layout.width} runs in ${layout.project}`);
};

export const frame = async (page: Page, name: string) => {
	const frames = process.env.CORSIA_CELL_FRAMES;
	if (!frames) return;
	await page.screenshot({ path: path.join(frames, `${name}.png`), animations: 'disabled' });
};

export type Seeded = { title: string; docPath: string; params: { token: string }; commentId: string | null; threadId: string; artifactId: string };

async function seed(page: Page, text: string, readOnly: boolean) {
	const request = page.request, params = { token: TEST_TOKEN };
	const title = `cell-editing-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const threadId = await createSession(request, title);
	const roots: Array<{ kind: string; path: string }> = (await (await request.get('/api/files/tree', { params })).json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`;
	if (readOnly) {
		// A workspace bootstrap file under the workspace's context folder is one the files API reports as not writable.
		const docPath = path.join(workspace!, 'context', fileName);
		fs.mkdirSync(path.dirname(docPath), { recursive: true });
		fs.writeFileSync(docPath, text);
		const registered = await request.post('/api/files/register', { params, data: { path: docPath, thread_id: threadId } });
		expect(registered.ok(), await registered.text()).toBeTruthy();
		return { title, fileName, docPath, params, threadId, artifactId: (await registered.json()).artifact.id as string };
	}
	const docPath = path.join(workspace!, fileName);
	const seeded = await request.put('/api/files/content', { params, data: { path: docPath, text, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	return { title, fileName, docPath, params, threadId, artifactId: (await seeded.json()).artifact.id as string };
}

/** Seed the document (and, when writable, a comment on one table cell), then open it in the canvas at `layout`. */
export async function openTableDocument(page: Page, layout: { width: number; height: number }, { readOnly = false, text = DOC } = {}): Promise<Seeded> {
	await page.setViewportSize({ width: layout.width, height: layout.height });
	await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' });
	const doc = await seed(page, text, readOnly);
	let commentId: string | null = null;
	if (!readOnly) {
		const quoted = 'trailhead sign points the wrong way';
		const from = text.indexOf(quoted);
		const created = await page.request.post(`/api/artifacts/${doc.artifactId}/comments`, {
			params: doc.params,
			data: { body: 'Which way does it point now?', anchor_text: quoted, from_off: from, to_off: from + quoted.length, delivery_policy: 'immediate' }
		});
		expect(created.ok(), await created.text()).toBeTruthy();
		commentId = (await created.json()).comment.id as string;
	}
	await page.addInitScript(({ threadId, docPath, fileName, artifactId }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: { tabs: [{ instanceId: 'edit-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'edit-tab', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, doc);
	await loginByUrl(page, { waitForComposer: false });
	// At phone width the thread list is behind the sidebar, so it is opened before the document is chosen.
	if (layout.width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(doc.title, { exact: true }).first().click();
	await expect(page.locator('.cm-md-table table').first()).toBeVisible({ timeout: 20_000 });
	return { title: doc.title, docPath: doc.docPath, params: doc.params, commentId, threadId: doc.threadId, artifactId: doc.artifactId };
}

export const savedText = async (page: Page, { docPath, params }: Seeded) => {
	const stored = await page.request.get('/api/files/content', { params: { ...params, path: docPath } });
	expect(stored.ok(), await stored.text()).toBeTruthy();
	return (await stored.json()).text as string;
};

/** Wait for the editor's autosave to reach the file, so a read-back proves what the reader left on screen. */
export async function savedContaining(page: Page, seeded: Seeded, needle: string) {
	await expect.poll(() => savedText(page, seeded), { message: `the saved file holds ${needle}`, timeout: 20_000 }).toContain(needle);
}

/** The saved file's line that starts with `start`, once the file holds `needle`. */
export async function savedRow(page: Page, seeded: Seeded, needle: string, start: string) {
	await savedContaining(page, seeded, needle);
	return (await savedText(page, seeded)).split('\n').find((line) => line.startsWith(start));
}

/**
 * The keys a person on this page's platform presses. CodeMirror reads `Mod` and its caret bindings off the page's
 * `navigator.platform`, which these projects emulate (Windows, Android), not the machine the test runs on. Redo is the
 * one prose answers to there: Ctrl+Y off a Mac, Cmd+Shift+Z on one.
 */
export async function keysOf(page: Page) {
	const mac = await page.evaluate(() => /Mac|iPhone|iPad/.test(navigator.platform));
	return mac
		? { mac, mod: 'Meta', redo: 'Meta+Shift+z', docStart: 'Meta+ArrowUp', docEnd: 'Meta+ArrowDown', wordLeft: 'Alt+ArrowLeft', lineStart: ['Home', 'Meta+ArrowLeft'], lineEnd: ['End', 'Meta+ArrowRight'] }
		: { mac, mod: 'Control', redo: 'Control+y', docStart: 'Control+Home', docEnd: 'Control+End', wordLeft: 'Control+ArrowLeft', lineStart: ['Home'], lineEnd: ['End'] };
}

/** A paste the way the browser delivers one: to the focused element, carrying the clipboard's text. */
export const paste = (page: Page, text: string) => page.evaluate((text) => {
	const data = new DataTransfer();
	data.setData('text/plain', text);
	document.activeElement?.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
}, text);

export const firstTable = (page: Page) => page.locator('.cm-md-table table').first();
export const cell = (page: Page, row: number, col: number) => firstTable(page).locator('tr').nth(row).locator('td, th').nth(col);
export const editorIn = (page: Page) => page.locator('.cm-md-cell-editor .cm-editor');
export const cellContent = (page: Page) => page.locator('.cm-md-cell-editor .cm-content');

/**
 * The open cell's editor lies inside the cell it edits, with the caret inside both, and covers nothing of the row below:
 * the numbers the review frames are checked by.
 */
export async function editorInCell(page: Page, target: Locator) {
	await expect(editorIn(page)).toBeVisible();
	await expect(cellContent(page)).toBeFocused();
	// The editor is one element that moves from cell to cell, so it is already drawn and focused when Tab or Enter is
	// pressed; its boxes are read until it has reached the cell it moved to, and a layer left elsewhere still fails.
	let e = { x: 0, y: 0, width: 0, height: 0 }, b = e;
	await expect(async () => {
		const [editor, box] = await Promise.all([editorIn(page).boundingBox(), target.boundingBox()]);
		expect(editor, 'the editor is drawn').not.toBeNull();
		expect(box, 'the cell is drawn').not.toBeNull();
		[e, b] = [editor!, box!];
		expect(e.x, 'the editor starts inside the cell').toBeGreaterThanOrEqual(b.x - 1);
		expect(e.y).toBeGreaterThanOrEqual(b.y - 1);
		expect(e.x + e.width, 'and ends inside it').toBeLessThanOrEqual(b.x + b.width + 1);
		expect(e.y + e.height, 'and covers nothing of the row below').toBeLessThanOrEqual(b.y + b.height + 1);
	}).toPass({ timeout: 5_000 });
	// The rest is read in one frame, once the layer has had its frame to follow the cell: read across separate calls, a cell
	// that moved in between (a notice drawn above the editor, a redraw) would be compared with an editor not yet moved.
	const read = await page.evaluate(async ([content, td]) => {
		await new Promise((done) => requestAnimationFrame(done));
		const selection = document.getSelection();
		const rect = selection?.rangeCount ? (selection.getRangeAt(0).getClientRects()[0] ?? selection.getRangeAt(0).getBoundingClientRect()) : null;
		// The editor's text is drawn where the cell draws it: the same first character at the same place, on as many lines.
		// The place is both ways: a cell in a row made tall by another is drawn at its vertical alignment, not at the top.
		const drawn = (el: Element) => {
			// Read off the text alone: the boxes of the elements holding it (the editor's line, a comment's mark) are not lines.
			const range = document.createRange(), bottoms = new Set<number>();
			let first: DOMRect | null = null;
			const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
			for (let node = walker.nextNode(); node; node = walker.nextNode()) {
				const text = node.textContent!, at = text.search(/\S/);
				if (at < 0) continue;
				range.setStart(node, at);
				range.setEnd(node, text.trimEnd().length);
				for (const line of range.getClientRects()) if (line.width > 0) bottoms.add(Math.round(line.bottom));
				// The first character that is not padding, measured on its own.
				range.setEnd(node, at + 1);
				first ??= range.getBoundingClientRect();
			}
			return { left: first?.left ?? 0, top: first?.top ?? 0, lines: bottoms.size };
		};
		const box = td!.getBoundingClientRect();
		return {
			cell: { x: box.left, y: box.top, width: box.width, height: box.height },
			caret: rect && { x: rect.left, y: rect.top, height: rect.height },
			text: drawn(content!), under: drawn(td!),
		};
	}, [await cellContent(page).elementHandle(), await target.elementHandle()] as const);
	const { caret, text, under } = read;
	b = read.cell;
	expect(caret, 'the caret is placed').not.toBeNull();
	expect(caret!.x, 'the caret is inside the cell').toBeGreaterThanOrEqual(b.x - 1);
	expect(caret!.x).toBeLessThanOrEqual(b.x + b.width + 1);
	expect(caret!.y).toBeGreaterThanOrEqual(b.y - 1);
	expect(caret!.y + caret!.height).toBeLessThanOrEqual(b.y + b.height + 1);
	expect(Math.abs(text.left - under.left), 'the text starts where the cell draws it').toBeLessThanOrEqual(1);
	expect(Math.abs(text.top - under.top), 'at the same height').toBeLessThanOrEqual(1);
	expect(text.lines, 'and wraps onto as many lines').toBe(under.lines);
	return { editor: e, cell: b };
}
