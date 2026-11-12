import path from 'node:path';
import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';

/**
 * Reclicking a commented cell to close its card must not send the next keystroke somewhere the reader never clicked.
 * The highlight sits in the rendered table widget, which takes the click instead of the editor; the press puts the
 * editor's caret on the clicked cell, so the card closing hands the keyboard that place and not the top of the document.
 * The proof is the saved document, read back from the server, and the marker typed in prose afterwards says the save
 * carried what the editor holds.
 */

const QUOTED = 'north sign';
const CELL_ROW = '| Trailhead signs |';
const INTRO = 'Intro line above the table.';
const PARA = 'Paragraph below the table.';
const DOC = [
	'# Editing table cells', '',
	INTRO, '',
	'| Item | Notes |', '| --- | --- |',
	`| Trailhead signs | Replace the faded ${QUOTED} |`, '',
	PARA, '',
].join('\n');
const FENCE = ['```mermaid', 'flowchart TD', '  Draft --> Review', '```'].join('\n');
const CLOSING = 'Closing line under the diagram.';
/** The table, then a commented diagram with a blank line and a paragraph under it. */
const WITH_DIAGRAM = DOC.replace(`${PARA}\n`, `${FENCE}\n\n${CLOSING}\n`);
/** The same document with enough below it to scroll. */
const LONG = DOC + Array.from({ length: 80 }, (_, i) => `Filler paragraph ${i + 1}.\n`).join('\n');

/** The document's own editor, never an editor nested inside one of its widgets. */
const HOST = '.md-editor > .cm-editor';

/** The editor at rest: nothing animating and its box unchanged across two frames. The reader's keystroke comes after the page has settled, and CodeMirror writes its own caret into the document only in that time. */
async function settled(page: Page) {
	await expect.poll(() => page.locator(HOST).evaluate(async (node) => {
		const scroller = node.querySelector(':scope > .cm-scroller')!;
		const before = `${scroller.scrollHeight}:${node.getBoundingClientRect().top}`;
		await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
		const running = document.getAnimations().filter((a) => a.playState === 'running' && a.effect?.getComputedTiming().iterations !== Infinity).length;
		return running === 0 && before === `${scroller.scrollHeight}:${node.getBoundingClientRect().top}`;
	}), { message: 'setup: the editor is at rest before the keystroke' }).toBe(true);
}

/** Seed a document with one comment on `anchor` (by default a phrase in a table cell), and open it in the artifact pane. */
async function openDocument(page: Page, size: { width: number; height: number }, text = DOC, anchor = QUOTED) {
	await page.setViewportSize(size);
	const request = page.request, params = { token: TEST_TOKEN };
	const title = `reclick-typing-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const threadId = await createSession(request, title);
	const roots: Array<{ kind: string; path: string }> = (await (await request.get('/api/files/tree', { params })).json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`, docPath = path.join(workspace!, fileName);
	const seeded = await request.put('/api/files/content', { params, data: { path: docPath, text, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId: string = (await seeded.json()).artifact.id;
	const from = text.indexOf(anchor);
	expect(from, 'setup: the commented text is in the document').toBeGreaterThanOrEqual(0);
	const created = await request.post(`/api/artifacts/${artifactId}/comments`, {
		params, data: { body: 'Which sign needs replacing?', anchor_text: anchor, from_off: from, to_off: from + anchor.length, delivery_policy: 'immediate' },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	const commentId: string = (await created.json()).comment.id;
	await page.addInitScript(({ threadId, docPath, fileName, artifactId }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: { tabs: [{ instanceId: 'reclick-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'reclick-tab', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { threadId, docPath, fileName, artifactId });
	await loginByUrl(page, { waitForComposer: false });
	if (size.width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.locator('.cm-md-table table')).toBeVisible({ timeout: 15_000 });
	const highlight = page.locator('.cm-md-table td .cm-comment-anchor', { hasText: QUOTED }).first();
	if (anchor === QUOTED) await expect(highlight).toBeVisible();
	return { request, docPath, commentId, highlight };
}

/** Type a marker at the end of the line above the table, save, and return the text the server holds. That line stays clear of an open card at either width. */
async function savedAfterProseMarker(page: Page, request: Page['request'], docPath: string) {
	const prose = page.locator('.cm-line', { hasText: INTRO });
	await prose.scrollIntoViewIfNeeded();
	await prose.click({ position: { x: 8, y: 6 } });
	await page.keyboard.press('End');
	await page.keyboard.type(' MK');
	await page.keyboard.press('ControlOrMeta+S');
	await expect.poll(async () => {
		const stored = await request.get('/api/files/content', { params: { token: TEST_TOKEN, path: docPath } });
		expect(stored.ok(), await stored.text()).toBeTruthy();
		return (await stored.json()).text as string;
	}, { message: 'setup: a save reaches the server' }).toContain(`${INTRO} MK`);
	return (await (await request.get('/api/files/content', { params: { token: TEST_TOKEN, path: docPath } })).json()).text as string;
}

/** The source of the table and the line under it, as seeded. */
const TABLE_AND_AFTER = ['| Item | Notes |', '| --- | --- |', `${CELL_ROW} Replace the faded ${QUOTED} |`, '', PARA].join('\n');

/** The typing lands where the press put the caret: the start of the clicked cell's source, and nowhere else. */
function assertTypedInClickedCell(saved: string) {
	expect(saved.startsWith('# Editing table cells'), `the text typed after the reclick landed before the heading: ${JSON.stringify(saved.slice(0, 60))}`).toBe(true);
	expect(saved, 'the text typed after the reclick reached the document').toContain('XYZ');
	expect(saved.split('XYZ'), `XYZ was typed once: ${JSON.stringify(saved)}`).toHaveLength(2);
	expect(saved, `XYZ is in the clicked cell's source: ${JSON.stringify(saved)}`).toContain(`${CELL_ROW} XYZReplace the faded ${QUOTED} |`);
}

test.describe('@flow:doc-table-cell-reclick-typing @surface:documents desktop', () => {
	test.beforeEach(({}, testInfo) => { test.skip(testInfo.project.name.includes('mobile'), 'a mouse click'); });

	// Typing in a cell at desktop widths needs the table cell editor (#2462): the browser cannot place its caret inside the
	// rendered table, and that is where desktop typing goes. Until then this asserts only that the keystrokes cannot damage
	// the document: nothing before the heading, no new table row, the cell's text as it was, nothing under the table.
	test('reclicking the highlight and typing leaves the table and the top of the document intact', async ({ page, request }, testInfo) => {
		test.setTimeout(120_000);
		const { docPath, commentId, highlight } = await openDocument(page, { width: 1440, height: 900 });

		await highlight.click();
		await expect(highlight).toHaveAttribute('data-comment-ids', commentId);
		await expect(page.locator('.cm-md-table .cm-comment-anchor.cm-comment-focused')).toBeVisible();

		await highlight.click();
		await settled(page);
		await saveEvidenceScreenshot(page, testInfo, 'desktop-after-reclick');

		await page.keyboard.type('XYZ');
		const saved = await savedAfterProseMarker(page, request, docPath);
		await saveEvidenceScreenshot(page, testInfo, 'desktop-after-typing');
		expect(saved.startsWith('# Editing table cells'), `the text typed after the reclick landed before the heading: ${JSON.stringify(saved.slice(0, 60))}`).toBe(true);
		expect(saved, `the table and the line under it are unchanged: ${JSON.stringify(saved)}`).toContain(TABLE_AND_AFTER);
		expect(saved.split('\n').filter((line) => line.startsWith('|')), 'the table kept its rows').toHaveLength(3);
	});

	test('a press on a cell leaves the document scrolled where it was', async ({ page }) => {
		test.setTimeout(120_000);
		await openDocument(page, { width: 1440, height: 900 }, LONG);
		const scroller = page.locator(`${HOST} > .cm-scroller`);
		await scroller.evaluate((el) => { el.scrollTop = 60; });
		await settled(page);
		const before = await scroller.evaluate((el) => el.scrollTop);
		expect(before, 'setup: the document is scrolled').toBeGreaterThan(0);
		await page.locator('.cm-md-table td', { hasText: 'Trailhead signs' }).click();
		await settled(page);
		expect(await scroller.evaluate((el) => el.scrollTop)).toBe(before);
	});
});

test.describe('@flow:doc-table-cell-reclick-typing @surface:documents phone', () => {
	test.beforeEach(({}, testInfo) => { test.skip(testInfo.project.name !== 'chromium-mobile', 'a touch tap'); });
	test.use({ hasTouch: true, isMobile: true });

	test('retapping the highlight and typing puts the text in the tapped cell', async ({ page, request }, testInfo) => {
		test.setTimeout(120_000);
		const { docPath, commentId, highlight } = await openDocument(page, { width: 393, height: 852 });

		await highlight.tap();
		await expect(highlight).toHaveAttribute('data-comment-ids', commentId);
		await expect(page.locator('.drawer-panel')).toBeVisible();

		// The sheet covers the table: the tap lands on whatever is on top of the highlight, as a reader's does.
		await highlight.tap({ force: true });
		await settled(page);
		await saveEvidenceScreenshot(page, testInfo, 'phone-after-reclick');

		await page.keyboard.type('XYZ');
		const saved = await savedAfterProseMarker(page, request, docPath);
		await saveEvidenceScreenshot(page, testInfo, 'phone-after-typing');
		assertTypedInClickedCell(saved);
	});
	test('typing after a diagram comment closes or after a tap on table padding leaves both blocks whole', async ({ page, request }, testInfo) => {
		test.setTimeout(120_000);
		const { docPath } = await openDocument(page, { width: 393, height: 852 }, WITH_DIAGRAM, FENCE);
		const diagram = page.locator('.cm-md-mermaid');
		await expect(diagram.locator('svg'), 'setup: the diagram rendered').toBeVisible({ timeout: 20_000 });
		await expect(diagram, 'setup: the diagram carries its comment').toHaveClass(/cm-comment-block/);

		// Open the diagram's comment and retap to close it: closing hands the keyboard back to the editor's caret.
		await diagram.tap();
		await expect(page.locator('.drawer-panel')).toBeVisible();
		await diagram.tap({ force: true });
		await settled(page);
		await page.keyboard.type('XYZ');

		// A tap in the table's own padding, above the Notes header: off every cell, nearest that one.
		const notes = (await page.locator('.cm-md-table th', { hasText: 'Notes' }).boundingBox())!;
		const widget = (await page.locator('.cm-md-table').boundingBox())!;
		expect(notes.y - widget.y, 'setup: the table has padding above its header').toBeGreaterThan(4);
		await page.touchscreen.tap(notes.x + notes.width / 2, widget.y + 2);
		await settled(page);
		// The tap leaves the editor unfocused. The next focus() that is not a tap (a card closing hands the keyboard back
		// this way) gives the keyboard the caret the tap placed. No card is open here, so call it as the card does.
		expect(await page.locator(`${HOST} > .cm-scroller > .cm-content`).evaluate((el) => {
			const view = (el as HTMLElement & { cmTile?: { root?: { view?: { focus(): void } } } }).cmTile?.root?.view;
			view?.focus();
			return Boolean(view);
		}), 'setup: the editor view is reachable from its content element').toBe(true);
		await settled(page);
		await page.keyboard.type('QRS');

		const saved = await savedAfterProseMarker(page, request, docPath);
		await saveEvidenceScreenshot(page, testInfo, 'phone-diagram-and-padding-after-typing');
		expect(saved.startsWith('# Editing table cells'), `typing landed before the heading: ${JSON.stringify(saved.slice(0, 60))}`).toBe(true);
		expect(saved, 'closing the diagram comment put the typing on the blank line under the fence').toContain(`${FENCE}\nXYZ\n${CLOSING}`);
		expect(saved, 'the padding tap put the typing in the nearest header cell').toContain('| Item | QRSNotes |');
		expect(saved.split('\n').filter((line) => line.startsWith('|')), 'the table kept its rows').toHaveLength(3);
		await expect(diagram.locator('svg'), 'the diagram still renders').toBeVisible();
		await expect(page.locator('.cm-md-table table tr'), 'the table still renders its two rows').toHaveCount(2);
	});
});
