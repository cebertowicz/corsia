import path from 'node:path';
import { expect, type Page } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { test } from '../support/test';

/**
 * Editing a table cell on a phone. A wide table scrolls sideways inside its own box rather than pushing the page, and
 * tapping a cell opens the editor in place: the table does not jump, the cell stays on screen, and what is typed reaches
 * the saved file. Emulated Chromium only — a real phone (its keyboard and scroll behaviour) and WebKit are unverified.
 */

const LONG = 'A long note that keeps going so the table needs more room than a phone has, with more words after it and a few more after those.';
const DOC = [
	'# Editing table cells on a phone', '',
	'| Area | Review note | Preview |', '| --- | --- | --- |',
	`| Wide | ${LONG} | open |`,
	'| Signs | Plain words in a cell. | open |', '',
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
		const target = cell(page, 1, 2);
		await target.tap();

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
		const stored = await page.request.get('/api/files/content', { params: { ...seeded.params, path: seeded.docPath } });
		expect((await stored.json()).text as string, 'the tap and typing reached the file').toContain('| open ok |');
		await frame(page, '06-phone-393-after');
	});
});
