import { expect, test } from '@playwright/test';
import { TEST_SESSION_TOKEN, openSettings, saveEvidenceScreenshot } from '../support/corsia';

test.describe('@surface:settings scheduled updates', () => {
	test('an owner can enable overnight updates and choose a local time', async ({ page }, testInfo) => {
		await page.goto(`/?token=${encodeURIComponent(TEST_SESSION_TOKEN)}`);
		await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
		await openSettings(page);
		await page.getByRole('button', { name: 'System' }).click();

		const scheduleToggle = page.getByRole('button', { name: 'Automatically install updates overnight' });
		await expect(scheduleToggle).toHaveAttribute('aria-pressed', 'false');
		await expect(page.getByText('Install updates overnight.')).toBeVisible();

		await scheduleToggle.click();
		await expect(scheduleToggle).toHaveAttribute('aria-pressed', 'true');
		const time = page.getByLabel('Automatic update time');
		await expect(time).toBeVisible();
		await time.fill('03:30');
		await expect(page.getByText('Install updates at 03:30.')).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'scheduled-updates-enabled');
	});
});
