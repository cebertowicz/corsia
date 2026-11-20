import path from 'node:path';
import { mkdirSync, writeFileSync } from 'node:fs';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { createSession, TEST_TOKEN } from '../support/corsia';
import { installWorkspace, openThread, setDebugVersionOverlay } from '../support/heldComments';
import { waitForMainSlideToSettle, waitForPanelsToSettle } from '../support/sidebar';

for (const [kind, filename] of [
	['typical', 'launch-plan.md'],
	['long', '2026-11-16-documents-header-design-review-pack.md'],
] as const) for (const changed of [false, true]) for (const width of [900, 1440]) {
	test(`desktop tab remains visible at ${width}: ${kind}, ${changed ? 'changed' : 'normal'}`, async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		await page.setViewportSize({ width, height: 900 });
		await setDebugVersionOverlay(request, false);
		const title = `Tab floor ${kind} ${changed}`;
		const threadId = await createSession(request, title);
		const tree = await (await request.get('/api/files/tree', { params: { token: TEST_TOKEN } })).json();
		const root = tree.roots.find((entry: { kind: string }) => entry.kind === 'workspace').path as string;
		// Guidance eval files use the supported snapshot route; ordinary documents open live.
		const folder = changed
			? path.join(root, '.threads', threadId, 'skills', 'tab-floor', 'evals')
			: path.join(root, '.threads', threadId, 'tab-floor');
		mkdirSync(folder, { recursive: true });
		const docPath = path.join(folder, filename);
		const text = '# Launch plan\n\nA clear plan for the next release.\n';
		writeFileSync(docPath, text);
		const registered = await request.post('/api/files/register', { params: { token: TEST_TOKEN }, data: { path: docPath, thread_id: threadId } });
		expect(registered.ok()).toBeTruthy();
		const artifactId = (await registered.json()).artifact.id as string;
		await installWorkspace(page, threadId, [{ artifactId, path: docPath, fileName: filename, text }], 0);
		await openThread(page, threadId, title);
		await expect(page.locator('.cm-content')).toContainText('clear plan');
		await waitForMainSlideToSettle(page);
		const pane = page.locator('aside.pane');
		if (await pane.evaluate((element) => element.classList.contains('collapsed'))) await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
		await expect(pane).not.toHaveClass(/\bcollapsed\b/);
		await waitForPanelsToSettle(page);
		await page.evaluate(async (id) => {
			const { recordDocActivity } = await import('/src/lib/docPresence.ts');
			for (let i = 0; i < 3; i++) recordDocActivity({ type: 'doc_activity', artifact_id: id, thread_id: `reader-${i}`, agent: `Reader ${i}`, kind: 'read', span: null, comment_id: null, revision: null, at: new Date().toISOString() });
		}, artifactId);
		await expect(page.getByRole('group', { name: '3 participants' })).toBeVisible();
		if (changed) {
			writeFileSync(docPath, `${text}\nUpdated.\n`);
			// Foregrounding causes the real revision watcher to check external changes.
			await page.evaluate(() => window.dispatchEvent(new Event('focus')));
			await expect(page.locator('[data-file-changed]:visible')).toBeVisible();
		}
		await page.screenshot({ path: testInfo.outputPath(`tab-${width}-${kind}-${changed ? 'changed' : 'normal'}.png`) });
		if (width === 900) {
			await expect(page.getByRole('button', { name: `${filename} · Open files, 1`, exact: true })).toBeInViewport({ ratio: 1 });
			return;
		}
		const tab = page.getByRole('tab', { selected: true });
		const strip = page.locator('.strip[data-document-bar-row]');
		const boxes = await strip.evaluate((element) => {
			const rect = element.getBoundingClientRect();
			return { height: rect.height, children: [...element.children].map((child) => { const box = child.getBoundingClientRect(); return { top: box.top, bottom: box.bottom, left: box.left, right: box.right }; }) };
		});
		expect(boxes.height).toBeLessThanOrEqual(57);
		expect(boxes.children[0].right).toBeLessThanOrEqual(boxes.children[1].left + 1);
		for (const box of boxes.children) expect(box.bottom - box.top).toBeLessThanOrEqual(boxes.height);
		const close = page.getByRole('button', { name: `Close ${filename}` });
		await expect(tab).toBeInViewport({ ratio: 1 });
		await expect(close).toBeInViewport({ ratio: 1 });
		const fit = await tab.locator('.tab-title').evaluate((element, name) => {
			const rect = element.getBoundingClientRect();
			const parent = element.closest('.tabs')!.getBoundingClientRect();
			const canvas = document.createElement('canvas').getContext('2d')!;
			canvas.font = getComputedStyle(element).font;
			const floor = name.length > 6 ? `${name[0]}…${name.slice(-5)}` : name;
			return { visible: Math.max(0, Math.min(rect.right, parent.right) - Math.max(rect.left, parent.left)), floor: canvas.measureText(floor).width };
		}, filename);
		expect(fit.visible).toBeGreaterThanOrEqual(fit.floor - 1);
		const name = tab.locator('.tab-title');
		const shown = name.getByTestId('middle-name-shown');
		if (await shown.count()) {
			const text = await shown.innerText();
			if (text !== filename) {
				expect(text.startsWith(filename[0])).toBe(true);
				expect(text).toContain('…');
				expect(text.endsWith(filename.slice(-5))).toBe(true);
			}
		} else {
			expect(await name.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
		}
	});
}
