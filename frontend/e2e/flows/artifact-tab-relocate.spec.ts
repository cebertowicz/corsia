import path from 'node:path';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { openThreadView, postLinks, watchSocket } from '../support/documentOpen';
import { TEST_TOKEN } from '../support/corsia';

test('@flow:artifact-tab-relocate keeps an open document tab at its relocated path', async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'The tab strip is not present on mobile.');
	test.setTimeout(90_000);
	await page.setViewportSize({ width: 1440, height: 1000 });
	const log = watchSocket(page);
	const { threadId, a } = await openThreadView(page, request, log, 'relocate');
	const commentText = 'This comment survives relocation.';
	const anchorText = 'Only in';
	const from = `# ${a.name}\n\n`.length + a.body.indexOf(anchorText);
	const comment = await request.post(`/api/artifacts/${a.artifactId}/comments`, {
		params: { token: TEST_TOKEN },
		data: { body: commentText, anchor_text: anchorText, from_off: from, to_off: from + anchorText.length },
	});
	expect(comment.ok(), await comment.text()).toBeTruthy();
	await postLinks(page, request, `[Open document](<${a.docPath}>)`);
	await page.getByRole('link', { name: 'Open document', exact: true }).click();
	await expect(page.locator('.cm-content')).toContainText(a.body);
	await expect(page.locator('.cm-comment-marker')).toHaveCount(1);

	const relocatedName = `relocated-${a.name}`;
	const relocatedPath = path.join(path.dirname(a.docPath), relocatedName);
	const moved = await request.post(`/api/artifacts/${a.artifactId}/relocate?token=${encodeURIComponent(TEST_TOKEN)}`, {
		data: { thread_id: threadId, target_path: relocatedPath },
	});
	expect(moved.ok(), await moved.text()).toBeTruthy();

	await expect(page.locator('.tab-title').filter({ hasText: relocatedName })).toHaveText(relocatedName);
	await expect(page.locator('.cm-content')).toContainText(a.body);
	await expect(page.locator('.cm-comment-marker')).toHaveCount(1);
	await page.locator('.cm-comment-marker').click();
	await expect(page.getByText(commentText, { exact: true }).first()).toBeVisible();
	await page.screenshot({ path: testInfo.outputPath('relocated-tab.png'), fullPage: true });
});
