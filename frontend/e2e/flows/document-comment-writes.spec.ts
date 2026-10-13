import path from 'node:path';
import { expect, test } from '@playwright/test';
import { replyField, showAllThreads } from '../support/commentDrawer';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';

test('@flow:document-comment-writes replies, agrees, resolves, reopens and saves moved anchors', async ({ page, request }, testInfo) => {
	test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'Uses the existing desktop Comments pane.');
	const title = `comment-writes-${Date.now()}`;
	const threadId = await createSession(request, title);
	const token = encodeURIComponent(TEST_TOKEN);
	const tree = await request.get(`/api/files/tree?token=${token}`);
	expect(tree.ok()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await tree.json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const docPath = path.join(workspace!, `${title}.md`);
	const seeded = await request.put(`/api/files/content?token=${token}`, {
		data: { path: docPath, text: 'Before Anchor sentence.\n', thread_id: threadId, create: true },
	});
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId = (await seeded.json()).artifact.id;
	const commentUrl = `/api/artifacts/${artifactId}/comments?token=${token}`;
	const created = await request.post(commentUrl, {
		data: { body: 'Review this sentence.', anchor_text: 'Anchor', from_off: 7, to_off: 13 },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	const commentId = (await created.json()).comment.id;
	await page.addInitScript(({ threadId, docPath, title }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: {
				tabs: [{ instanceId: 'comment-tab', artifactId: null, path: docPath, title,
					loadedRevision: null, latestRevision: null, status: 'loading' }],
				activeTabId: 'comment-tab', visible: true, activeSurface: 'artifact',
			} }, splitWidth: 420,
		}));
	}, { threadId, docPath, title });
	// Comments live in the canvas drawer, which starts closed on every load.
	const openComments = async () => {
		await page.getByRole('button', { name: /^1 open thread\b/ }).click();
		await expect(page.getByRole('region', { name: 'Document comments', exact: true })).toBeVisible();
		await showAllThreads(page.getByRole('region', { name: 'Document comments', exact: true }));
	};
	await loginByUrl(page);
	await page.getByText(title, { exact: true }).first().click();
	await openComments();
	const comment = page.getByRole('region', { name: /^Comment by / });
	await expect(comment).toContainText('Review this sentence.');
	await (await replyField(comment)).fill('Reply persisted.');
	await comment.getByRole('button', { name: 'Send reply' }).click();
	await expect(comment).toContainText('Reply persisted.');
	await comment.getByRole('button', { name: 'Agree — end the exchange' }).click();
	await expect(comment).toContainText('Agreed');
	await comment.getByRole('button', { name: 'Resolve', exact: true }).click();
	const resolved = page.getByRole('region', { name: /^Resolved comment by / });
	await expect(resolved).toBeVisible();
	await resolved.getByRole('button', { name: 'Resolved · 2 messages' }).click();
	await resolved.getByRole('button', { name: 'Re-open', exact: true }).click();
	await expect(comment).toBeVisible();
	// The adaptive comments drawer is modal, so close it before returning to the document to move the anchor.
	await page.getByRole('region', { name: 'Document comments', exact: true }).getByRole('button', { name: 'Close comments', exact: true }).click();

	const editor = page.locator('.cm-content');
	await editor.click();
	await page.keyboard.press('ControlOrMeta+A');
	await page.keyboard.press('ArrowLeft');
	await page.keyboard.type('Prefix ');
	await expect(editor).toContainText('Prefix Before Anchor sentence.');
	await page.keyboard.press('ControlOrMeta+S');
	await expect(page.getByText('✓ Saved')).toBeVisible();
	await expect.poll(async () => {
		const response = await request.get(commentUrl);
		expect(response.ok()).toBeTruthy();
		return (await response.json()).comments.find((row: { id: string }) => row.id === commentId);
	}).toMatchObject({ resolved: 0, acked: 1, from_off: 14, to_off: 20,
		replies: [expect.objectContaining({ body: 'Reply persisted.' })] });
	await page.reload();
	await page.getByText(title, { exact: true }).first().click();
	await openComments();
	await expect(comment).toContainText('Agreed');
	await expect(editor).toContainText('Prefix Before Anchor sentence.');
	await saveEvidenceScreenshot(page, testInfo, 'document-comment-persisted', { fullPage: false });
});
