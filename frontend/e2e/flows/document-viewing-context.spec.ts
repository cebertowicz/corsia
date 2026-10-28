import path from 'node:path';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';

const authenticated = (route: string) => `${route}?${new URLSearchParams({ token: TEST_TOKEN })}`;

test('@flow:document-viewing-context @surface:chat @surface:documents sends the viewed document and nearest viewport section only with a user message', async ({ page, request }) => {
	test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'Requires the desktop split canvas');
	const title = `viewing-context-${Date.now()}`;
	const threadId = await createSession(request, title);
	const tree = await (await request.get(authenticated('/api/files/tree'))).json();
	const root = tree.roots.find((candidate: { kind: string }) => candidate.kind === 'workspace').path;
	const docPath = path.join(root, `${title}.md`);
	const text = `# First\n${'Paragraph.\n\n'.repeat(100)}## Second\n${'Tail paragraph.\n\n'.repeat(100)}`;
	const secondLine = text.split('\n').findIndex((line) => line === '## Second') + 1;
	const seed = await request.put(authenticated('/api/files/content'), {
		data: { path: docPath, text, thread_id: threadId, create: true },
	});
	expect(seed.ok()).toBeTruthy();
	const registered = await request.post(authenticated('/api/files/register'), {
		data: { path: docPath, thread_id: threadId },
	});
	expect(registered.ok()).toBeTruthy();
	const artifactId = (await registered.json()).artifact.id;
	await page.addInitScript(({ threadId, docPath, artifactId }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: {
				tabs: [{ kind: 'document', instanceId: 'viewed-tab', artifactId, path: docPath,
					title: 'Viewed document', loadedRevision: null, latestRevision: null, status: 'loading' }],
				activeTabId: 'viewed-tab', visible: true, activeSurface: 'artifact',
			} }, splitWidth: 420,
		}));
	}, { threadId, docPath, artifactId });
	const inputs: Record<string, unknown>[] = [];
	page.on('websocket', (socket) => socket.on('framesent', ({ payload }) => {
		const frame = JSON.parse(String(payload));
		if (frame.type === 'message') inputs.push(frame);
	}));
	await loginByUrl(page);
	await page.getByText(title).first().click();
	const scroller = page.locator('.doc .cm-scroller');
	await expect(scroller).toBeVisible();
	await scroller.evaluate((element) => { element.scrollTop = element.scrollHeight; });
	await expect(page.locator('.doc .cm-content')).toContainText('Tail paragraph.');
	expect(inputs).toHaveLength(0);
	const composer = page.getByRole('textbox', { name: 'Message CORSIA' });
	await composer.fill('Explain this section.');
	await composer.press('Enter');
	await expect.poll(() => inputs.length).toBe(1);
	expect(inputs[0]).toMatchObject({ thread_id: threadId,
		viewing_document: { artifact_id: artifactId, heading_anchor: secondLine, heading_line: '## Second' } });
	await page.getByRole('button', { name: 'Collapse Files' }).click();
	await composer.fill('Now without a document.');
	await composer.press('Enter');
	await expect.poll(() => inputs.length).toBe(2);
	expect(inputs[1]).not.toHaveProperty('viewing_document');
});
