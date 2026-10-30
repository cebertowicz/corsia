import path from 'node:path';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { expectQuietSave, observeSaveCue } from '../support/docSaveCue';
import { replyField, showAllThreads } from '../support/commentDrawer';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';

async function seedCommentedDocument({ page, request }: { page: Page; request: APIRequestContext }) {
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
		data: { body: 'Review this sentence.', anchor_text: 'Anchor', from_off: 7, to_off: 13, delivery_policy: 'immediate' },
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
	return { title, commentUrl, commentId, artifactId, threadId };
}

test('@flow:document-comment-writes replies, agrees, resolves, reopens and saves moved anchors', async ({ page, request }, testInfo) => {
	test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'Uses the existing desktop Comments pane.');
	const { title, commentUrl, commentId } = await seedCommentedDocument({ page, request });
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
	const field = await replyField(comment);
	const oneLine = (await field.boundingBox())!.height;
	const reply = 'Reply persisted.\nOn several\nlines.\nAs a person types it.';
	await field.fill(reply);
	await expect.poll(async () => (await field.boundingBox())!.height).toBeGreaterThan(oneLine);
	await comment.getByRole('button', { name: 'Send reply' }).click();
	await expect(comment).toContainText('Reply persisted.');
	// The sent reply leaves the field empty and one line tall again, not held at the height of the text it held.
	await expect.poll(async () => (await (await replyField(comment)).boundingBox())!.height).toBe(oneLine);
	await comment.getByRole('button', { name: 'Agree', exact: true }).click();
	await expect(comment).toContainText('Agreed');
	await expect(await replyField(comment)).toBeVisible();
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
	await observeSaveCue(page);
	await page.keyboard.type('Prefix ');
	await expect(editor).toContainText('Prefix Before Anchor sentence.');
	await page.keyboard.press('ControlOrMeta+S');
	// Persistence below, rather than a badge, proves the fast save has landed.
	await expect.poll(async () => {
		const response = await request.get(commentUrl);
		expect(response.ok()).toBeTruthy();
		return (await response.json()).comments.find((row: { id: string }) => row.id === commentId);
	}).toMatchObject({ resolved: 0, acked: 1, from_off: 14, to_off: 20,
		replies: [expect.objectContaining({ body: reply })] });
	await expectQuietSave(page);
	await page.reload();
	await page.getByText(title, { exact: true }).first().click();
	await openComments();
	await expect(comment).toContainText('Agreed');
	await expect(editor).toContainText('Prefix Before Anchor sentence.');
	await saveEvidenceScreenshot(page, testInfo, 'document-comment-persisted', { fullPage: false });
});

test('@flow:resolved-reply a person can reply to an agent on a resolved thread', async ({ page, request }, testInfo) => {
	const mobile = testInfo.project.name.includes('mobile');
	await page.setViewportSize({ width: mobile ? 393 : 1440, height: mobile ? 852 : 900 });
	await page.emulateMedia({ colorScheme: 'light' });
	const { title, commentUrl, commentId, threadId } = await seedCommentedDocument({ page, request });
	const params = { token: TEST_TOKEN };
	let result = await request.post('/api/delegation/docs/comments', { params, data: { as_session_id: threadId, parent_id: commentId, body: 'Agent reply before resolution' } });
	expect(result.ok(), await result.text()).toBeTruthy();
	result = await request.patch(`/api/delegation/docs/comments/${commentId}`, { params, data: { as_session_id: threadId, resolved: true } });
	expect(result.ok(), await result.text()).toBeTruthy();
	await loginByUrl(page);
	if (mobile) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await page.getByRole('button', { name: /^0 open threads\b/ }).click();
	const drawer = page.getByRole('region', { name: 'Document comments', exact: true });
	await drawer.getByRole('button', { name: /Filter comments:/ }).click();
	await drawer.getByRole('menuitemradio', { name: 'Resolved', exact: true }).click();
	const resolved = drawer.getByRole('region', { name: /^Resolved comment by / });
	await resolved.getByRole('button', { name: 'Resolved · 2 messages' }).click();
	await expect(resolved).toContainText('Agent reply before resolution');
	const input = resolved.getByRole('textbox', { name: 'Reply' });
	await expect(input).toBeVisible();
	await saveEvidenceScreenshot(page, testInfo, 'resolved-open', { fullPage: false });
	await input.click(); await page.keyboard.type('Hello');
	await expect(input).toBeFocused();
	await input.evaluate((el: HTMLTextAreaElement) => el.setSelectionRange(2, 2));
	await page.keyboard.type('X');
	await expect(input).toHaveValue('HeXllo'); await expect(input).toBeFocused();
	await saveEvidenceScreenshot(page, testInfo, 'resolved-typing', { fullPage: false });
	await resolved.getByRole('button', { name: 'Send reply' }).click();
	await expect(resolved).toContainText('HeXllo');
	await expect(resolved.getByRole('button', { name: 'Re-open', exact: true })).toBeVisible();
	await expect.poll(async () => {
		const response = await request.get(commentUrl);
		const rows = (await response.json()).comments;
		return rows.find((row: { id: string }) => row.id === commentId);
	}).toMatchObject({ resolved: 1, replies: [expect.objectContaining({ body: 'Agent reply before resolution' }), expect.objectContaining({ body: 'HeXllo', author_kind: 'user' })] });
	await saveEvidenceScreenshot(page, testInfo, 'resolved-sent', { fullPage: false });
});

test('@flow:document-comment-highlight a resolved comment loses its highlight, shows it again only while picked in the drawer, and re-opening restores it', async ({ page, request }, testInfo) => {
	test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'Uses the existing desktop Comments pane.');
	const { title } = await seedCommentedDocument({ page, request });
	await loginByUrl(page);
	await page.getByText(title, { exact: true }).first().click();
	const anchorMark = page.locator('.cm-content .cm-comment-anchor');
	// The highlight opens the comment's popover, which focuses it: resolving it from there must end the highlight too.
	await anchorMark.click();
	await expect(anchorMark).toHaveClass(/cm-comment-focused/);
	await saveEvidenceScreenshot(page, testInfo, 'highlight-1-open', { fullPage: false });
	await page.getByRole('region', { name: /^Comment by / }).getByRole('button', { name: 'Resolve', exact: true }).click();
	await expect(anchorMark).toHaveCount(0);
	await saveEvidenceScreenshot(page, testInfo, 'highlight-2-resolved', { fullPage: false });
	// Picking the resolved comment in the drawer shows which text it was.
	await page.getByRole('button', { name: /^0 open threads\b/ }).click();
	const drawer = page.getByRole('region', { name: 'Document comments', exact: true });
	await showAllThreads(drawer);
	await drawer.getByRole('button', { name: /^Go to text: / }).click();
	await expect(anchorMark).toHaveCount(1);
	await saveEvidenceScreenshot(page, testInfo, 'highlight-3-resolved-picked-in-drawer', { fullPage: false });
	await drawer.getByRole('region', { name: /^Resolved comment by / }).getByRole('button', { name: /^Resolved · / }).click();
	await drawer.getByRole('button', { name: 'Re-open', exact: true }).click();
	await expect(anchorMark).toHaveCount(1);
	await saveEvidenceScreenshot(page, testInfo, 'highlight-4-reopened', { fullPage: false });
});
