import path from 'node:path';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import {
	createSession,
	loginByUrl,
	sidebarThread,
	saveEvidenceScreenshot,
	TEST_TOKEN,
} from '../support/corsia';

const COMBINED_MIN = 420 + 360 + 8;

test.describe('@flow:artifact-leaf @surface:chat @surface:documents artifact leaf compact shell', () => {
	test('gives the artifact the whole canvas, with Back to chat and an accessible tab sheet', async ({ page, request }, testInfo) => {
		test.skip((page.viewportSize()?.width ?? 0) >= COMBINED_MIN, 'Compact mode needs a canvas below the split minima');

		const stamp = Date.now();
		const sessionTitle = `artifact-compact-${stamp}`;
		const sessionId = await createSession(request, sessionTitle);
		const tok = encodeURIComponent(TEST_TOKEN);

		const tree = await (await request.get(`/api/files/tree?token=${tok}`)).json();
		const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
		const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
		expect(workspaceRoot).toEqual(expect.any(String));

		const names = [`compact-a-${stamp}.md`, `compact-b-${stamp}.md`];
		const paths = names.map((name) => path.join(workspaceRoot, name));
		for (const [i, docPath] of paths.entries()) {
			const seed = await request.put(`/api/files/content?token=${tok}`, {
				data: {
					path: docPath,
					text: `# Compact ${i === 0 ? 'first' : 'second'}\n\nBody ${i}.`,
					project_id: null,
					thread_id: sessionId,
					create: true,
				},
			});
			expect(seed.ok(), `seed failed: ${seed.status()} ${await seed.text()}`).toBeTruthy();
		}

		await page.addInitScript(
			({ threadId, tabs }) => {
				localStorage.setItem(
					'corsia:artifactWorkspace:v1',
					JSON.stringify({
						threads: {
							[threadId]: {
								tabs: tabs.map((t) => ({
									instanceId: t.instanceId,
									artifactId: null,
									path: t.path,
									title: t.title,
									loadedRevision: null,
									latestRevision: null,
									status: 'loading',
								})),
								activeTabId: tabs[0].instanceId,
								visible: true,
								activeSurface: 'artifact',
							},
						},
						splitWidth: null,
					}),
				);
			},
			{
				threadId: sessionId,
				tabs: paths.map((p, i) => ({ instanceId: `tab-${i}`, path: p, title: names[i] })),
			},
		);

		await loginByUrl(page);
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await sidebarThread(page, sessionTitle).click();

		// AC5 — the artifact owns the canvas and the header takes over.
		const back = page.getByRole('button', { name: 'Chat' });
		await expect(back).toBeVisible();
		await expect(page.getByText('Compact first')).toBeVisible();
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeHidden();
		const tabsAction = page.getByRole('button', { name: /Tabs \(2\)/ });
		await expect(tabsAction).toBeVisible();
		// Compact replaces the desktop strip rather than stacking both.
		await expect(page.getByRole('tablist', { name: 'Open side panel items' })).toHaveCount(0);
		await saveEvidenceScreenshot(page, testInfo, 'artifact-leaf-compact-full', { fullPage: false });

		// AC10 — the sheet traps focus.
		await tabsAction.click();
		const sheet = page.getByRole('dialog', { name: 'Open side panel items' });
		await expect(sheet).toBeVisible();
		await expect.poll(async () =>
			sheet.evaluate((el, active) => el.contains(active), await page.evaluateHandle(() => document.activeElement)),
		).toBe(true);
		await saveEvidenceScreenshot(page, testInfo, 'artifact-leaf-compact-sheet', { fullPage: false });

		// Escape closes the sheet only — the artifact still owns the canvas.
		await page.keyboard.press('Escape');
		await expect(sheet).toBeHidden();
		await expect(page.getByText('Compact first')).toBeVisible();
		await expect(tabsAction).toBeVisible();

		// Selecting from the sheet switches tabs and closes it.
		await tabsAction.click();
		await sheet.getByRole('button', { name: names[1], exact: true }).click();
		await expect(sheet).toBeHidden();
		await expect(page.getByText('Compact second')).toBeVisible();
		await expect(page.getByRole('button', { name: /Tabs \(2\)/ })).toBeVisible();

		// AC5 — Back returns to chat and keeps every tab.
		await back.click();
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
		await expect(page.getByRole('button', { name: /Tabs \(/ })).toHaveCount(0);
		const stored = await page.evaluate(() => JSON.parse(localStorage.getItem('corsia:artifactWorkspace:v1')!));
		expect(Object.values(stored.threads)[0]).toMatchObject({ activeSurface: 'chat' });
		expect((Object.values(stored.threads)[0] as { tabs: unknown[] }).tabs).toHaveLength(2);
		await saveEvidenceScreenshot(page, testInfo, 'artifact-leaf-compact-chat', { fullPage: false });
	});

	test('hardware Back closes the sheet, then the artifact, before leaving the app', async ({ page, request }) => {
		test.skip((page.viewportSize()?.width ?? 0) >= COMBINED_MIN, 'Compact mode needs a canvas below the split minima');

		const stamp = Date.now();
		const sessionTitle = `artifact-backstack-${stamp}`;
		const sessionId = await createSession(request, sessionTitle);
		const tok = encodeURIComponent(TEST_TOKEN);

		const tree = await (await request.get(`/api/files/tree?token=${tok}`)).json();
		const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
		const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
		const fileName = `backstack-${stamp}.md`;
		const docPath = path.join(workspaceRoot, fileName);
		const seed = await request.put(`/api/files/content?token=${tok}`, {
			data: { path: docPath, text: '# Back stack\n\nBody.', project_id: null, thread_id: sessionId, create: true },
		});
		expect(seed.ok()).toBeTruthy();

		await page.addInitScript(
			({ threadId, artifactPath, artifactTitle }) => {
				localStorage.setItem(
					'corsia:artifactWorkspace:v1',
					JSON.stringify({
						threads: {
							[threadId]: {
								tabs: [{
									instanceId: 'tab-back',
									artifactId: null,
									path: artifactPath,
									title: artifactTitle,
									loadedRevision: null,
									latestRevision: null,
									status: 'loading',
								}],
								activeTabId: 'tab-back',
								visible: true,
								activeSurface: 'artifact',
							},
						},
						splitWidth: null,
					}),
				);
			},
			{ threadId: sessionId, artifactPath: docPath, artifactTitle: fileName },
		);

		await loginByUrl(page);
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await sidebarThread(page, sessionTitle).click();
		await expect(page.getByText('Back stack')).toBeVisible();

		await page.getByRole('button', { name: /Tabs \(1\)/ }).click();
		const sheet = page.getByRole('dialog', { name: 'Open side panel items' });
		await expect(sheet).toBeVisible();

		// One back closes the sheet only.
		await page.goBack();
		await expect(sheet).toBeHidden();
		await expect(page.getByText('Back stack')).toBeVisible();

		// The next back hands the canvas to chat, keeping the tab.
		await page.goBack();
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
		const stored = await page.evaluate(() => JSON.parse(localStorage.getItem('corsia:artifactWorkspace:v1')!));
		expect((Object.values(stored.threads)[0] as { tabs: unknown[] }).tabs).toHaveLength(1);
	});
});
