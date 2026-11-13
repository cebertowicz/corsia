// A promoted document's comment task: when promote moves the document's identity, the task follows it and opens the
// document with its comments; when the promoted identity cannot be the source's own, the comments and the task move
// onto the new identity instead of being left on the retired one.
import { execFileSync } from 'node:child_process';
import { appendFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN } from '../support/corsia';

const repo = path.resolve(process.cwd(), '..');
const MOVED = 'This document is in another place, and its comments are there with it.';

for (const editedOnDisk of [false, true]) test(`comment task after promote, edited on disk first: ${editedOnDisk}`, async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'promote is the same at every width; the main spec covers the row at each');
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
	const title = `Promote ${editedOnDisk ? 'fallback' : 'move'} ${Date.now()}`;
	const threadId = await createSession(request, title);
	const root = seed('working-root', '-', threadId).root as string;
	mkdirSync(root, { recursive: true });
	const name = `Launch plan ${editedOnDisk ? 'b' : 'a'}.md`;
	const source = path.join(root, name);
	const query = new URLSearchParams({ token: TEST_TOKEN });
	const saved = await request.put(`/api/files/content?${query}`, { data: { path: source, text: `Launch plan details for review ${editedOnDisk ? 'b' : 'a'}.`, thread_id: threadId, create: true } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	seed('feedback', artifactId, threadId);
	// The task's id before promote: live plan reads leave a closed task out.
	const auth = { Authorization: `Bearer ${TEST_TOKEN}` };
	const envelope = await (await request.get(`/api/plans/operator/threads/${threadId}`, { headers: auth })).json();
	const planId = envelope.plans[0].plan.id as string;
	const task = envelope.plans[0].nodes.find((node: { title: string }) => node.title === name) as { id: string };
	expect(task, 'the document has its comment task before promote').toBeTruthy();
	// An edit on disk outside a registered write leaves the recorded hash stale, so promote can't carry the identity
	// over: it tombstones the source and registers the copy as a new document.
	if (editedOnDisk) appendFileSync(source, '\nEdited outside CORSIA.');
	const promoted = seed('promote', artifactId, threadId) as {
		shared: string; source_state: string; source_comments: number; carried_comments: number;
		comments_before: number; comment_task: string | null;
	};
	expect([promoted.shared === artifactId, promoted.source_state]).toEqual(editedOnDisk ? [false, 'tombstone'] : [true, 'active']);
	const node = await (await request.get(`/api/plans/${planId}/nodes/${task.id}`, { headers: auth })).json();

	await openAppPastLanding(page);
	await sidebarThread(page, title).click();
	const steps = page.getByRole('list', { name: 'Plan steps' });
	await expect(steps).toBeVisible({ timeout: 30_000 });
	const row = steps.getByRole('listitem').filter({ hasText: name });
	if (editedOnDisk) {
		// The comments came along, so the old task closes as moved and nothing here asks the person to open
		// a document that isn't there; the ask and the threads now live on the promoted identity.
		expect([node.state, node.status_reason]).toEqual(['closed', MOVED]);
		await expect(row).toHaveCount(0);
		expect([promoted.comments_before > 0, promoted.carried_comments, promoted.source_comments])
			.toEqual([true, promoted.comments_before, 0]);
		const carried = await (await request.get(`/api/artifacts/${promoted.shared}/comments?${query}`)).json();
		// The API returns threads with their replies nested, so each thread counts once plus its replies.
		const rows = (carried.comments as { replies?: unknown[] }[]).reduce((sum, thread) => sum + 1 + (thread.replies?.length ?? 0), 0);
		expect(rows).toBe(promoted.comments_before);
		expect(promoted.comment_task, 'the promoted document carries the person’s comment task').toBeTruthy();
		await page.screenshot({ path: testInfo.outputPath('moved-comments.png') });
		return;
	}
	expect(node.state).toBe('pending');
	await expect(row.first()).toContainText('2 comments, 2 suggestions');
	await row.first().getByRole('button').first().click();
	await expect(page.locator('.review-bar').first()).toContainText('2 comments, 2 suggestions to review');
	await page.getByRole('button', { name: /\d+ comments?, \d+ unread/ }).first().click();
	await expect(page.getByRole('region', { name: /^Comment by / })).toHaveCount(4);
	await page.screenshot({ path: testInfo.outputPath('moved-document.png') });
});
