import path from 'node:path';
import { expect, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN, waitForModelListLoaded } from './corsia';

/** A comment left by another thread: a suggestion when it has a `replacement`, a plain comment when it has none. */
export interface SeededReviewItem { quote: string; replacement?: string; body: string }

export async function chooseThread(page: Page, title: string) {
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: title }).click();
}

export async function setEvidenceViewport(page: Page, testInfo: TestInfo) {
	await page.setViewportSize(testInfo.project.name.includes('mobile') ? { width: 393, height: 852 } : { width: 1440, height: 900 });
}

/**
 * Seeds `text` as a workspace document owned by a new thread, leaves `suggestions` on it from a second thread
 * (the way an agent leaves them), and opens the owner's canvas on it. `ids` follows `suggestions`.
 */
export async function openSuggestionDocument(page: Page, request: APIRequestContext, text: string, suggestions: SeededReviewItem[], { splitWidth = 540 }: { splitWidth?: number } = {}) {
	const stamp = Date.now();
	const ownerTitle = `suggestions-owner-${stamp}`;
	const reviewerTitle = `Suggestion Reviewer ${stamp}`;
	const ownerId = await createSession(request, ownerTitle);
	const reviewerId = await createSession(request, reviewerTitle);
	const params = { token: TEST_TOKEN };
	const tree = await request.get('/api/files/tree', { params });
	expect(tree.ok()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await tree.json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const name = `suggestions-${stamp}.md`;
	const docPath = path.join(workspace!, name);
	const seeded = await request.put('/api/files/content', { params, data: { path: docPath, text, thread_id: ownerId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId = (await seeded.json()).artifact.id;
	const ids: string[] = [];
	for (const { quote, replacement, body } of suggestions) {
		const created = await request.post('/api/delegation/docs/comments', {
			params, data: { artifact_id: artifactId, quote, replacement, body, as_session_id: reviewerId },
		});
		expect(created.ok(), await created.text()).toBeTruthy();
		ids.push((await created.json()).comment.id as string);
	}

	await page.addInitScript(({ ownerId, docPath, name, artifactId, splitWidth }) => {
		// The theme follows the emulated colour scheme, so the evidence below can show both.
		localStorage.setItem('corsia_theme', 'auto');
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [ownerId]: {
			tabs: [{ instanceId: 'suggestions-tab', artifactId, path: docPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' }],
			activeTabId: 'suggestions-tab', visible: true, activeSurface: 'artifact',
		} }, splitWidth }));
	}, { ownerId, docPath, name, artifactId, splitWidth });
	await loginByUrl(page);
	await chooseThread(page, ownerTitle);
	// On a phone the restored document covers the chat, so the composer's model chip can be hidden when checked.
	await waitForModelListLoaded(page);
	const editor = page.locator('.cm-content');
	await expect(editor).toContainText(text.split('\n').find((line) => line.trim())!.replace(/^#+\s*/, ''));

	const drawer = page.getByRole('region', { name: 'Document comments', exact: true });
	const row = (id: string) => drawer.locator(`[data-comment-id="${id}"]`);
	const accept = (id: string) => row(id).getByRole('button', { name: 'Accept', exact: true });
	const reject = (id: string) => row(id).getByRole('button', { name: 'Reject', exact: true });
	/** The saved file, read through the API rather than the screen. */
	const storedText = async () => {
		const stored = await request.get('/api/files/content', { params: { ...params, path: docPath } });
		expect(stored.ok(), await stored.text()).toBeTruthy();
		return (await stored.json()).text as string;
	};
	return { artifactId, accept, docPath, drawer, editor, ids, ownerId, params, reject, reviewerId, reviewerTitle, row, storedText };
}
