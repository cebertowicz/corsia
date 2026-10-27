import { test } from '../support/test';
import { expect } from '@playwright/test';
import { loginByUrl, openSettings, openSettingsSection } from '../support/corsia';

test('shows the remote provider connection fallback', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop');
	test.setTimeout(90_000);
	await page.route('**/api/settings/network-servers?**', async (route) => {
		if (route.request().method() === 'POST') await route.fulfill({ status: 503, json: {} });
		else await route.continue();
	});
	for (const width of [1920, 393]) {
		await page.setViewportSize({ width, height: 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		await loginByUrl(page, { waitForComposer: false });
		await openSettings(page);
		await openSettingsSection(page, 'Providers');
		await page.getByRole('button', { name: 'Add provider', exact: true }).click();
		await page.getByRole('button', { name: /^oMLX on (another Mac|a Mac)/ }).click();
		const dialog = page.getByRole('dialog').last();
		await dialog.getByPlaceholder('100.64.1.5  ·  macbook-m4.local').fill('studio.local');
		await dialog.getByRole('button', { name: 'Test & add', exact: true }).click();
		await expect(dialog.getByText('Couldn’t reach studio.local. Check the address and that the server is running.')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`provider-connect-${width}.png`), fullPage: true, animations: 'disabled' });
	}
});

test('shows the real model server connection failure', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop');
	test.setTimeout(90_000);
	for (const width of [1920, 393]) {
		await page.setViewportSize({ width, height: 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		await loginByUrl(page, { waitForComposer: false });
		await openSettings(page);
		await openSettingsSection(page, 'Providers');
		await page.getByRole('button', { name: 'Add provider', exact: true }).click();
		await page.getByRole('button', { name: /^oMLX on (another Mac|a Mac)/ }).click();
		const dialog = page.getByRole('dialog').last();
		await dialog.getByPlaceholder('100.64.1.5  ·  macbook-m4.local').fill('127.0.0.1');
		await dialog.getByPlaceholder('8000', { exact: true }).fill('1');
		await dialog.getByRole('button', { name: 'Test & add', exact: true }).click();
		await expect(dialog.getByText('Couldn’t reach 127.0.0.1:1. Check the address and that the server is running.')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`server-connect-${width}.png`), fullPage: true, animations: 'disabled' });
	}
});
