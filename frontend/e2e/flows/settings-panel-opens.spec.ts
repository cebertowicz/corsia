
import { expect, test } from '@playwright/test';
import { captureConsoleErrors, loginByUrl, openSettings, saveEvidenceScreenshot } from '../support/corsia';

test.describe('@flow:settings @surface:settings settings panel opens', () => {
	test('System settings has no retired Core Agent controls or legacy summary requests', async ({ page }, testInfo) => {
		const summaryRequests: string[] = [];
		page.on('request', (request) => {
			if (new URL(request.url()).pathname.startsWith('/api/summaries')) {
				summaryRequests.push(request.url());
			}
		});
		await loginByUrl(page);
		await openSettings(page);
		await page.getByRole('button', { name: 'System', exact: true }).click();
		await expect(page.getByRole('combobox', { name: 'Reviewer model', exact: true })).toBeVisible();
		await expect(page.locator('#core-agent-primary-model')).toHaveCount(0);
		await expect(page.getByText('Core Agent', { exact: true })).toHaveCount(0);
		await expect(page.locator('#summary-default-model')).toHaveCount(0);
		await expect(page.getByText('Thread summaries', { exact: true })).toHaveCount(0);
		await expect(page.getByText('Recent summaries', { exact: true })).toHaveCount(0);
		await saveEvidenceScreenshot(page, testInfo, 'system-without-legacy-summaries');
		expect(summaryRequests).toEqual([]);
	});

	test('opens to providers, navigates through tabs, raises zero console errors', async ({ page }, testInfo) => {
		const consoleErrors = captureConsoleErrors(page);
		await loginByUrl(page);
		await openSettings(page);

		await expect(page.locator('.providers-host').first()).toBeVisible();

		await page.getByRole('button', { name: 'Costs' }).click();
		await expect(page.locator('.settings-content')).toBeVisible();

		await page.getByRole('button', { name: 'System' }).click();
		await expect(page.locator('.settings-content')).toBeVisible();
		const autoApprove = page.getByRole('button', {
			name: 'Automatically approve reviewed host-access leases',
		});
		await expect(autoApprove).toBeVisible();
		const initialAutoApprove = await autoApprove.getAttribute('aria-pressed');
		expect(initialAutoApprove === 'true' || initialAutoApprove === 'false').toBe(true);
		const toggledAutoApprove = initialAutoApprove === 'true' ? 'false' : 'true';
		await autoApprove.click();
		await expect(autoApprove).toHaveAttribute('aria-pressed', toggledAutoApprove);
		await saveEvidenceScreenshot(page, testInfo, 'settings-host-access-auto-approval');
		await autoApprove.click();
		await expect(autoApprove).toHaveAttribute('aria-pressed', initialAutoApprove!);

		await page.getByRole('button', { name: 'Debug' }).click();
		await expect(page.locator('.settings-content')).toBeVisible();

		await saveEvidenceScreenshot(page, testInfo, 'settings-panel-opens');

		expect(consoleErrors).toEqual([]);
	});

	test('shows the rail badge and Update button only when an update is available', async ({ page }, testInfo) => {
		await page.route('**/api/system/update-status**', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					branch: 'dev', current_sha: 'aaaaaaa', remote_sha: 'bbbbbbb',
					commits_behind: 3, available: true, checked_at: 1, error: null,
				}),
			});
		});
		let restartRequest = '';
		await page.route('**/api/system/restart?**', async (route) => {
			restartRequest = route.request().url();
			await route.fulfill({
				status: 409,
				contentType: 'application/json',
				body: JSON.stringify({ detail: 'Python dependency check failed.' }),
			});
		});

		await loginByUrl(page);

		// Badge is visible on the Settings rail item before opening Settings.
		const settingsRail = page.locator('button[title="Settings"]').first();
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await expect(settingsRail.locator('.update-icon')).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'settings-rail-update-icon');

		// Click the rail directly; openSettings() would toggle the sidebar a
		// second time on mobile and close it.
		await settingsRail.click();
		await expect(page.getByRole('button', { name: 'Providers' })).toBeVisible();
		await page.getByRole('button', { name: 'System' }).click();
		await expect(page.getByText('3 commits behind on dev')).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'settings-system-update-available');

		// Update is the button that pulls; Restart stays a plain bounce.
		await page.getByRole('button', { name: 'Update', exact: true }).click();
		await expect(page.getByText('Update + restart?')).toBeVisible();
		await page.getByRole('button', { name: 'Confirm', exact: true }).click();

		await expect(page.getByText('Python dependency check failed.')).toBeVisible();
		expect(restartRequest).toContain('mode=update');
	});

	test('restart stays available and never pulls when no update exists', async ({ page }) => {
		await page.route('**/api/system/update-status**', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					branch: 'dev', current_sha: 'aaaaaaa', remote_sha: 'aaaaaaa',
					commits_behind: 0, available: false, checked_at: 1, error: null,
				}),
			});
		});
		let restartRequest = '';
		await page.route('**/api/version', async (route) => {
			const restarted = restartRequest !== '';
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					frontend: { full_hash: 'aaaaaaa' },
					backend: {
						full_hash: 'aaaaaaa',
						start_iso: restarted ? 'new-start' : 'old-start',
						current_head: { full_hash: 'aaaaaaa' },
					},
				}),
			});
		});
		await page.route('**/api/system/restart?**', async (route) => {
			restartRequest = route.request().url();
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ ok: true, message: 'Restarting.' }),
			});
		});

		await loginByUrl(page);
		await openSettings(page);
		await page.getByRole('button', { name: 'System' }).click();

		await expect(page.getByRole('button', { name: 'Update', exact: true })).toHaveCount(0);

		await page.getByRole('button', { name: 'Restart', exact: true }).click();
		await expect(page.getByText('Drain + restart?')).toBeVisible();
		await page.getByRole('button', { name: 'Confirm', exact: true }).click();

		await expect.poll(() => restartRequest).toContain('mode=restart');
		expect(restartRequest).not.toContain('mode=update');
	});
});
