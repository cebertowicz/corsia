import path from 'node:path';
import { expect, type Page } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { savedContaining, settled } from '../support/tableCells';
import { test } from '../support/test';

/**
 * Editing a table cell on a phone. A wide table scrolls sideways inside its own box rather than pushing the page, and
 * tapping a cell opens the editor in place: the table does not jump, the cell stays on screen, and what is typed reaches
 * the saved file. Emulated Chromium only — a real phone (its keyboard and scroll behaviour) and WebKit are unverified.
 */

const LONG = 'A long note that keeps going so the table needs more room than a phone has, with more words after it and a few more after those.';
const DOC = [
	'# Editing table cells on a phone', '',
	// Seven columns: more words side by side than a phone has room for, however the long note wraps.
	'| Area | Review note | Preview | Owner | Priority | Deadline | Reviewer |', '| --- | --- | --- | --- | --- | --- | --- |',
	`| Wide | ${LONG} | open | Dana | High | Friday | Samantha |`,
	'| Signs | Plain words in a cell. | open | Lee | Low | Monday | Ari |', '',
	'Paragraph below the table.', '',
].join('\n');

const frames = process.env.CORSIA_CELL_FRAMES;
const frame = async (page: Page, name: string) => {
	if (!frames) return;
	await page.screenshot({ path: path.join(frames, `${name}.png`), animations: 'disabled' });
};

async function openDocument(page: Page, size: { width: number; height: number }) {
	await page.setViewportSize(size);
	await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' });
	const request = page.request, params = { token: TEST_TOKEN };
	const title = `cell-editing-phone-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
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
			threads: { [threadId]: { tabs: [{ instanceId: 'phone-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'phone-tab', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { threadId, docPath, fileName, artifactId });
	await loginByUrl(page, { waitForComposer: false });
	// At phone width the thread list is behind the sidebar, so it is opened before the document is chosen.
	await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.locator('.cm-md-table table').first()).toBeVisible({ timeout: 20_000 });
	// Choosing the document closes the sidebar, and the page slides back into place after the table is drawn.
	await settled(page);
	return { docPath, params };
}

const wrap = (page: Page) => page.locator('.cm-md-table .table-wrap').first();
const cell = (page: Page, row: number, col: number) => page.locator('.cm-md-table table').first().locator('tr').nth(row).locator('td, th').nth(col);

test.describe('@flow:doc-table-cell-editing-phone @surface:documents', () => {
	test.beforeEach(({}, testInfo) => { test.skip(testInfo.project.name !== 'chromium-mobile', 'phone width'); });

	test('a wide table scrolls sideways and a tap edits the cell where it stands', async ({ page }) => {
		test.setTimeout(120_000);
		const seeded = await openDocument(page, { width: 393, height: 851 });
		const room = await wrap(page).evaluate((el) => ({ scrollable: el.scrollWidth - el.clientWidth, width: el.clientWidth }));
		expect(room.scrollable, 'the wide table needs side room at 393').toBeGreaterThan(0);
		await frame(page, '06-phone-393-before');

		// Scroll the table sideways, as a reader does to reach the last column, then tap the cell that is in view.
		await wrap(page).evaluate((el) => { el.scrollLeft = el.scrollWidth; });
		const before = {
			scrollLeft: await wrap(page).evaluate((el) => el.scrollLeft),
			page: await page.evaluate(() => ({ x: window.scrollX, y: window.scrollY })),
			table: (await page.locator('.cm-md-table table').first().boundingBox())!,
		};
		const target = cell(page, 1, 6);
		// Tapped to the right of the cell's word, on its line: the caret goes after the word, as a tap there does in prose.
		const cellBox = (await target.boundingBox())!;
		await target.tap({ position: { x: cellBox.width - 6, y: cellBox.height / 2 } });

		const editor = page.locator('.cm-md-cell-editor .cm-editor');
		await expect(editor).toBeVisible();
		await expect(page.locator('.cm-md-cell-editor .cm-content'), 'the tap brings the keyboard to the cell').toBeFocused();
		expect(await wrap(page).evaluate((el) => el.scrollLeft), 'the table did not jump back').toBe(before.scrollLeft);
		expect(await page.evaluate(() => ({ x: window.scrollX, y: window.scrollY })), 'the page did not move').toEqual(before.page);
		const box = (await editor.boundingBox())!;
		expect(box.x, 'the cell editor is on screen').toBeGreaterThanOrEqual(0);
		expect(box.x + box.width, 'and fits the phone width').toBeLessThanOrEqual(393 + 1);
		await frame(page, '06-phone-393-typing');

		await page.keyboard.type(' ok');
		await expect(target).toContainText('ok');
		await page.keyboard.press('Escape');
		await expect(page.locator('.cm-md-cell-editor')).toHaveCount(0);
		await savedContaining(page, seeded, '| Samantha ok |');
		await frame(page, '06-phone-393-after');
	});

	test('an open cell scrolled partly out of view is cut off where the table and the document cut the cell off', async ({ page }) => {
		test.setTimeout(120_000);
		await openDocument(page, { width: 393, height: 851 });
		const target = cell(page, 1, 1);
		const cellBox = (await target.boundingBox())!;
		await target.tap({ position: { x: 12, y: cellBox.height / 2 } });
		await expect(page.locator('.cm-md-cell-editor .cm-content')).toBeFocused();
		// Whether the open cell's editor is what shows at a point on the screen.
		// Read once the layer has followed the cell, which it does on the next frame after the scroll.
		const editorAt = (x: number, y: number) => page.evaluate(async ([x, y]) => {
			for (let frame = 0; frame < 2; frame++) await new Promise(requestAnimationFrame);
			return !!document.elementFromPoint(x, y)?.closest('.cm-md-cell-layer');
		}, [x, y]);

		// A swipe or trackpad scroll moves the table sideways with no press, so the cell stays open while 30px of it goes past the box's left edge.
		const sideways = await target.evaluate((td) => {
			const box = td.closest<HTMLElement>('.table-wrap')!;
			box.scrollLeft += td.getBoundingClientRect().left - box.getBoundingClientRect().left + 30;
			return { edge: box.getBoundingClientRect().left, cellLeft: td.getBoundingClientRect().left, y: td.getBoundingClientRect().top + 12 };
		});
		expect(sideways.cellLeft, 'setup: the cell runs past the table box\'s left edge').toBeLessThan(sideways.edge - 20);
		expect(sideways.edge, 'setup: there is page to the left of the table box to probe').toBeGreaterThan(6);
		expect(await editorAt(sideways.edge - 3, sideways.y), 'the editor does not draw left of the table box').toBe(false);
		expect(await editorAt(sideways.edge + 3, sideways.y), 'and still draws inside it').toBe(true);

		// The document scrolls under its top edge the same way, with the cell still open.
		const down = await target.evaluate((td) => {
			const area = td.closest<HTMLElement>('.cm-scroller')!;
			area.scrollTop += td.getBoundingClientRect().top - area.getBoundingClientRect().top + 12;
			const box = td.closest<HTMLElement>('.table-wrap')!.getBoundingClientRect();
			return { edge: area.getBoundingClientRect().top, cellTop: td.getBoundingClientRect().top, x: box.left + 20 };
		});
		expect(down.cellTop, 'setup: the cell runs past the scroll area\'s top edge').toBeLessThan(down.edge - 6);
		expect(down.edge, 'setup: there is something above the scroll area to probe').toBeGreaterThan(6);
		expect(await editorAt(down.x, down.edge - 3), 'the editor does not draw above the document\'s scroll area').toBe(false);
		expect(await editorAt(down.x, down.edge + 3), 'and still draws inside it').toBe(true);
		await expect(page.locator('.cm-md-cell-editor'), 'the cell stayed open throughout').toHaveCount(1);
		await frame(page, '06-phone-393-cut-off');
	});
});
