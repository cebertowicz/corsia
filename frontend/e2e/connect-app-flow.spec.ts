import { expect, test, type Route } from '@playwright/test';
import { loginByUrl, openSettings, saveEvidenceScreenshot } from './support/corsia';

const viewports = [
	{ name: '1440', width: 1440, height: 900 },
	{ name: '393', width: 393, height: 852 },
] as const;
const themes = ['light', 'dark'] as const;

for (const viewport of viewports) {
	for (const theme of themes) {
		test(`Connect an app captures the reviewed states at ${viewport.name} ${theme}`, async ({ page }, testInfo) => {
			let pendingConnect: Route | null = null;
			await page.emulateMedia({ colorScheme: theme });
			await page.setViewportSize({ width: viewport.width, height: viewport.height });
			await page.route('**/api/mcp/servers**', async (route) => {
				if (route.request().method() === 'POST') {
					pendingConnect = route;
					return;
				}
				await route.fulfill({ json: { servers: [] } });
			});
			await page.route('**/api/google/connect**', (route) => route.fulfill({ json: { url: 'https://accounts.google.example/authorize' } }));
			await page.route('**/api/projects**', (route) => route.fulfill({ json: { projects: [{ id: 'garden', name: 'Garden plan', status: 'active', accent_color: null }] } }));
			await loginByUrl(page, { waitForComposer: false });
			await openSettings(page);
			await page.getByRole('button', { name: 'System' }).first().click();
			await page.getByRole('button', { name: 'Connect an app', exact: true }).click();
			const dialog = page.getByRole('dialog');
			await expect(dialog.getByRole('heading', { name: 'What' })).toBeVisible();
			// Modal.svelte's open fly transition runs for 380ms; capture the settled surface.
			await page.waitForTimeout(400);
			await saveEvidenceScreenshot(page, testInfo, `what-${viewport.name}-${theme}`);

			await dialog.getByLabel('Google (email and calendar)').click();
			await expect(dialog.getByText('Opens a chat with CORSIA to sign in.')).toBeVisible();
			await saveEvidenceScreenshot(page, testInfo, `google-choice-${viewport.name}-${theme}`);
			await dialog.getByLabel('An app that runs on this Mac').click();
			await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
			await expect(dialog.getByRole('heading', { name: 'Set it up' })).toBeVisible();
			await saveEvidenceScreenshot(page, testInfo, `mac-setup-${viewport.name}-${theme}`);

			if (viewport.name === '393') {
				await dialog.getByLabel('Name').fill('An exceptionally long connected app name that must remain readable');
				await dialog.getByLabel('What it runs').fill('npx -y @modelcontextprotocol/an-exceptionally-long-server-command-with-many-arguments --project garden-plan --verbose --safe-mode');
				await saveEvidenceScreenshot(page, testInfo, `long-mac-setup-${viewport.name}-${theme}`);
				await dialog.getByLabel('Name').fill('Files');
				await dialog.getByLabel('What it runs').fill('npx -y @example/app');
			}

			await dialog.getByRole('button', { name: 'Back', exact: true }).click();
			await dialog.getByLabel('An app at a web address').click();
			await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
			await expect(dialog.getByLabel('Web address')).toBeVisible();
			await saveEvidenceScreenshot(page, testInfo, `web-setup-${viewport.name}-${theme}`);
			await dialog.getByRole('button', { name: 'Back', exact: true }).click();
			await dialog.getByLabel('An app that runs on this Mac').click();
			await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
			await dialog.getByLabel('Name').fill('Files');
			await dialog.getByLabel('What it runs').fill('npx -y @example/app');
			await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
			await expect(dialog.getByRole('heading', { name: 'Where it works' })).toBeVisible();
			await saveEvidenceScreenshot(page, testInfo, `all-projects-${viewport.name}-${theme}`);

			await dialog.getByLabel('Select individually').click();
			await expect(dialog.getByText('Garden plan')).toBeVisible();
			await saveEvidenceScreenshot(page, testInfo, `select-individually-${viewport.name}-${theme}`);
			await dialog.getByLabel('All projects').click();
			await dialog.getByRole('button', { name: 'Connect', exact: true }).click();
			await expect(dialog.getByRole('button', { name: 'Connect', exact: true })).toBeDisabled();
			await expect.poll(() => pendingConnect).not.toBeNull();
			await saveEvidenceScreenshot(page, testInfo, `mac-checking-${viewport.name}-${theme}`);
			await pendingConnect!.fulfill({ status: 400, json: { detail: 'The app did not answer' } });
			await expect(dialog.getByText('The app did not answer')).toBeVisible();
			await saveEvidenceScreenshot(page, testInfo, `mac-no-answer-${viewport.name}-${theme}`);

			await dialog.getByRole('button', { name: 'Back', exact: true }).click();
			await dialog.getByRole('button', { name: 'Back', exact: true }).click();
			await dialog.getByLabel('Google (email and calendar)').click();
			const popup = page.waitForEvent('popup');
			await dialog.getByRole('button', { name: 'Continue', exact: true }).click();
			await (await popup).close();
			await expect(dialog).toBeHidden();
			await saveEvidenceScreenshot(page, testInfo, `google-after-continue-${viewport.name}-${theme}`);
		});
	}
}
