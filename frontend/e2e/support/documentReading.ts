import { expect, type Page } from '@playwright/test';

/** Explicitly enter document typing when its opening uses Reading; mouse openings already edit. */
export async function enterDocumentEdit(page: Page) {
	const edit = page.locator('.doc-panel .p-edit');
	if (!await edit.count()) return;
	if (await page.getByRole('button', { name: 'Done', exact: true }).isVisible()) return;
	await page.locator('[data-document-details]').click();
	await expect(edit).toBeVisible();
	await expect(edit).toBeEnabled();
	await edit.click();
	await expect(page.getByRole('button', { name: 'Done', exact: true })).toBeVisible();
}
