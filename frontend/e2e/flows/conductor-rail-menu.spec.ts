import { test } from '../support/test';
import { expect } from '@playwright/test';
import { loginByUrl, saveEvidenceScreenshot } from '../support/corsia';

/**
 * The CORSIA / Conductor row's right-click menu is never empty.
 *
 * Mark read/unread is offered only once the thread has been opened and no "Needs you" or Failed word
 * outranks the dot, so a fresh row's menu holds Agent settings alone. Rename and Deactivate stay with the
 * Agent rows (the row has no rename field and deactivating it would leave the scope without its agent).
 */
test.describe('@flow:conductor-rail-menu right-click on the CORSIA row', () => {
	test('opens a menu with Agent settings, which opens that agent\'s settings', async ({ page }, testInfo) => {
		test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'right-click is a desktop gesture');
		await loginByUrl(page);
		const row = page.locator('.scoped-item.conductor');
		await expect(row).toBeVisible();

		await row.click({ button: 'right' });
		const menu = page.locator('.context-menu');
		await expect(menu).toBeVisible();
		// A row that has never been opened has no seen state to mark, and nothing here renames or deactivates it.
		await expect(menu.locator(':scope > button')).toHaveText(['Agent settings']);
		await saveEvidenceScreenshot(page, testInfo, 'conductor-rail-menu');

		await menu.getByRole('button', { name: 'Agent settings' }).click();
		await expect(menu).toHaveCount(0);
		await expect(page.locator('.ts .sec-cap', { hasText: 'Chat skills' })).toBeVisible();
	});
});
