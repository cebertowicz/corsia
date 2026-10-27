import { test } from '../support/test';
import { expect } from '@playwright/test';
import { loginByUrl, openSettings, openSettingsSection } from '../support/corsia';

const model = { id: 'codex/test', harness: 'codex', available: true, enabled: true, display_name: 'Test model' };

test('shows model warning and checkpoint copy', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	await page.route('**/api/settings/models?**', route => route.fulfill({ json: { models: [model] } }));
	await page.route('**/api/settings/providers?**', route => route.fulfill({ json: { providers: [] } }));
	await page.route('**/api/settings/network-servers?**', route => route.fulfill({ json: { servers: [] } }));
	await page.route('**/api/vendor-accounts?**', route => route.fulfill({ json: { accounts: [] } }));
	await page.route('**/api/models**', route => {
		const pathname = new URL(route.request().url()).pathname;
		if (pathname.endsWith('/profile')) return route.fulfill({ json: { context_window: null, context_window_override: null } });
		if (pathname !== '/api/models') return route.continue();
		return route.fulfill({ json: { models: [{
			id: 'codex/test', selector: 'codex/test', litellm_model: 'codex/test', name: 'Test model', display_name: 'Test model',
			connection: 'codex', provider: 'codex', backend: 'codex', status: 'unavailable', status_reason: 'connection_unavailable',
			available: true, desc: '', size: '', speed: '', capabilities: {},
		}], active_local_threads: 0 } });
	});
	for (const width of [1920, 393]) {
		await page.setViewportSize({ width, height: 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		await loginByUrl(page, { waitForComposer: false });
		await openSettings(page);
		await openSettingsSection(page, 'Providers');
		const card = page.locator('[data-provider-card="codex"]');
		await card.locator('.pr-expand').click();
		const row = card.locator('.mr').filter({ hasText: 'Test model' });
		await expect(row.getByText('Not shown in the model list: Codex isn’t connected.')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`model-warning-${width}.png`), fullPage: true, animations: 'disabled' });
		await row.getByRole('button', { name: 'Info' }).click();
		const dialog = page.getByRole('dialog').last();
		await expect(dialog.getByText('The provider reports no context window, so set one to use this model. CORSIA checkpoints a chat before it outgrows it.')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`checkpoint-window-${width}.png`), fullPage: true, animations: 'disabled' });
	}
});
