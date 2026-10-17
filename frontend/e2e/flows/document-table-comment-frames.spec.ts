import path from 'node:path';
import { expect, test, type Locator, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';

/**
 * Where a comment's text lands. With the comment list open beside the editor (desktop), a navigation brings a table row and a
 * prose line inside the visible editor and saving a comment on text already in view does not scroll. At phone width, the draft
 * sheet leaves a commented table cell above where it rests.
 */

const TABLE_ROWS = Array.from({ length: 14 }, (_, index) => `| row-${String(index).padStart(2, '0')} | owner-${String(index).padStart(2, '0')} |`);
const filler = (from: number, to: number) => Array.from({ length: to - from }, (_, index) => `## Section ${from + index}\n\nA public-safe paragraph that makes this document long enough to scroll in the editor, repeated so that each section takes real room.\n`);
const DOC = [
	'# Table comment frames', '',
	...filler(0, 18),
	'| Item | Owner |', '| --- | --- |', ...TABLE_ROWS, '',
	...filler(18, 30),
	'Prose anchor sentence to comment on.', '',
	'Visible save target line.', '',
	...filler(30, 48),
].join('\n');

type Opened = { artifactId: string; ids: { table: string; prose: string } };

async function openDocument(page: Page, size: { width: number; height: number }): Promise<Opened> {
	await page.setViewportSize(size);
	await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' });
	const request = page.request, params = { token: TEST_TOKEN };
	const title = `table-frames-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const threadId = await createSession(request, title);
	const roots: Array<{ kind: string; path: string }> = (await (await request.get('/api/files/tree', { params })).json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`, docPath = path.join(workspace!, fileName);
	const seeded = await request.put('/api/files/content', { params, data: { path: docPath, text: DOC, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId: string = (await seeded.json()).artifact.id;
	const add = async (quoted: string, body: string) => {
		const from = DOC.indexOf(quoted);
		expect(from, `setup: ${quoted} is in the document`).toBeGreaterThanOrEqual(0);
		const created = await request.post(`/api/artifacts/${artifactId}/comments`, { params, data: { body, anchor_text: quoted, from_off: from, to_off: from + quoted.length } });
		expect(created.ok(), await created.text()).toBeTruthy();
		return (await created.json()).comment.id as string;
	};
	const ids = { table: await add('owner-07', 'Thread on the table row'), prose: await add('Prose anchor sentence', 'Thread on the prose line') };
	await page.addInitScript(({ threadId, docPath, fileName, artifactId, splitWidth }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: { tabs: [{ instanceId: 'frames-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'frames-tab', visible: true, activeSurface: 'artifact' } }, splitWidth,
		}));
	}, { threadId, docPath, fileName, artifactId, splitWidth: size.width > 1600 ? 1000 : null });
	await loginByUrl(page, { waitForComposer: false });
	if (size.width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.locator('.cm-content')).toBeVisible();
	await expect(page.getByRole('button', { name: /^2 open threads\b/ })).toBeEnabled();
	await settle(page);
	return { artifactId, ids };
}

/** Waits until nothing animates and the editor's geometry is steady across frames. */
async function settle(page: Page) {
	await expect.poll(() => page.locator('.cm-editor').evaluate(async (node) => {
		const scroller = node.querySelector('.cm-scroller')!;
		const before = `${scroller.scrollHeight}:${node.getBoundingClientRect().top}`;
		await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
		return { running: document.getAnimations().filter((a) => a.playState === 'running').length, steady: before === `${scroller.scrollHeight}:${node.getBoundingClientRect().top}` };
	}), { message: 'setup: the canvas is loaded and still' }).toEqual({ running: 0, steady: true });
}

/** The element that scrolls the document and the part of it on screen (the editor grows to its content inside the pane). */
async function viewportOf(page: Page) {
	const handle = (await page.locator('.cm-scroller').evaluateHandle((node) => {
		for (let element: HTMLElement | null = node as HTMLElement; element; element = element.parentElement) {
			const { overflowY } = getComputedStyle(element);
			if ((overflowY === 'auto' || overflowY === 'scroll') && element.scrollHeight > element.clientHeight) return element;
		}
		throw new Error('the document has no scrolling viewport');
	})).asElement()!;
	const band = async () => {
		const box = (await handle.boundingBox())!;
		return { top: Math.max(0, box.y), bottom: Math.min(page.viewportSize()!.height, box.y + box.height) };
	};
	return { handle, band, scrollTop: () => handle.evaluate((element) => element.scrollTop) };
}

const boxOf = async (locator: Locator) => (await locator.boundingBox({ timeout: 2_000 }).catch(() => null));

/** Polls until the box has been inside the band for several frames, and returns it: a scroll that overshoots or is still settling fails. */
async function landsInside(page: Page, target: Locator, band: () => Promise<{ top: number; bottom: number }>, message: string) {
	let held = 0;
	await expect.poll(async () => {
		const [box, region] = [await boxOf(target), await band()];
		held = box && box.y >= region.top - 0.5 && box.y + box.height <= region.bottom + 0.5 ? held + 1 : 0;
		return held >= 4;
	}, { message, intervals: [100], timeout: 15_000 }).toBe(true);
	return (await boxOf(target))!;
}

const evidence = async (page: Page, testInfo: TestInfo, name: string) => {
	await page.screenshot({ path: testInfo.outputPath(`${name}.png`), animations: 'disabled' });
};

test.describe('@flow:document-table-comment-frames @surface:documents desktop with the comment list open', () => {
	test.beforeEach(({}, testInfo) => { test.skip(testInfo.project.name !== 'chromium-desktop', 'desktop width'); });

	test('navigating to a table row and a prose comment lands inside the editor, and saving on visible text does not scroll', async ({ page }, testInfo) => {
		test.setTimeout(90_000);
		const { ids } = await openDocument(page, { width: 2400, height: 900 });
		await page.getByRole('button', { name: /^2 open threads\b/ }).click();
		const side = page.locator('[data-doc-comments-drawer]');
		await expect(side).toBeVisible();
		await expect(page.locator('[data-adaptive-drawer]'), 'the list is the side-by-side drawer, not a sheet').toHaveCount(0);
		const { band, scrollTop, handle } = await viewportOf(page);
		const drawerBox = (await side.boundingBox())!, editorBox = (await page.locator('.cm-editor').boundingBox())!;
		expect(Math.abs(drawerBox.y - editorBox.y), 'the drawer beside the editor starts at the editor top').toBeLessThanOrEqual(2);

		const row = page.locator('.cm-md-table tr', { hasText: 'owner-07' });
		await side.locator(`[data-comment-id="${ids.table}"]`).locator('[data-comment-jump]').click();
		await landsInside(page, row, band, 'the table row lands inside the editor');
		await evidence(page, testInfo, 'desktop-table-row-navigation');

		await handle.evaluate((element) => element.scrollTo(0, 0));
		const prose = page.locator('.cm-line', { hasText: 'Prose anchor sentence' });
		await side.locator(`[data-comment-id="${ids.prose}"]`).locator('[data-comment-jump]').click();
		await landsInside(page, prose, band, 'the prose line lands inside the editor');
		await evidence(page, testInfo, 'desktop-prose-navigation');

		// A save on text that is already in view leaves the reader where they are. The list closes first: closing it widens the editor and
		// rewraps the text, so that reflow, which is not a scroll, would otherwise sit between the two measurements.
		await side.getByRole('button', { name: 'Close comments' }).click();
		await expect(side).toHaveCount(0);
		await settle(page);
		const target = page.locator('.cm-line', { hasText: 'Visible save target line.' });
		await landsInside(page, target, band, 'the line to comment on is in view');
		const before = { scrollTop: await scrollTop(), y: (await boxOf(target))!.y };
		await target.dblclick({ position: { x: 12, y: 8 } });
		await page.locator('.sel-comment').click();
		await page.getByPlaceholder('Comment…').fill('Saved on visible text.');
		await page.getByRole('button', { name: 'Comment', exact: true }).last().click();
		await expect(page.getByText('Saved on visible text.')).toBeVisible();
		await expect(target.locator('.cm-comment-anchor')).toHaveCount(1);
		await settle(page);
		const after = { scrollTop: await scrollTop(), y: (await boxOf(target))!.y };
		await evidence(page, testInfo, 'desktop-save-on-visible-text');
		expect(Math.abs(after.y - before.y), `the saved line moved from y=${before.y} to y=${after.y}`).toBeLessThanOrEqual(1);
		expect(Math.abs(after.scrollTop - before.scrollTop), 'the editor did not scroll').toBeLessThanOrEqual(1);
	});
});

test.describe('@flow:document-table-comment-frames @surface:documents phone', () => {
	test.beforeEach(({}, testInfo) => { test.skip(testInfo.project.name !== 'chromium-mobile', 'phone width'); });
	test.use({ hasTouch: true, isMobile: true });

	test('the draft sheet leaves a commented table cell above where it rests', async ({ page }, testInfo) => {
		test.setTimeout(90_000);
		await openDocument(page, { width: 390, height: 844 });
		const { handle, scrollTop } = await viewportOf(page);
		const cell = page.locator('.cm-md-table td', { hasText: 'owner-10' });
		await expect(async () => {
			await handle.evaluate((element) => element.scrollBy(0, element.clientHeight / 2));
			expect(await cell.count()).toBeGreaterThan(0);
		}).toPass({ timeout: 15_000 });
		// Park the cell in the lower part of the screen, where the sheet will cover it.
		await cell.scrollIntoViewIfNeeded();
		const place = async () => {
			const box = (await boxOf(cell))!;
			await handle.evaluate((element, delta) => element.scrollBy(0, delta), box.y + box.height - (page.viewportSize()!.height - 120));
		};
		await place();
		await expect.poll(async () => { const box = await boxOf(cell); return box ? box.y + box.height > 700 : false; }).toBe(true);
		const scrolledTo = await scrollTop();
		await cell.tap();
		await page.locator('.sel-comment').tap();
		const panel = page.locator('.drawer-panel');
		await expect(panel).toBeVisible();
		await settle(page);
		// The sheet has finished its slide and any keyboard: its resting top is where it is now.
		await expect.poll(async () => { const a = await boxOf(panel); await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))); const b = await boxOf(panel); return !!a && !!b && a.y === b.y; }).toBe(true);
		const sheetTop = (await boxOf(panel))!.y;
		await expect.poll(async () => { const box = await boxOf(cell); return !!box && box.y + box.height <= sheetTop; }, { message: 'the commented cell sits above the sheet', timeout: 10_000 }).toBe(true);
		const cellBox = (await boxOf(cell))!;
		expect(cellBox.y, 'the cell is still on screen below the top of the editor').toBeGreaterThanOrEqual(0);
		expect(await scrollTop(), 'the sheet moved the document to clear the cell').not.toBe(scrolledTo);
		await evidence(page, testInfo, 'phone-draft-sheet-over-table-cell');
	});
});
