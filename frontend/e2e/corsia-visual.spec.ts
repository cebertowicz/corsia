import { expect, test } from '@playwright/test';
import { loginByUrl, openSettings } from './support/corsia';

test.skip(process.env.CORSIA_VISUAL !== '1', 'Visual regression baselines are opt-in until approved.');

test.describe('@visual @surface:auth @surface:chat @surface:settings CORSIA visual baselines', () => {
	test('auth gate visual baseline', async ({ page }) => {
		await page.goto('/');
		await expect(page.getByPlaceholder('Username')).toBeVisible();
		await expect(page).toHaveScreenshot('auth-gate.png', { fullPage: true });
	});

	test('empty chat shell visual baseline', async ({ page }) => {
		await loginByUrl(page);
		await expect(page).toHaveScreenshot('chat-shell-empty.png', { fullPage: true });
	});

	test('settings provider surface visual baseline', async ({ page }) => {
		await loginByUrl(page);
		await openSettings(page);
		await expect(page).toHaveScreenshot('settings-providers.png', { fullPage: true });
	});
});
