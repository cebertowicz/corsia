// A pane still open on a document that was promoted and whose promoted copy is then deleted: nothing takes the
// person's reply. The server refuses it and the pane shows that sentence as given, with the draft still in the field.
import { execFileSync } from 'node:child_process';
import { appendFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN } from '../support/corsia';

const repo = path.resolve(process.cwd(), '..');
const PANE_BODY = 'Launch plan details for review.';
const REPLY = 'Reply sent from a pane whose document is gone';
const SENTENCE = 'Comments can’t be added to this document. Your comment wasn’t sent, and your text is still here.';

test('a reply refused because its document no longer takes comments shows the server sentence and keeps the draft', async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'the refusal is the same at every width');
	test.setTimeout(180_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const state = process.env.CORSIA_E2E_STATE_DIR;
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	if (!workspace || !state || !python || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Requires disposable mock environment');
	const env = { ...process.env, PYTHONPATH: 'backend:backend/tests', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state };
	const seed = (mode: string, artifact: string, thread: string) => {
		const out = execFileSync(python, ['frontend/e2e/support/person-comment-task-fixture.py', mode, artifact, thread], { cwd: repo, encoding: 'utf8', env });
		const line = out.split('\n').find((l) => l.startsWith('FIXTURE_RESULT='));
		return line ? JSON.parse(line.slice('FIXTURE_RESULT='.length)) : null;
	};
	const title = `Not commentable ${Date.now()}`;
	const threadId = await createSession(request, title);
	const root = seed('working-root', '-', threadId).root as string;
	mkdirSync(root, { recursive: true });
	const name = 'Gone plan.md';
	const source = path.join(root, name);
	const query = new URLSearchParams({ token: TEST_TOKEN });
	const saved = await request.put(`/api/files/content?${query}`, { data: { path: source, text: PANE_BODY, thread_id: threadId, create: true } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	seed('feedback', artifactId, threadId);

	const texts = async (id: string) => {
		const body = await (await request.get(`/api/artifacts/${id}/comments?${query}`)).json();
		return (body.comments as { body: string; replies?: { body: string }[] }[]).flatMap((thread) => [thread.body, ...(thread.replies ?? []).map((reply) => reply.body)]);
	};

	// The person opens the document and its comments.
	await openAppPastLanding(page);
	await sidebarThread(page, title).click();
	const steps = page.getByRole('list', { name: 'Plan steps' });
	await expect(steps).toBeVisible({ timeout: 30_000 });
	await steps.getByRole('listitem').filter({ hasText: name }).first().getByRole('button').first().click();
	await page.getByRole('button', { name: /\d+ comments?, \d+ unread/ }).first().click();
	await expect(page.getByRole('region', { name: /^Comment by / })).toHaveCount(4);

	// Under the open pane the file is edited outside CORSIA, promoted, and the promoted copy is deleted.
	appendFileSync(source, '\nEdited outside CORSIA.');
	const promoted = seed('promote', artifactId, threadId) as { shared: string };
	expect(promoted.shared).not.toBe(artifactId);
	expect(seed('file-gone', promoted.shared, threadId).state).toBe('tombstone');
	const before = await texts(promoted.shared);

	// The reply goes from the pane that is still open: it is refused with the server's sentence, nothing offers a retry,
	// and the reply stays in the field.
	const field = page.getByPlaceholder('Reply…').first();
	await field.fill(REPLY);
	await field.press('Enter');
	await expect(page.getByText(SENTENCE).first()).toBeVisible({ timeout: 15_000 });
	await expect(page.getByText(/try again/i)).toHaveCount(0);
	await expect(field).toHaveValue(REPLY);
	expect(await texts(promoted.shared)).toEqual(before);
	expect(await texts(artifactId)).not.toContain(REPLY);
	await page.screenshot({ path: testInfo.outputPath('reply-refused.png') });
});
