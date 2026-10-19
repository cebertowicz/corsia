import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';

/**
 * A mouse selection inside a rendered table cell stays selected, so it can be commented on. With the editor focused, the editor
 * used to write its own caret over the selection, and painting the pending mark collapsed it; either cleared it at once. Holding
 * the selection takes focus away from the editor, so the test also checks that a later click in prose still types where clicked.
 */

const WRAPPED = 'A long wrapping note that keeps going so the cell wraps onto several lines at ordinary pane widths, with more words after it.';
const DOC = [
	'# Cell selection', '',
	'Intro paragraph above the table.', '',
	'| # | Capability | Notes |', '| --- | --- | --- |',
	`| 1 | Plain words in a cell | ${WRAPPED} |`,
	'| 2 | Inline `code_span` value | Short note. |', '',
	'Paragraph below the table.', '',
].join('\n');

async function openDocument(page: Page) {
	const request = page.request, params = { token: TEST_TOKEN };
	const title = `cell-selection-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const threadId = await createSession(request, title);
	const roots: Array<{ kind: string; path: string }> = (await (await request.get('/api/files/tree', { params })).json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`, docPath = path.join(workspace!, fileName);
	const seeded = await request.put('/api/files/content', { params, data: { path: docPath, text: DOC, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId: string = (await seeded.json()).artifact.id;
	await page.addInitScript(({ threadId, docPath, fileName, artifactId }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: { tabs: [{ instanceId: 'cell-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'cell-tab', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { threadId, docPath, fileName, artifactId });
	await loginByUrl(page, { waitForComposer: false });
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.locator('.cm-md-table table')).toBeVisible({ timeout: 15_000 });
}

/** Page points just inside the first and last characters of `text` in the cell, which may sit on different wrapped lines. */
async function textEnds(page: Page, cellText: string, text: string) {
	return page.evaluate(({ cellText, text }) => {
		const cell = [...document.querySelectorAll<HTMLElement>('.cm-md-table td')].find((td) => td.textContent?.includes(cellText))!;
		const start = cell.textContent!.indexOf(text);
		const point = (offset: number) => {
			const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
			for (let seen = 0, node = walker.nextNode() as Text | null; node; seen += node.data.length, node = walker.nextNode() as Text | null) {
				if (offset < seen + node.data.length) {
					const range = document.createRange();
					range.setStart(node, offset - seen);
					range.setEnd(node, offset - seen + 1);
					const box = range.getBoundingClientRect();
					return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
				}
			}
			throw new Error(`offset ${offset} is outside the cell`);
		};
		return { from: point(start), to: point(start + text.length - 1) };
	}, { cellText, text });
}

/** The current selection's text, and whether both of its ends sit in the same table cell. */
function selectionState(page: Page) {
	return page.evaluate(() => {
		const selection = document.getSelection();
		const cellOf = (node: Node | null | undefined) => (node instanceof Element ? node : node?.parentElement)?.closest('.cm-md-table td') ?? null;
		const anchorCell = cellOf(selection?.anchorNode);
		return { text: selection?.toString() ?? '', inOneCell: !!anchorCell && anchorCell === cellOf(selection?.focusNode) };
	});
}

test.describe('@flow:doc-table-cell-selection @surface:documents', () => {
	test.beforeEach(({}, testInfo) => { test.skip(testInfo.project.name.includes('mobile'), 'a mouse drag'); });

	test('a drag inside a table cell stays selected, posts a comment there, and prose still types where clicked', async ({ page }, testInfo) => {
		test.setTimeout(90_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		await openDocument(page);

		// The editor holds focus before the drag, as it does after any click in the document.
		await page.locator('.cm-line', { hasText: 'Intro paragraph above the table.' }).click();
		await expect(page.locator('.cm-content')).toBeFocused();

		const selected = 'wrapping note that keeps going so the cell wraps';
		const { from, to } = await textEnds(page, WRAPPED, selected);
		await page.mouse.move(from.x, from.y);
		await page.mouse.down();
		for (let step = 1; step <= 12; step++) await page.mouse.move(from.x + ((to.x - from.x) * step) / 12, from.y + ((to.y - from.y) * step) / 12);
		await page.mouse.up();
		await page.waitForTimeout(500);

		const held = await selectionState(page);
		expect(held.text.trim().length, 'the selection is still there after the drag').toBeGreaterThan(0);
		expect(held.inOneCell, 'the selection sits inside the cell').toBe(true);
		await expect(page.locator('.sel-comment')).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'cell-selection-held');

		await page.locator('.sel-comment').click();
		await page.getByPlaceholder('Comment…').fill('Anchored in a cell.');
		await page.getByRole('button', { name: 'Comment', exact: true }).last().click();
		await expect(page.getByText('Anchored in a cell.')).toBeVisible();
		await expect(page.locator('.cm-md-table td', { hasText: 'wrapping note' }).locator('.cm-comment-anchor').first()).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'cell-selection-posted');

		// Holding the selection blurred the editor; a click in prose must focus it again with the caret where the click was.
		// The intro line sits above the open comment card, so the click reaches the editor.
		const intro = page.locator('.cm-line', { hasText: 'paragraph above the table' });
		const afterWord = await intro.evaluate((line) => {
			const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
			const node = walker.nextNode() as Text;
			const range = document.createRange();
			const at = node.data.indexOf(' paragraph');
			range.setStart(node, at);
			range.setEnd(node, at + 1);
			const box = range.getBoundingClientRect();
			return { x: box.left + 0.5, y: box.top + box.height / 2 };
		});
		await page.mouse.click(afterWord.x, afterWord.y);
		await expect(page.locator('.cm-content')).toBeFocused();
		await page.keyboard.type('ZZ');
		await expect(intro).toHaveText('IntroZZ paragraph above the table.');
		await expect(page.locator('.cm-line').first()).not.toContainText('ZZ');
	});
});
