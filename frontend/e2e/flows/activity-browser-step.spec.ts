import { test, expect } from '../support/test';
import { createSession, loginByUrl, sendChatMessage, sidebarThread } from '../support/corsia';
import { seedMockResponses } from '../support/mock';
import { startProbeServer } from '../support/probeServer';
import { revealWorked } from '../support/documentOpen';
import path from 'node:path';

test('Activity opens the browser step', async ({ page, request }, info) => {
	test.setTimeout(180_000);
	const phone = info.project.name === 'chromium-mobile';
	await page.setViewportSize(phone ? { width: 393, height: 852 } : { width: 1440, height: 900 });
	const probe = await startProbeServer();
	try {
		const title = `activity-browser-${Date.now()}`;
		const thread = await createSession(request, title);
		await loginByUrl(page);
		if (phone) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await sidebarThread(page, title).click();
		await seedMockResponses(request, [
			{ tool_calls: [{ id: 'open-headless', name: 'browser_open', arguments: JSON.stringify({ url: `${probe.origin}/browser-pane-probe.html?password` }) }], finish_reason: 'tool_calls', match_thread: thread },
			{ tool_calls: [{ id: 'read-page', name: 'browser_snapshot', arguments: '{}' }], finish_reason: 'tool_calls', match_thread: thread },
			{ content: 'Waiting for your password.', finish_reason: 'stop', match_thread: thread },
		]);
		await sendChatMessage(page, 'Sign in to the fixture.');
		await revealWorked(page);
		await expect(page.getByRole('button', { name: /Do: Enter your password in the shared browser/ })).toBeVisible({ timeout: 45_000 });
		if (phone) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.getByRole('button', { name: 'Activity', exact: true }).click();
		const row = page.locator('[data-activity-row]').filter({ hasText: 'Enter your password' });
		await expect(row).toBeVisible();
		await row.click();
		if (process.env.ACTIVITY_BASELINE) {
			await expect(page.locator('.pane-col').getByText('Enter your password', { exact: false }).first()).toBeVisible();
		} else {
			await expect(page.locator('canvas[aria-label="Shared browser"]')).toBeVisible({ timeout: 30_000 });
		}
		const suffix = process.env.ACTIVITY_BASELINE ? 'before' : 'after';
		if (process.env.ACTIVITY_OUT) await page.screenshot({ path: path.join(process.env.ACTIVITY_OUT, `activity-browser-${phone ? 393 : 1440}-${suffix}.png`), animations: 'disabled' });
	} finally { await probe.close(); }
});
