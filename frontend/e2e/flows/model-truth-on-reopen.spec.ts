import { expect, test } from '@playwright/test';
import { loginByUrl, saveEvidenceScreenshot, sidebarThread, TEST_TOKEN } from '../support/corsia';

test.describe('@flow:model-truth saved model on reopen', () => {
	test('shows the opened thread model immediately and ignores an older thread load', async ({ page, request }, testInfo) => {
	const stamp = Date.now();
	const firstTitle = `model-first-${stamp}`;
	const secondTitle = `model-second-${stamp}`;
	const first = await request.post(`/api/threads?token=${TEST_TOKEN}`, {
		data: { title: firstTitle, model: 'mock/test-model' },
	});
	const second = await request.post(`/api/threads?token=${TEST_TOKEN}`, {
		data: { title: secondTitle, model: 'mock/pressure-model' },
	});
	expect(first.ok()).toBeTruthy();
	expect(second.ok()).toBeTruthy();
	const firstId = (await first.json()).thread_id as string;

	let releaseFirst!: () => void;
	const firstPageGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
	await page.route(`**/api/threads/${firstId}/messages?*`, async (route) => {
		await firstPageGate;
		await route.continue();
	});
	await loginByUrl(page, { waitForComposer: false });
	// On a phone the sidebar is a drawer that closes once a thread opens, so each pick reopens it.
	const openThread = async (title: string) => {
		if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await sidebarThread(page, title).click();
	};
	await expect(page.getByText(firstTitle).first()).toBeVisible();

	try {
		await openThread(firstTitle);
		await expect(page.locator('button.model-chip .model-chip-name')).toHaveText('Mock Test Model');
		await openThread(secondTitle);
		await expect(page.locator('button.model-chip .model-chip-name')).toHaveText('Mock Pressure Model');
	} finally {
		releaseFirst();
	}
	await expect(page.locator('button.model-chip .model-chip-name')).toHaveText('Mock Pressure Model');
	await page.locator('button.model-chip').click();
	await expect(page.locator('button.model-list-item.active')).toContainText('Mock Pressure Model');
	await saveEvidenceScreenshot(page, testInfo, 'model-truth-on-reopen');
});
});
