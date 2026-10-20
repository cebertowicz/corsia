import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const TAG = '@surface:doc-reply-waiting';

function makeAgentReply(replyId: string, threadId: string): void {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !stateRoot) throw new Error('Requires isolated synthetic state');
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', [
			'import sqlite3, sys',
			'connection = sqlite3.connect(sys.argv[1])',
			'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Reviewer", sys.argv[3], sys.argv[2])).rowcount',
			'assert changed == 1, changed',
			'connection.commit()',
			'connection.close()',
		].join('\n'), path.join(stateRoot, 'corsia.db'), replyId, threadId,
	], { cwd: repoRoot, encoding: 'utf8' });
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		test(`${TAG} ${theme} ${width}px: row and margin markers clear only after thread disclosure`, async ({ page, request }) => {
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			await page.setViewportSize({ width, height: 900 });
			await page.emulateMedia({ colorScheme: theme });
			const title = `reply-waiting-${theme}-${width}-${Date.now()}`;
			const threadId = await createSession(request, title);
			const roots = await request.get(`/api/files/tree?token=${TEST_TOKEN}`);
			const workspace = ((await roots.json()).roots as Array<{ kind: string; path: string }>).find((root) => root.kind === 'workspace')?.path;
			expect(workspace).toEqual(expect.any(String));
			const name = `${title}.md`, docPath = path.join(workspace!, name), text = 'A reply marker belongs on this sentence.\n';
			const saved = await request.put(`/api/files/content?token=${TEST_TOKEN}`, { data: { path: docPath, text, thread_id: threadId, create: true } });
			expect(saved.ok(), await saved.text()).toBeTruthy();
			const artifactId = (await saved.json()).artifact.id as string;
			const root = await request.post(`/api/artifacts/${artifactId}/comments?token=${TEST_TOKEN}`, { data: { body: 'Please review.', anchor_text: 'reply marker', from_off: 2, to_off: 14 } });
			expect(root.ok(), await root.text()).toBeTruthy();
			const rootId = (await root.json()).comment.id as string;
			const reply = await request.post(`/api/artifacts/${artifactId}/comments?token=${TEST_TOKEN}`, { data: { body: 'Reviewed.', parent_id: rootId } });
			expect(reply.ok(), await reply.text()).toBeTruthy();
			makeAgentReply((await reply.json()).comment.id as string, threadId);

			await page.addInitScript(({ threadId, docPath, name }) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
				threads: { [threadId]: { tabs: [{ instanceId: 'reply-waiting', artifactId: null, path: docPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'reply-waiting', visible: true, activeSurface: 'artifact' } }, splitWidth: 420,
			})), { threadId, docPath, name });
			await loginByUrl(page);
			if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await page.getByText(title, { exact: true }).first().click();
			const replyMark = page.locator('.reply-mark');
			const marginReply = page.locator('.cm-comment-marker svg circle');
			if (width === 393) {
				// The artifact row remains mounted in the hidden pane while the document is active.
				await expect(replyMark).toHaveCount(1);
				await expect(marginReply).toBeVisible();
				await page.getByRole('button', { name: 'Toggle panel' }).click();
				await expect(replyMark).toBeVisible();
				await page.getByRole('button', { name: 'Toggle panel' }).click();
			} else {
				await expect(replyMark).toBeVisible();
				await expect(marginReply).toBeVisible();
			}
			// Opening the document alone keeps the passive markers intact.
			await expect(marginReply).toBeVisible();
			// Opening the thread from its marker shows its replies; showing the reply is what stamps it viewed.
			await page.locator('.cm-comment-marker').first().press('Enter');
			await expect(page.getByText('Reviewed.', { exact: true })).toBeVisible();
			await expect(page.locator('.reply-mark')).toHaveCount(0);
			await expect(page.locator('.cm-comment-marker svg circle')).toHaveCount(0);
		});
	}
}

export { TAG };
