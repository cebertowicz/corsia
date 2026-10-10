import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN, waitForWsConnected } from '../support/corsia';

test('@flow:document-second-canvas a reply and a resolve made in one canvas appear in a second open canvas', async ({ page, request }, testInfo) => {
	test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'Uses the existing desktop Comments pane.');
	const title = `second-canvas-${Date.now()}`;
	const threadId = await createSession(request, title);
	const params = { token: TEST_TOKEN };
	const tree = await request.get('/api/files/tree', { params });
	expect(tree.ok()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await tree.json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const docPath = path.join(workspace!, `${title}.md`);
	const seeded = await request.put('/api/files/content', {
		params, data: { path: docPath, text: 'Before Anchor sentence.\n', thread_id: threadId, create: true },
	});
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId = (await seeded.json()).artifact.id;
	const created = await request.post(`/api/artifacts/${artifactId}/comments`, {
		params, data: { body: 'Review this sentence.', anchor_text: 'Anchor', from_off: 7, to_off: 13 },
	});
	expect(created.ok(), await created.text()).toBeTruthy();

	// Both canvases start from the same stored layout: the document open in this thread.
	await page.context().addInitScript(({ threadId, docPath, title }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: {
				tabs: [{ instanceId: 'comment-tab', artifactId: null, path: docPath, title,
					loadedRevision: null, latestRevision: null, status: 'loading' }],
				activeTabId: 'comment-tab', visible: true, activeSurface: 'artifact',
			} }, splitWidth: 420,
		}));
	}, { threadId, docPath, title });

	const open = async (canvas: typeof page) => {
		await loginByUrl(canvas);
		await canvas.getByText(title, { exact: true }).first().click();
		await waitForWsConnected(canvas);
		await canvas.getByRole('button', { name: '1 comment', exact: true }).click();
		await expect(canvas.getByRole('region', { name: 'Document comments', exact: true })).toBeVisible();
		const comment = canvas.getByRole('region', { name: /^Comment by / });
		await expect(comment).toContainText('Review this sentence.');
		return comment;
	};
	const watched = await open(page);
	const writer = await page.context().newPage();
	const written = await open(writer);

	// No reload and no injected frame: the first canvas only hears what the server sends.
	await written.getByPlaceholder('Reply…').fill('Reply from the other canvas.');
	await written.getByRole('button', { name: 'Send reply' }).click();
	await expect(written).toContainText('Reply from the other canvas.');
	// The reply arrives collapsed in the canvas that did not write it.
	await watched.getByRole('button', { name: '⊕ 1 reply' }).click();
	await expect(watched).toContainText('Reply from the other canvas.');

	await written.getByRole('button', { name: 'Resolve', exact: true }).click();
	await expect(writer.getByRole('region', { name: /^Resolved comment by / })).toBeVisible();
	await expect(page.getByRole('region', { name: /^Resolved comment by / })).toBeVisible();
	await saveEvidenceScreenshot(page, testInfo, 'document-second-canvas-current', { fullPage: false });
	await writer.close();
});
