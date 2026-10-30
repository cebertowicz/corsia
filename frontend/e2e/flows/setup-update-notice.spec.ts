import { test } from '../support/test';
import { expect } from '@playwright/test';
import { TEST_SESSION_TOKEN } from '../support/corsia';

test('@surface:first-run update notice waits for setup', async ({ page }) => {
	let complete = false;
	await page.route('**/api/setup/progress', (route) => route.fulfill({ json: { setup_completed: complete, step: complete ? null : 'models', local_ai: false } }));
	await page.route('**/api/setup/status', (route) => route.fulfill({ json: { apple_silicon: true, macos_supported: true, local_eligible: false, work_folder: '~/CORSIA' } }));
	await page.route('**/api/system/update-status**', (route) => route.fulfill({ json: { available: true, target_version: 'next', operation: null } }));
	const updateRead = page.waitForResponse((response) => response.url().includes('/api/system/update-status'));
	await page.goto(`/?token=${TEST_SESSION_TOKEN}`);
	await updateRead;
	await expect(page.locator('[data-setup-page="models"]')).toBeVisible();
	await expect(page.getByText('Update ready. Open Updates')).toHaveCount(0);

	complete = true;
	await page.reload();
	await expect(page.getByText('Update ready. Open Updates')).toBeVisible();
});
