import { expect, test } from '@playwright/test';
import { loginByUrl, openSettings, saveEvidenceScreenshot } from './support/corsia';

for (const viewport of [
	{ name: '1440', width: 1440, height: 900 },
	{ name: '393', width: 393, height: 852 },
]) {
	test(`Connect an app flow renders every step at ${viewport.name} light`, async ({ page }, testInfo) => {
		await page.emulateMedia({ colorScheme: 'light' });
		await page.setViewportSize({ width: viewport.width, height: viewport.height });
		await page.route('**/api/mcp/servers**', (route) => route.fulfill({ json: { servers: [] } }));
		await page.route('**/api/projects**', (route) => route.fulfill({ json: { projects: [{ id: 'garden', name: 'Garden plan', status: 'active', accent_color: null }] } }));
		await loginByUrl(page, { waitForComposer: false });
		await openSettings(page);
		await page.getByRole('button', { name: 'System' }).first().click();
		await page.getByRole('button', { name: 'Connect an app', exact: true }).click();
		const dialog = page.getByRole('dialog');
		await expect(dialog.getByRole('heading', { name: 'What' })).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, `connect-app-${viewport.name}-step-1`);
		await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
		await expect(dialog.getByRole('heading', { name: 'Set it up' })).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, `connect-app-${viewport.name}-step-2`);
		await dialog.getByLabel('Name').fill('Files');
		await dialog.getByLabel('What it runs').fill('npx -y @example/app');
		await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
		await expect(dialog.getByRole('heading', { name: 'Where it works' })).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, `connect-app-${viewport.name}-step-3`);
	});
}
