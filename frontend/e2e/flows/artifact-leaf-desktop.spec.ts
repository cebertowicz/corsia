import path from 'node:path';
import { expect, test } from '@playwright/test';
import {
	createSession,
	loginByUrl,
	saveEvidenceScreenshot,
	TEST_TOKEN,
} from '../support/corsia';

test.describe('@flow:artifact-leaf @surface:chat @surface:documents artifact leaf desktop shell', () => {
	test('renders a thread-pinned split with comments, resize, collapse, and expand', async ({ page, request }, testInfo) => {
		test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'Slice 1 is desktop-only');

		const stamp = Date.now();
		const sessionTitle = `artifact-leaf-${stamp}`;
		const fileName = `artifact-leaf-${stamp}.md`;
		const sessionId = await createSession(request, sessionTitle);
		const tok = encodeURIComponent(TEST_TOKEN);

		const tree = await (await request.get(`/api/files/tree?token=${tok}`)).json();
		const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
		const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
		expect(workspaceRoot).toEqual(expect.any(String));
		const docPath = path.join(workspaceRoot, fileName);
		const docText = '# Artifact leaf E2E\n\nThread-scoped document body.';

		const seed = await request.put(`/api/files/content?token=${tok}`, {
			data: {
				path: docPath,
				text: docText,
				project_id: null,
				thread_id: sessionId,
				create: true,
			},
		});
		expect(seed.ok(), `seed failed: ${seed.status()} ${await seed.text()}`).toBeTruthy();

		await page.addInitScript(
			({ threadId, artifactPath, artifactTitle }) => {
				localStorage.setItem(
					'corsia:artifactWorkspace:v1',
					JSON.stringify({
						threads: {
							[threadId]: {
								tabs: [{
									artifactId: null,
									path: artifactPath,
									title: artifactTitle,
									loadedRevision: null,
									status: 'loading',
								}],
								activeTabId: `path:${artifactPath}`,
								visible: true,
								activeSurface: 'artifact',
							},
						},
						splitWidth: 420,
					}),
				);
			},
			{ threadId: sessionId, artifactPath: docPath, artifactTitle: fileName },
		);

		await loginByUrl(page);
		await page.getByText(sessionTitle).first().click();

		const appBox = await page.locator('.app').boundingBox();
		expect(appBox).not.toBeNull();
		expect(appBox!.height).toBe(page.viewportSize()!.height);

		const tab = page.getByRole('tab', { name: fileName });
		const divider = page.getByRole('separator', { name: 'Resize artifact panel' });
		await expect(tab).toBeVisible();
		await expect(page.getByText('Artifact leaf E2E')).toBeVisible();
		await expect(divider).toBeVisible();

		const collapseButton = page.getByRole('button', { name: 'Collapse artifact panel' });
		const [headerBox, collapseBox] = await Promise.all([
			page.locator('.shell-header').boundingBox(),
			collapseButton.boundingBox(),
		]);
		expect(headerBox).not.toBeNull();
		expect(collapseBox).not.toBeNull();
		expect(collapseBox!.y).toBeGreaterThanOrEqual(headerBox!.y + headerBox!.height);

		const widthBefore = Number(await divider.getAttribute('aria-valuenow'));
		await divider.focus();
		await page.keyboard.press('ArrowRight');
		await expect.poll(async () => Number(await divider.getAttribute('aria-valuenow'))).toBe(widthBefore - 24);
		await saveEvidenceScreenshot(page, testInfo, 'artifact-leaf-desktop-split', { fullPage: false });

		await collapseButton.click();
		const rail = page.getByRole('button', { name: 'Expand artifact panel (1 open)' });
		await expect(rail).toBeVisible();
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'artifact-leaf-desktop-rail', { fullPage: false });

		await rail.click();
		await expect(tab).toBeVisible();
		await expect(divider).toBeVisible();
	});
});
