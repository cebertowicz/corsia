import path from 'node:path';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';

const TEXT = 'A sentence to select and comment on.';
test.use({ hasTouch: true });
test('@flow:doc-draft-outside-dev draft remains available after outside tap', async ({ page, request }, info) => {
	await page.setViewportSize(info.project.name === 'chromium-mobile' ? { width: 393, height: 852 } : { width: 1440, height: 900 });
	const title = `draft-outside-${Date.now()}`, threadId = await createSession(request, title), params = { token: TEST_TOKEN };
	const roots = (await (await request.get('/api/files/tree', { params })).json()).roots as Array<{ kind: string; path: string }>;
	const docPath = path.join(roots.find(root => root.kind === 'workspace')!.path, `${title}.md`);
	const response = await request.put('/api/files/content', { params, data: { path: docPath, text: `# Draft\n\n${TEXT}\n`, thread_id: threadId, create: true } });
	expect(response.ok()).toBe(true);
	const artifactId = (await response.json()).artifact.id;
	await page.addInitScript(({ threadId, docPath, title, artifactId }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [threadId]: { tabs: [{ instanceId: 'draft-tab', artifactId, path: docPath, title, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'draft-tab', visible: true, activeSurface: 'artifact' } }, splitWidth: null }));
	}, { threadId, docPath, title, artifactId });
	await loginByUrl(page, { waitForComposer: false });
	await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.locator('.cm-line', { hasText: TEXT })).toBeVisible();
	const select = async () => {
		await page.evaluate(text => {
			const content = document.querySelector('.doc .cm-content') as HTMLElement & { cmTile: { root: { view: { state: { doc: { toString(): string } }; focus(): void; dispatch(t: unknown): void } } } };
			const view = content.cmTile.root.view, at = view.state.doc.toString().indexOf(text);
			view.focus(); view.dispatch({ selection: { anchor: at, head: at + text.length }, userEvent: 'select' });
		}, TEXT);
		await page.locator('.sel-comment').click();
	};
	await select();
	const box = page.getByPlaceholder('Comment, or @ to tag an agent');
	await box.fill('Writing that must survive.');
	const point = await page.locator('.doc .cm-scroller').boundingBox();
	if (!point) throw new Error('Document scroller absent');
	await page.touchscreen.tap(point.x + 5, point.y + 12);
	await expect(box).toBeHidden();
	await select();
	await expect(box).toHaveValue('Writing that must survive.');
});
