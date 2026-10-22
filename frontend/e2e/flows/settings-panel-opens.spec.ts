
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { captureConsoleErrors, loginByUrl, openSettings, openSettingsSection, saveEvidenceScreenshot, TEST_SESSION_TOKEN } from '../support/corsia';

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
		await openSettingsSection(page, 'System');
		await expect(page.getByRole('combobox', { name: 'Reviewer model', exact: true })).toBeVisible();
		await expect(page.locator('#core-agent-primary-model')).toHaveCount(0);
		await expect(page.getByText('Core Agent', { exact: true })).toHaveCount(0);
		await expect(page.locator('#summary-default-model')).toHaveCount(0);
		await expect(page.getByText('Thread summaries', { exact: true })).toHaveCount(0);
		await expect(page.getByText('Recent summaries', { exact: true })).toHaveCount(0);
		await saveEvidenceScreenshot(page, testInfo, 'system-without-legacy-summaries');
		expect(summaryRequests).toEqual([]);
	});

	test('opens to providers, navigates through sections, raises zero console errors', async ({ page }, testInfo) => {
		const consoleErrors = captureConsoleErrors(page);
		// Host-access settings change only for a signed-in person (api/system.py require_human_user),
		// so this flow signs in with the disposable person session, not the machine token.
		await page.goto(`/?token=${TEST_SESSION_TOKEN}`);
		await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible({ timeout: 20_000 });
		if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.getByRole('button', { name: 'New chat' }).first().click();
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
		await openSettings(page);
		await openSettingsSection(page, 'Providers');
		await expect(page.getByRole('heading', { name: 'Providers', exact: true })).toBeVisible();
		await expect(page.getByRole('button', { name: 'Add provider', exact: true })).toBeVisible();

		await openSettingsSection(page, 'System');
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

		await openSettingsSection(page, 'Debug');
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
					current_version: '1.0.0', target_version: '1.1.0',
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
		const providers = (page.viewportSize()?.width ?? 0) <= 768
			? page.locator('.settings-view .mobile-root [data-settings-section="providers"] button.face')
			: page.locator('.settings-view .desktop-nav [data-settings-section="providers"]');
		await expect(providers).toBeVisible();
		await openSettingsSection(page, 'System');
		// SettingsUpdate.svelte: the Updates card names the version on offer; Update now is the one action.
		await expect(page.getByText('1.1.0 available')).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'settings-system-update-available');

		// Update now is the button that pulls (no confirm step); Restart stays a plain bounce.
		await page.getByRole('button', { name: 'Update now', exact: true }).click();

		await expect(page.getByText('Python dependency check failed.').first()).toBeVisible();
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
		await openSettingsSection(page, 'System');

		await expect(page.getByRole('button', { name: 'Update now', exact: true })).toHaveCount(0);

		await page.getByRole('button', { name: 'Restart', exact: true }).click();
		await expect(page.getByText('Drain + restart?')).toBeVisible();
		await page.getByRole('button', { name: 'Confirm', exact: true }).click();

		await expect.poll(() => restartRequest).toContain('mode=restart');
		expect(restartRequest).not.toContain('mode=update');
	});
});
