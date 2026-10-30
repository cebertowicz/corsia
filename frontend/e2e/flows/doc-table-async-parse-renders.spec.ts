import { test } from '../support/test';
import { expect } from '@playwright/test';
import { captureConsoleErrors, createSession, openAppPastLanding, TEST_TOKEN, sidebarThread } from '../support/corsia';

// Regression for the doc-viewer table bug: a GFM table that sits BELOW CodeMirror's
// initial parse budget never rendered. The live-preview block widget comes from a
// StateField that (before the fix) rebuilt only on `docChanged`. CodeMirror parses
// incrementally, so a table dozens of lines down enters the syntax tree later via a
// background-parse transaction that carries no doc change — the field never recomputed,
// leaving the table stuck on raw `| … |` pipes until the next edit.
//
// The sibling XSS doc test seeds a table on line 3 (inside the initial budget), so it
// renders even with the bug. This test deliberately buries the table under a large
// filler body so only the parse-worker path can surface it — it fails on the old
// code and passes once the StateField also rebuilds when the syntax tree advances.
// A real browser is required: jsdom never runs CodeMirror's background parse worker.
const FILLER = Array.from(
	{ length: 600 },
	(_, i) => `Filler paragraph ${i} with **bold** and \`code\` words to push the table past the initial parse budget.`
).join('\n\n');

const DOC_PATH = 'e2e-table-async-parse.md';
const DOC_TEXT = ['# async-parse table', '', FILLER, '', '## Buried table', '', '| Capability | Verdict |', '|---|---|', '| BuriedRowMarker | Renders |', ''].join('\n');

test.describe('@flow:doc-table-async-parse @surface:documents a table below the parse budget still renders', () => {
	test('a GFM table buried under a large body renders without an edit', async ({ page, request }) => {
		const errors = captureConsoleErrors(page);
		const tok = encodeURIComponent(TEST_TOKEN);
		const sessionTitle = `e2e table async ${Date.now()}`;
		const sessionId = await createSession(request, sessionTitle);

		const tree = await (await request.get(`/api/files/tree?token=${tok}`)).json();
		const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
		const wsRoot = (roots.find((r) => r.kind === 'workspace') ?? roots[0]).path;
		const absPath = `${wsRoot}/${DOC_PATH}`;
		let seed = await request.put(`/api/files/content?token=${tok}`, {
			data: { path: absPath, text: DOC_TEXT, project_id: null, thread_id: sessionId, create: true }
		});
		if (!seed.ok() && seed.status() === 409) {
			seed = await request.put(`/api/files/content?token=${tok}`, {
				data: { path: absPath, text: DOC_TEXT, project_id: null, thread_id: sessionId, create: false }
			});
		}
		expect(seed.ok(), `seed failed: ${seed.status()} ${await seed.text()}`).toBeTruthy();

		await openAppPastLanding(page);
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await sidebarThread(page, sessionTitle).click({ timeout: 10_000 });
		const browseDocuments = page.getByRole('button', { name: 'Browse documents…' });
		if (!(await browseDocuments.isVisible())) {
			await page.getByRole('button', { name: 'Toggle panel' }).click();
		}
		await browseDocuments.evaluate((button) => button.click());
		await page.getByPlaceholder('Search docs…').fill('e2e-table-async-parse');
		await page.getByText(DOC_PATH).first().click({ timeout: 10_000 });
		await page.getByRole('button', { name: 'Open document' }).evaluate((button) => button.click());

		// Scroll the buried table into the viewport so CodeMirror parses + renders it,
		// then assert the rendered HTML table (not raw pipe source) appears. No edit is
		// made — the only thing that can produce the widget is the parse-worker rebuild.
		// CodeMirror virtualizes, so the buried line isn't in the DOM until we scroll its
		// container there; step the scroll so the parse worker advances over the region.
		// A document canvas clips its leaf body (ArtifactLeaf.svelte), so the editor's own scroller scrolls.
		const editor = page.locator('.cm-editor');
		await expect(editor).toBeVisible({ timeout: 10_000 });
		const scroller = page.locator('.cm-scroller');
		const scrollHeight = await scroller.evaluate((sc) => sc.scrollHeight);
		for (let y = 0; y <= scrollHeight; y += Math.max(300, Math.ceil(scrollHeight / 12))) {
			await scroller.evaluate((sc, top) => { sc.scrollTop = top; }, y);
			await page.waitForTimeout(50);
		}
		await scroller.evaluate((sc) => { sc.scrollTop = sc.scrollHeight; });

		const table = page.locator('.cm-md-table table').first();
		await expect(table).toBeVisible({ timeout: 10_000 });
		await expect(table).toContainText('BuriedRowMarker');
		await expect(table).toContainText('Renders');

		// The buried table's pipe source must be gone (replaced by the widget).
		const rawPipeLines = await page
			.locator('.cm-line')
			.filter({ hasText: 'BuriedRowMarker' })
			.filter({ hasText: '|' })
			.count();
		expect(rawPipeLines, 'the buried table still shows raw pipe source').toBe(0);

		const appErrors = errors.filter((e) => !/Failed to load resource/i.test(e));
		expect(appErrors, appErrors.join('\n')).toEqual([]);
	});
});
