import path from 'node:path';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import {
	createSession,
	loginByUrl,
	saveEvidenceScreenshot,
	sendChatMessage,
	TEST_TOKEN,
	waitForWsConnected,
} from '../support/corsia';
import { readMockCallCount, seedMockResponses } from '../support/mock';

// No frame is injected: each reader is a real mock-provider turn whose
// read_file settles through the registry observer into doc_activity.
async function readAsAgent(reader: Page, request: APIRequestContext, title: string, docPath: string) {
	await reader.goto(`/?token=${encodeURIComponent(TEST_TOKEN)}`);
	if ((reader.viewportSize()?.width ?? 0) <= 768) {
		await reader.getByRole('button', { name: 'Toggle sidebar' }).click();
	}
	await reader.locator('.thread-item').filter({ hasText: title }).click({ timeout: 10_000 });
	await waitForWsConnected(reader);
	await seedMockResponses(request, [
		{
			tool_calls: [{ id: `read-${Date.now()}`, name: 'read_file', arguments: JSON.stringify({ path: docPath }) }],
			finish_reason: 'tool_calls',
		},
		{ content: `Read by ${title}.`, finish_reason: 'stop' },
	]);
	await sendChatMessage(reader, 'Read the document.');
	// Focus mode hides the plain reply; the turn is done once the post-tool
	// call has taken the second reply, so the next seed cannot be consumed here.
	await expect.poll(() => readMockCallCount(request), { timeout: 20_000 }).toBe(2);
}

// The whole popover is on screen and nothing (the Workspace pane) covers its edges.
async function expectUncovered(page: Page, surface: Locator) {
	const box = await surface.boundingBox();
	expect(box).not.toBeNull();
	const width = page.viewportSize()!.width;
	expect(box!.x).toBeGreaterThanOrEqual(0);
	expect(box!.x + box!.width).toBeLessThanOrEqual(width);
	const covered = await surface.evaluate((el) => {
		const r = el.getBoundingClientRect();
		const y = r.top + r.height / 2;
		return [r.left + 4, r.right - 4].filter((x) => !el.contains(document.elementFromPoint(x, y)));
	});
	expect(covered).toEqual([]);
}

const escapeRegExp = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

test.describe('@flow:doc-presence @surface:doc-presence @surface:documents document presence', () => {
	test('two agent reads show both readers, and the popover opens by keyboard', async ({ page, request }, testInfo) => {
		const stamp = Date.now();
		const viewerTitle = `presence-viewer-${stamp}`;
		const readers = [`Presence Reader A ${stamp}`, `Presence Reader B ${stamp}`];
		// The viewer is not in their own presence row, so a first reader keeps three avatars in the stack.
		const firstReader = `Presence Reader 0 ${stamp}`;
		const viewerId = await createSession(request, viewerTitle);
		for (const title of [firstReader, ...readers]) await createSession(request, title);

		const tok = encodeURIComponent(TEST_TOKEN);
		const tree = await request.get(`/api/files/tree?token=${tok}`);
		expect(tree.ok()).toBeTruthy();
		const roots: Array<{ kind: string; path: string }> = (await tree.json()).roots;
		const workspace = roots.find((root) => root.kind === 'workspace')?.path;
		expect(workspace).toEqual(expect.any(String));
		const docTitle = `presence-${stamp}.md`;
		const docPath = path.join(workspace!, docTitle);
		const seeded = await request.put(`/api/files/content?token=${tok}`, {
			data: { path: docPath, text: '# Presence\n\nAgents read this document.\n', thread_id: viewerId, create: true },
		});
		expect(seeded.ok(), await seeded.text()).toBeTruthy();

		await page.addInitScript(({ viewerId, docPath, docTitle }) => {
			localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
				threads: { [viewerId]: {
					tabs: [{ instanceId: 'presence-tab', artifactId: null, path: docPath, title: docTitle,
						loadedRevision: null, latestRevision: null, status: 'loading' }],
					activeTabId: 'presence-tab', visible: true, activeSurface: 'artifact',
				} }, splitWidth: 420,
			}));
		}, { viewerId, docPath, docTitle });
		await loginByUrl(page);
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await page.getByText(viewerTitle, { exact: true }).first().click();
		await expect(page.locator('.cm-content')).toContainText('Agents read this document.');

		const reader = await page.context().newPage();
		for (const title of [firstReader, ...readers]) await readAsAgent(reader, request, title, docPath);
		await reader.close();

		const stack = page.getByRole('group', { name: '3 participants' });
		await expect(stack).toBeVisible({ timeout: 10_000 });
		// Most recent reader leads the stack.
		const latest = stack.getByRole('button', { name: readers[1] });
		const earlier = stack.getByRole('button', { name: readers[0] });
		await expect(latest).toBeVisible();
		await expect(earlier).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'doc-presence-two-readers', { fullPage: false });
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			// Each avatar takes one phone slot, so the stack steps evenly (plain/detail slot parity: Avatar.test.ts).
			const lefts = await stack.locator('.avatar').evaluateAll((els) => els.map((el) => el.getBoundingClientRect().left));
			expect(lefts).toHaveLength(3);
			expect(Math.abs((lefts[1] - lefts[0]) - (lefts[2] - lefts[1]))).toBeLessThanOrEqual(1);
		}

		const detail = stack.getByRole('dialog');
		await latest.focus();
		await expect(detail).toHaveText(new RegExp(`^${escapeRegExp(readers[1])} · read `));
		await expectUncovered(page, detail);
		await page.keyboard.press('Tab');
		await expect(earlier).toBeFocused();
		await expect(detail).toHaveCount(1);
		await expect(detail).toHaveText(new RegExp(`^${escapeRegExp(readers[0])} · read `));
		await expectUncovered(page, detail);
		await saveEvidenceScreenshot(page, testInfo, 'doc-presence-popover', { fullPage: false });
		await page.keyboard.press('Escape');
		await expect(detail).toHaveCount(0);
	});
});
