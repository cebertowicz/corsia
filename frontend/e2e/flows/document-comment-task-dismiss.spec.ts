import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { createSession, TEST_TOKEN } from '../support/corsia';
import { installWorkspace, openThread } from '../support/heldComments';
import { openDetails } from '../support/documentBar';

for (const width of [393, 320, 1440]) test(`document comment task is dismissed from Details at ${width}px`, async ({ page, request }, testInfo) => {
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const state = process.env.CORSIA_E2E_STATE_DIR;
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	if (!workspace || !state || !python || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Requires disposable mock environment');
	await page.setViewportSize({ width, height: 900 });
	const title = `document-comment-task-${width}`;
	const threadId = await createSession(request, title);
	const docPath = path.join(workspace, `comment-task-${width}.md`);
	const text = 'Launch plan details for review.';
	const saved = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: docPath, text, thread_id: threadId, create: true } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	execFileSync(python, ['frontend/e2e/support/person-comment-task-fixture.py', 'feedback', artifactId, threadId], {
		cwd: path.resolve(process.cwd(), '..'), encoding: 'utf8',
		env: { ...process.env, PYTHONPATH: 'backend:backend/tests', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state },
	});
	await installWorkspace(page, threadId, [{ artifactId, path: docPath, fileName: path.basename(docPath), text }], 0);
	await openThread(page, threadId, title);
	await expect(page.locator('.cm-content')).toContainText(text);
	const review = await request.get(`/api/attention/${artifactId}/review`, { headers: { Authorization: `Bearer ${TEST_TOKEN}` } });
	expect((await review.json()).person_step?.comment_attention).toBeTruthy();
	const detailsButton = page.locator('[data-document-details]:visible');
	await expect(detailsButton.locator('.bar-needs')).toHaveText('1');
	await page.screenshot({ path: testInfo.outputPath(`task-${width}-rest.png`) });
	await expect(page.getByRole('button', { name: 'Dismiss all', exact: true }), 'no floating Dismiss all over the document').toHaveCount(0);
	const details = await openDetails(page);
	await page.screenshot({ path: testInfo.outputPath(`task-${width}-details.png`) });
	await expect(details.getByText(/comments, .*suggestions to review/)).toBeVisible();
	await expect(details.locator('.task-who'), 'the asker is named over the summary, as on its card').toHaveText(new RegExp(`^${title.slice(0, 8)}`));
	await details.getByRole('button', { name: 'Dismiss all', exact: true }).click();
	await expect.poll(async () => {
		const response = await request.get(`/api/attention/${artifactId}/review`, { headers: { Authorization: `Bearer ${TEST_TOKEN}` } });
		return (await response.json()).person_step?.comment_attention ?? null;
	}).toBeNull();
	await expect(page.locator('[data-ask-card]')).toHaveCount(0);
	await expect(detailsButton.locator('.bar-needs')).toHaveCount(0);
});
