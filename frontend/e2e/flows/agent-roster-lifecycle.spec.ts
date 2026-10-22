import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';

test.describe('@flow:agent-roster-lifecycle @surface:navigation', () => {
	test('a saved Agent can be deactivated without losing its conversation', async ({ page, request }, testInfo) => {
		const title = `Roster owner ${testInfo.project.name} ${Date.now()}`;
		const threadId = await createSession(request, title);
		const promoted = await request.patch(`/api/agents/${threadId}?token=${encodeURIComponent(TEST_TOKEN)}`, {
			data: { responsibility: 'Owns release verification.', active: true },
		});
		expect(promoted.ok()).toBeTruthy();
		await loginByUrl(page, { waitForComposer: false });
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		const agent = page.locator('.scoped-item.agent').filter({ hasText: title });
		await expect(agent).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'saved-agent');
		await agent.click({ button: 'right' });
		const menu = page.locator('.context-menu');
		await expect(menu).toBeVisible();
		await menu.getByRole('button', { name: 'Deactivate Agent' }).click();
		await expect(agent).toHaveCount(0);
		await expect(page.locator('.thread-item').filter({ hasText: title })).toBeVisible();
		const roster = await request.get(`/api/agents?token=${encodeURIComponent(TEST_TOKEN)}`);
		expect(roster.ok()).toBeTruthy();
		expect((await roster.json()).agents.map((row: { thread_id: string }) => row.thread_id))
			.not.toContain(threadId);
		await saveEvidenceScreenshot(page, testInfo, 'deactivated-conversation');
	});
});
