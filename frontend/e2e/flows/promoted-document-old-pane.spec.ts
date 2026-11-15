// A pane still open on a document that is promoted under it: the reply the person sends from it lands on the promoted
// document, where their comments now live, instead of on the retired file the pane was opened on.
import { execFileSync } from 'node:child_process';
import { appendFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN } from '../support/corsia';

const repo = path.resolve(process.cwd(), '..');
const PANE_BODY = 'Launch plan details for review.';
const REPLY = 'Reply sent from the pane that was open before the promote';

test('a reply sent from a pane open before a promote lands on the promoted document', async ({ page, request }, testInfo) => {
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
	const title = `Old pane ${Date.now()}`;
	const threadId = await createSession(request, title);
	const root = seed('working-root', '-', threadId).root as string;
	mkdirSync(root, { recursive: true });
	const name = 'Old pane plan.md';
	const source = path.join(root, name);
	const query = new URLSearchParams({ token: TEST_TOKEN });
	const saved = await request.put(`/api/files/content?${query}`, { data: { path: source, text: PANE_BODY, thread_id: threadId, create: true } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	seed('feedback', artifactId, threadId);

	const listOn = async (id: string) => {
		const body = await (await request.get(`/api/artifacts/${id}/comments?${query}`)).json();
		return body.comments as { body: string; anchor_text: string | null; from_off: number | null; to_off: number | null; replies?: { body: string }[] }[];
	};

	// The person opens the document and its comments: the pane now holds the working file's id.
	await openAppPastLanding(page);
	await sidebarThread(page, title).click();
	const steps = page.getByRole('list', { name: 'Plan steps' });
	await expect(steps).toBeVisible({ timeout: 30_000 });
	await steps.getByRole('listitem').filter({ hasText: name }).first().getByRole('button').first().click();
	await page.getByRole('button', { name: /\d+ comments?, \d+ unread/ }).first().click();
	const threads = page.getByRole('region', { name: /^Comment by / });
	await expect(threads).toHaveCount(4);
	const placedBefore = (await listOn(artifactId)).map((thread) => [thread.anchor_text, thread.from_off, thread.to_off]);

	// Under the open pane, the file is edited outside CORSIA and then promoted: the promote cannot carry the working
	// file's identity, so it retires it and carries its comments to the promoted document.
	appendFileSync(source, '\nEdited outside CORSIA.');
	const promoted = seed('promote', artifactId, threadId) as { shared: string; source_state: string; source_comments: number; carried_comments: number };
	expect([promoted.shared === artifactId, promoted.source_state, promoted.source_comments]).toEqual([false, 'tombstone', 0]);

	// The reply goes from the pane that is still open on the retired id.
	const field = page.getByPlaceholder('Reply…').first();
	await field.fill(REPLY);
	await field.press('Enter');

	// It shows on the promoted document with the comments that moved there (the retired id's own rows are checked
	// by the backend tests); the list the open pane reads is that same list.
	const textsOn = async (id: string) => (await listOn(id)).flatMap((thread) => [thread.body, ...(thread.replies ?? []).map((reply) => reply.body)]);
	await expect.poll(async () => (await textsOn(promoted.shared)).includes(REPLY), { timeout: 20_000 }).toBe(true);
	expect(await textsOn(artifactId)).toEqual(await textsOn(promoted.shared));

	// The same pane, without reopening: all four comments and the reply, each comment on its quoted text of the body
	// the pane still shows (offsets read against that body, not shifted).
	await expect(page.getByText(REPLY).first()).toBeVisible({ timeout: 15_000 });
	await expect(page.getByRole('region', { name: /^Comment by / })).toHaveCount(4);
	const place = (thread: { anchor_text: string | null; from_off: number | null; to_off: number | null }) => [thread.anchor_text, thread.from_off, thread.to_off];
	const after = (await listOn(artifactId)).map(place);
	expect(after).toHaveLength(4);
	expect(after).toEqual(placedBefore);
	await page.screenshot({ path: testInfo.outputPath('reply-after-promote.png') });
});
