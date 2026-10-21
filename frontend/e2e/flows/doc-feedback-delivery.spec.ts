import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { showAllThreads } from '../support/commentDrawer';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { openUnsentFooter } from '../support/docFeedbackFooter';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

function makeFeedbackBatchDue(artifactId: string): void {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !stateRoot) {
		throw new Error('Due-batch fixture requires isolated E2E mock state');
	}
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', [
			'import sqlite3, sys',
			'connection = sqlite3.connect(sys.argv[1])',
			'changed = connection.execute("UPDATE doc_feedback_batches SET closes_at=? WHERE state=\'open\' AND author_kind=\'user\' AND artifact_id=?", ("2000-01-01T00:00:00+00:00", sys.argv[2])).rowcount',
			'assert changed == 1, changed',
			'connection.commit()',
			'connection.close()',
		].join('\n'), path.join(stateRoot, 'corsia.db'), artifactId,
	], { cwd: repoRoot, encoding: 'utf8' });
}

function seedAgentComment(artifactId: string, commentId: string, threadId: string): void {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !stateRoot) {
		throw new Error('Agent-comment fixture requires isolated E2E mock state');
	}
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', [
			'import sqlite3, sys',
			'connection = sqlite3.connect(sys.argv[1])',
			'connection.execute("DELETE FROM doc_feedback_batches WHERE author_kind=\'user\' AND artifact_id=?", (sys.argv[2],))',
			'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Reviewer", sys.argv[4], sys.argv[3])).rowcount',
			'assert changed == 1, changed',
			'connection.commit()',
			'connection.close()',
		].join('\n'), path.join(stateRoot, 'corsia.db'), artifactId, commentId, threadId,
	], { cwd: repoRoot, encoding: 'utf8' });
}

function deliveredOwnerTask(artifactId: string): { title: string; description: string } | null {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !stateRoot) {
		throw new Error('Delivered-task fixture requires isolated E2E mock state');
	}
	const output = execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', [
			'import json, sqlite3, sys',
			'connection = sqlite3.connect(sys.argv[1])',
			'row = connection.execute("SELECT n.title,n.description FROM doc_feedback_batches b JOIN plan_nodes n ON n.id=b.delivered_node_id WHERE b.artifact_id=? AND b.state=\'delivered\' ORDER BY b.updated_at DESC LIMIT 1", (sys.argv[2],)).fetchone()',
			'print(json.dumps(dict(zip(("title", "description"), row)) if row else None))',
			'connection.close()',
		].join('\n'), path.join(stateRoot, 'corsia.db'), artifactId,
	], { cwd: repoRoot, encoding: 'utf8' });
	return JSON.parse(output) as { title: string; description: string } | null;
}

test('@surface:doc-feedback-delivery closes a due user batch in the open canvas', async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	test.setTimeout(100_000);
	const deliveredFrames: Array<{ type?: string; artifact_id?: string }> = [];
	let documentReloads = 0;
	let navigations = 0;
	page.on('websocket', (socket) => socket.on('framereceived', (frame) => {
		try { deliveredFrames.push(JSON.parse(String(frame.payload))); } catch { /* Ignore non-JSON frames. */ }
	}));
	page.on('request', (outgoing) => {
		if (new URL(outgoing.url()).pathname === '/api/files/content') documentReloads += 1;
	});
	page.on('framenavigated', (frame) => {
		if (frame === page.mainFrame()) navigations += 1;
	});
	const stamp = Date.now();
	const title = `feedback-delivery-${stamp}`;
	const threadId = await createSession(request, title);
	const token = encodeURIComponent(TEST_TOKEN);
	const rootsResponse = await request.get(`/api/files/tree?token=${token}`);
	expect(rootsResponse.ok()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await rootsResponse.json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`;
	const docPath = path.join(workspace!, fileName);
	const seeded = await request.put(`/api/files/content?token=${token}`, {
		data: { path: docPath, text: 'A comment that will become due.\n', thread_id: threadId, create: true },
	});
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId = (await seeded.json()).artifact.id as string;
	const created = await request.post(`/api/artifacts/${artifactId}/comments?token=${token}`, {
		data: { body: 'This pending comment is delivered by the due-batch seam.', anchor_text: 'comment', from_off: 2, to_off: 9 },
	});
	expect(created.ok(), await created.text()).toBeTruthy();

	await page.addInitScript(({ threadId, docPath, fileName }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: {
				tabs: [{ instanceId: 'feedback-tab', artifactId: null, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }],
				activeTabId: 'feedback-tab', visible: true, activeSurface: 'artifact',
			} }, splitWidth: 420,
		}));
	}, { threadId, docPath, fileName });
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	// The pending batch shows as the comments drawer's Send now footer, which goes once the batch is delivered.
	const send = await openUnsentFooter(page);
	await expect(send.getByRole('button', { name: 'Send now', exact: true })).toBeVisible();
	await page.evaluate(() => {
		(window as Window & { feedbackVisibilityChanges?: number }).feedbackVisibilityChanges = 0;
		document.addEventListener('visibilitychange', () => {
			const current = window as Window & { feedbackVisibilityChanges?: number };
			current.feedbackVisibilityChanges = (current.feedbackVisibilityChanges ?? 0) + 1;
		});
	});
	documentReloads = 0;
	navigations = 0;
	makeFeedbackBatchDue(artifactId);
	await expect.poll(() => deliveredFrames.some((frame) =>
		frame.type === 'doc_comment_changed' && frame.artifact_id === artifactId,
	), { timeout: 75_000 }).toBe(true);
	await expect(send).toHaveCount(0, { timeout: 75_000 });
	expect(documentReloads).toBe(0);
	expect(navigations).toBe(0);
	expect(await page.evaluate(() =>
		(window as Window & { feedbackVisibilityChanges?: number }).feedbackVisibilityChanges,
	)).toBe(0);
});

test('@surface:doc-reopen-feedback-delivery sends a re-opened comment to the document owner', async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	const title = `reopen-feedback-${Date.now()}`;
	const threadId = await createSession(request, title);
	const token = TEST_TOKEN;
	const rootsResponse = await request.get(`/api/files/tree?token=${token}`);
	expect(rootsResponse.ok()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await rootsResponse.json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`;
	const docPath = path.join(workspace!, fileName);
	const seeded = await request.put(`/api/files/content?token=${token}`, {
		data: { path: docPath, text: 'An agent comment can be resolved and re-opened.\n', thread_id: threadId, create: true },
	});
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId = (await seeded.json()).artifact.id as string;
	const created = await request.post(`/api/artifacts/${artifactId}/comments?token=${token}`, {
		data: { body: 'Please revisit this comment.', anchor_text: 'comment', from_off: 9, to_off: 16 },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	const commentId = (await created.json()).comment.id as string;
	seedAgentComment(artifactId, commentId, threadId);

	await page.addInitScript(({ threadId, docPath, fileName }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: {
				tabs: [{ instanceId: 'reopen-tab', artifactId: null, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }],
				activeTabId: 'reopen-tab', visible: true, activeSurface: 'artifact',
			} }, splitWidth: 420,
		}));
	}, { threadId, docPath, fileName });
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await page.getByRole('button', { name: /^1 open thread\b/ }).click();
	await showAllThreads(page.getByRole('region', { name: 'Document comments', exact: true }));
	const comment = page.getByRole('region', { name: /^Comment by / });
	await expect(comment).toContainText('Please revisit this comment.');
	await comment.getByRole('button', { name: 'Resolve', exact: true }).click();
	const resolved = page.getByRole('region', { name: /^Resolved comment by / });
	await expect(resolved).toBeVisible();
	await resolved.getByRole('button', { name: 'Resolved · 1 message' }).click();
	// The document has an owner, so the card says a re-open reaches it.
	await expect(resolved).toContainText('Re-open asks the agent to revisit.');
	await resolved.getByRole('button', { name: 'Re-open', exact: true }).click();
	const footer = page.getByRole('region', { name: 'Document comments' }).locator('[data-drawer-unsent]');
	await expect(footer).toContainText('1 message not sent yet');
	await footer.getByRole('button', { name: 'Send now', exact: true }).click();
	await expect.poll(() => deliveredOwnerTask(artifactId), { timeout: 20_000 }).toMatchObject({
		title: expect.stringMatching(/re-opened 1 comment/),
		description: expect.stringContaining(`Comment ${commentId} (re-opened by`),
	});
});
