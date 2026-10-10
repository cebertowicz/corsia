import { expect, test } from '@playwright/test';
import { loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';

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
	await expect(page.getByText(firstTitle).first()).toBeVisible();

	try {
		await page.getByText(firstTitle).first().click();
		await expect(page.locator('button.model-chip .model-chip-name')).toHaveText('Mock Test Model');
		await page.getByText(secondTitle).first().click();
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
