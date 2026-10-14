import { expect, test } from '@playwright/test';
import { TEST_SESSION_TOKEN, openSettings, saveEvidenceScreenshot } from '../support/corsia';

test.describe('@surface:settings scheduled updates', () => {
	test('an owner can enable overnight updates and choose a local time', async ({ page }, testInfo) => {
		await page.goto(`/?token=${encodeURIComponent(TEST_SESSION_TOKEN)}`);
		await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
		const reset = await page.evaluate(async () => {
			const sessionToken = localStorage.getItem('corsia_token');
			const response = await fetch('/api/system/update-schedule', {
				method: 'PUT',
				headers: { Authorization: `Bearer ${sessionToken}`, 'Content-Type': 'application/json' },
				body: JSON.stringify({ automatic: false, time: '03:00' }),
			});
			return response.ok;
		});
		expect(reset).toBeTruthy();
		await page.reload();
		await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
		await openSettings(page);
		await page.getByRole('button', { name: 'System' }).click();

		const scheduleToggle = page.getByRole('button', { name: 'Automatically install updates overnight' });
		await expect(scheduleToggle).toHaveAttribute('aria-pressed', 'false');
		await expect(page.getByText('Keep CORSIA up to date automatically')).toBeVisible();
		await expect(page.getByText('Installs at 3:00 AM.')).toBeVisible();

		await scheduleToggle.click();
		await expect(scheduleToggle).toHaveAttribute('aria-pressed', 'true');
		const time = page.getByLabel('Automatic update time');
		await expect(time).toBeVisible();
		await time.fill('03:30');
		await expect(page.getByText('Installs at 3:30 AM.')).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'scheduled-updates-enabled');
	});
});
