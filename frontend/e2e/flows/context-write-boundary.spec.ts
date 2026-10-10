import { expect, test } from '@playwright/test';
import { loginByUrl, openSettings, saveEvidenceScreenshot } from '../support/corsia';

test.describe('@flow:context-write @surface:settings context write outcomes', () => {
	test('creates real context and keeps its visible row when removal is rejected', async ({ page }, testInfo) => {
		await loginByUrl(page);
		await openSettings(page);
		await page.getByRole('button', { name: 'System', exact: true }).click();
		await page.getByText('Create new context file', { exact: true }).click();
		// context/*.md is directory-backed regardless of saved-list membership.
		const filename = `extras/write-boundary-${testInfo.project.name}.md`;
		await page.getByRole('textbox', { name: 'New system context file' }).fill(filename);
		await page.getByRole('button', { name: 'Create', exact: true }).click();
		const row = page.locator('.context-file-row').filter({ hasText: filename.split('/').at(-1) });
		await expect(row.getByText('Loaded', { exact: true })).toBeVisible();
		await expect(page.getByRole('textbox', { name: 'New system context file' })).toHaveValue('');

		// SQLite race tests prove the backend conflict. Inject only its HTTP
		// response here to prove the real UI preserves the row and shows the detail.
		const endpoint = '**/api/context/bootloader';
		const detail = 'Context changed while validating files. Refresh and try again.';
		await page.route(endpoint, route => route.fulfill({
			status: 400, contentType: 'application/json', body: JSON.stringify({ detail }),
		}));
		await row.getByRole('button', { name: /Remove/ }).click();
		await expect(page.locator('.settings-error')).toHaveText(detail);
		await expect(row.getByText('Loaded', { exact: true })).toBeVisible();
		await page.locator('.settings-error').scrollIntoViewIfNeeded();
		await saveEvidenceScreenshot(page, testInfo, 'context-conflict-preserves-row');
		await page.unroute(endpoint);
		await row.getByRole('button', { name: /Remove/ }).click();
		await expect(row).toHaveCount(0);
		await expect(page.locator('.settings-error')).toHaveCount(0);
	});

	test('shows the partial file-save error without clearing the requested filename', async ({ page }, testInfo) => {
		const detail = 'File saved at context/saved.md, but context was not updated: Context changed while validating files. Refresh and try again.';
		await page.route('**/api/context/bootloader/file', route => route.fulfill({
			status: 400, contentType: 'application/json', body: JSON.stringify({ detail }),
		}));
		await loginByUrl(page);
		await openSettings(page);
		await page.getByRole('button', { name: 'System', exact: true }).click();
		await page.getByText('Create new context file', { exact: true }).click();
		const input = page.getByRole('textbox', { name: 'New system context file' });
		await input.fill('context/saved.md');
		await page.getByRole('button', { name: 'Create', exact: true }).click();
		await expect(page.locator('.settings-error')).toHaveText(detail);
		await expect(input).toHaveValue('context/saved.md');
		await expect(page.getByRole('button', { name: 'Create', exact: true })).toBeEnabled();
		await page.locator('.settings-error').scrollIntoViewIfNeeded();
		await saveEvidenceScreenshot(page, testInfo, 'context-partial-save-is-visible');
	});
});
