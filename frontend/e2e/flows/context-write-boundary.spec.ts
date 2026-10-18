import { expect, test, type Locator, type Page } from '@playwright/test';
import { loginByUrl, openSettings, saveEvidenceScreenshot } from '../support/corsia';

// Settings › Context lists the workspace's files first, under "All projects" (SettingsContext.svelte).
const CREATE_FIELD = 'New context file for all projects';

async function openContextSection(page: Page) {
	await page.getByRole('button', { name: 'Context', exact: true }).click();
	await expect(page.getByRole('region', { name: 'All projects' })).toBeVisible();
}

async function createContextFile(page: Page, filename: string) {
	await page.getByRole('button', { name: 'New file', exact: true }).first().click();
	await page.getByRole('textbox', { name: CREATE_FIELD }).fill(filename);
	await page.getByRole('button', { name: 'Create', exact: true }).click();
}

function contextRow(page: Page, name: string) {
	return page.locator(`[data-settings-row="${name}"]`);
}

/** A row's verbs live on its context menu (right-click or long-press). */
async function removeContextRow(page: Page, row: Locator) {
	await row.click({ button: 'right' });
	await page.getByRole('menuitem', { name: 'Remove from context' }).click();
}

const settingsError = (page: Page) => page.getByRole('alert').filter({ hasText: /\S/ });

test.describe('@flow:context-write @surface:settings context write outcomes', () => {
	test('creates real context and keeps its visible row when removal is rejected', async ({ page }, testInfo) => {
		await loginByUrl(page);
		await openSettings(page);
		await openContextSection(page);
		// context/*.md is directory-backed regardless of saved-list membership.
		const filename = `extras/write-boundary-${testInfo.project.name}.md`;
		await createContextFile(page, filename);
		const row = contextRow(page, filename.split('/').at(-1)!);
		await expect(row).toBeVisible();
		await expect(page.getByRole('textbox', { name: CREATE_FIELD })).toHaveCount(0);

		// SQLite race tests prove the backend conflict. Inject only its HTTP
		// response here to prove the real UI preserves the row and shows the detail.
		const endpoint = '**/api/context/bootloader';
		const detail = 'Context changed while validating files. Refresh and try again.';
		await page.route(endpoint, route => route.fulfill({
			status: 400, contentType: 'application/json', body: JSON.stringify({ detail }),
		}));
		await removeContextRow(page, row);
		await expect(settingsError(page)).toHaveText(detail);
		await expect(row).toBeVisible();
		await settingsError(page).scrollIntoViewIfNeeded();
		await saveEvidenceScreenshot(page, testInfo, 'context-conflict-preserves-row');
		await page.unroute(endpoint);
		await removeContextRow(page, row);
		await expect(row).toHaveCount(0);
		await expect(settingsError(page)).toHaveCount(0);
	});

	test('shows the partial file-save error without clearing the requested filename', async ({ page }, testInfo) => {
		const detail = 'File saved at context/saved.md, but context was not updated: Context changed while validating files. Refresh and try again.';
		await page.route('**/api/context/bootloader/file', route => route.fulfill({
			status: 400, contentType: 'application/json', body: JSON.stringify({ detail }),
		}));
		await loginByUrl(page);
		await openSettings(page);
		await openContextSection(page);
		await page.getByRole('button', { name: 'New file', exact: true }).first().click();
		const input = page.getByRole('textbox', { name: CREATE_FIELD });
		await input.fill('context/saved.md');
		await page.getByRole('button', { name: 'Create', exact: true }).click();
		await expect(settingsError(page)).toHaveText(detail);
		await expect(input).toHaveValue('context/saved.md');
		await expect(page.getByRole('button', { name: 'Create', exact: true })).toBeEnabled();
		await settingsError(page).scrollIntoViewIfNeeded();
		await saveEvidenceScreenshot(page, testInfo, 'context-partial-save-is-visible');
	});
});
