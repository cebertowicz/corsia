
import { expect, test } from '@playwright/test';
import {
	e2eModel,
	loginByUrl,
	saveEvidenceScreenshot,
	sendChatMessage,
	waitForWsConnected,
	TEST_TOKEN,
} from '../support/corsia';

const tok = encodeURIComponent(TEST_TOKEN);

test.describe('@flow:nav-shell @surface:navigation v2 shell — project scope + attach (real)', () => {
	test('palette → scope → scoped chat; move-to-project persists across reload', async ({ page, request }, testInfo) => {
		test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'scope/attach logic is viewport-independent; verified on desktop');

		const stamp = Date.now();
		const projName = `e2e-scope-${stamp}`;

		const presp = await request.post(`/api/projects?token=${tok}`, { data: { name: projName, description: 'scope flow', status: 'active' } });
		expect(presp.ok()).toBeTruthy();
		const plist = await (await request.get(`/api/projects?token=${tok}&status=active`)).json();
		const proj = (plist.projects || []).find((p: any) => p.name === projName);
		expect(proj, 'seeded project present').toBeTruthy();
		const projId = proj.id;

		const loose = await (await request.post(`/api/threads?token=${tok}`, { data: { title: `loose-${stamp}`, model: e2eModel() } })).json();
		const looseId = loose.thread_id;
		expect(looseId).toBeTruthy();

		await loginByUrl(page);
		await waitForWsConnected(page);

		await expect(page.locator('button.section-header').filter({ hasText: 'Projects' })).toHaveCount(0);
		await expect(page.locator('.rail-scope-here')).toContainText('Workspace');

		await page.getByRole('button', { name: /Jump to/ }).click();
		await expect(page.locator('.palette')).toBeVisible();
		const projRow = page.locator('.palette .res').filter({ hasText: projName });
		await expect(projRow).toBeVisible({ timeout: 10_000 });

		await projRow.click();
		await expect(page.locator('.palette')).toHaveCount(0);
		await expect(page.getByRole('button', { name: 'Back to workspace' })).toBeVisible({ timeout: 10_000 });
		await expect(page.locator('.pane-scope')).toContainText(projName);

		// Session creation is Send/Queue-only (#611/#612 removed create-on-keystroke);
		// actually send so a scoped session is created (a bare fill no longer does).
		await sendChatMessage(page, `scoped hello ${stamp}`);
		await expect.poll(async () => {
			const s = await (await request.get(`/api/threads?token=${tok}`)).json();
			return (s.threads || []).filter((x: any) => x.project_id === projId).length;
		}, { message: 'a new chat in this scope is tagged to the project', timeout: 10_000 }).toBeGreaterThan(0);

		await saveEvidenceScreenshot(page, testInfo, 'nav-shell-scope-scoped');

		await page.getByRole('button', { name: 'Back to workspace' }).click();
		await expect(page.locator('.rail-scope-here')).toContainText('Workspace');
		const looseRow = page.locator('.thread-item').filter({ hasText: `loose-${stamp}` });
		await expect(looseRow).toBeVisible({ timeout: 10_000 });
		await looseRow.click({ button: 'right' });
		await page.locator('.context-menu-item').filter({ hasText: 'Move to project' }).hover();
		await page.locator('.submenu .submenu-item').filter({ hasText: projName }).click();

		await expect.poll(async () => {
			const s = await (await request.get(`/api/threads?token=${tok}`)).json();
			return (s.threads || []).find((x: any) => x.id === looseId)?.project_id ?? null;
		}, { message: 'loose conversation now belongs to the project', timeout: 10_000 }).toBe(projId);

		await page.reload();
		await loginByUrl(page);
		const after = await (await request.get(`/api/threads?token=${tok}`)).json();
		expect((after.threads || []).find((x: any) => x.id === looseId)?.project_id).toBe(projId);
	});

	test('palette keyboard nav: ↓↓ + Enter selects a project across sections', async ({ page, request }) => {
		test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'keyboard model is viewport-independent; verified on desktop');

		const stamp = Date.now();
		const projName = `e2e-kbd-${stamp}`;
		expect((await request.post(`/api/projects?token=${tok}`, { data: { name: projName, status: 'active' } })).ok()).toBeTruthy();

		await loginByUrl(page);
		await waitForWsConnected(page);

		await page.getByRole('button', { name: /Jump to/ }).click();
		await expect(page.locator('.palette')).toBeVisible();

		await page.locator('.palette input').fill(projName);
		await expect(page.locator('.palette .res').filter({ hasText: projName })).toBeVisible({ timeout: 10_000 });

		await page.keyboard.press('ArrowDown');
		await page.keyboard.press('ArrowDown');
		await page.keyboard.press('ArrowDown');
		await expect(page.locator('.palette .res.sel')).toContainText(projName);

		await page.keyboard.press('Enter');
		await expect(page.locator('.palette')).toHaveCount(0);
		await expect(page.getByRole('button', { name: 'Back to workspace' })).toBeVisible({ timeout: 10_000 });
		await expect(page.locator('.pane-scope')).toContainText(projName);
	});
});
