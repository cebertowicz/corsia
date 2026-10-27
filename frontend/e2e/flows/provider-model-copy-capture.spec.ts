import { test } from '../support/test';
import { expect } from '@playwright/test';
import { loginByUrl, openSettings, openSettingsSection } from '../support/corsia';

const model = { id: 'codex/test', harness: 'codex', available: true, enabled: true, display_name: 'Test model' };
const missing = { ...model, id: 'codex/missing', display_name: 'Missing window' };
const unavailable = { ...model, id: 'codex/unavailable', display_name: 'Unavailable model', provider_context_length: 128000 };

test('shows model warning and checkpoint copy', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	await page.route('**/api/settings/models?**', route => route.fulfill({ json: { models: [model, missing, unavailable] } }));
	await page.route('**/api/settings/providers?**', route => route.fulfill({ json: { providers: [] } }));
	await page.route('**/api/settings/network-servers?**', route => route.fulfill({ json: { servers: [] } }));
	await page.route('**/api/vendor-accounts?**', route => route.fulfill({ json: { accounts: [] } }));
	await page.route('**/api/models**', route => {
		const pathname = new URL(route.request().url()).pathname;
		if (pathname.endsWith('/profile')) return route.fulfill({ json: { context_window: pathname.includes('unavailable') ? 128000 : null, context_window_override: null } });
		if (pathname !== '/api/models') return route.continue();
		return route.fulfill({ json: { models: [{
			id: 'codex/test', selector: 'codex/test', litellm_model: 'codex/test', name: 'Test model', display_name: 'Test model',
			connection: 'codex', provider: 'codex', backend: 'codex', status: 'unavailable', status_reason: 'connection_unavailable',
			available: true, desc: '', size: '', speed: '', capabilities: {},
		}, ...[missing, unavailable].map(item => ({
			id: item.id, selector: item.id, litellm_model: item.id, name: item.display_name, display_name: item.display_name,
			connection: 'codex', provider: 'codex', backend: 'codex', status: 'unavailable',
			status_reason: item.id === missing.id ? 'missing_context_window' : 'other',
			available: true, desc: '', size: '', speed: '', capabilities: {},
		}))], active_local_threads: 0 } });
	});
	for (const width of [1920, 393]) {
		await page.setViewportSize({ width, height: 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		await loginByUrl(page, { waitForComposer: false });
		await openSettings(page);
		await openSettingsSection(page, 'Providers');
		const card = page.locator('[data-provider-card="codex"]');
		await expect(page.getByText('Run by the CLI on this computer')).toBeVisible();
		await card.locator('.pr-expand').click();
		const row = card.locator('.mr').filter({ hasText: 'Test model' });
		await expect(row.getByText('Not shown in the model list: Codex isn’t connected.')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`model-warning-${width}.png`), fullPage: true, animations: 'disabled' });
		await expect(card.getByText('Not shown in the model list: its context window is unknown.')).toBeVisible();
		await expect(card.getByText('Not shown in the model list: this model is unavailable right now.')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`model-other-warnings-${width}.png`), fullPage: true, animations: 'disabled' });
		await row.getByRole('button', { name: 'Info' }).click();
		const dialog = page.getByRole('dialog').last();
		await expect(dialog.getByText('The provider reports no context window, so set one to use this model. CORSIA checkpoints a chat before it outgrows it.')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`checkpoint-window-${width}.png`), fullPage: true, animations: 'disabled' });
		await dialog.getByRole('button', { name: 'Close' }).click();
		await card.locator('.mr').filter({ hasText: 'Unavailable model' }).getByRole('button', { name: 'Info' }).click();
		await expect(page.getByText("CORSIA checkpoints a chat before it outgrows this window. Leave empty to use the provider's 128,000 tokens.")).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`checkpoint-known-${width}.png`), fullPage: true, animations: 'disabled' });
	}
});

test('shows the external model missing-window warning', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	const server = { id: 'copy-external', provider: 'external', instance_id: 'external', machine_id: 'external', instance_name: 'External', kind: 'external', port: 8000, status: 'online', source: 'manual', endpoints: [{ id: 'external-endpoint', server_id: 'copy-external', host: '127.0.0.1', kind: 'local' }], active_host: '127.0.0.1' };
	const externalId = 'external/copy-model';
	await page.route('**/api/settings/providers?**', route => route.fulfill({ json: { providers: [{ name: 'external', label: 'External', type: 'network', enabled: true }] } }));
	await page.route('**/api/settings/network-servers?**', route => route.fulfill({ json: { servers: [server] } }));
	await page.route('**/api/settings/network-servers/copy-external/models?**', route => route.fulfill({ json: { loaded: [{ id: 'copy-model', db_id: externalId, display_name: 'External model' }], available: [] } }));
	await page.route('**/api/models**', route => route.fulfill({ json: { models: [{ id: externalId, selector: externalId, litellm_model: externalId, name: 'External model', display_name: 'External model', backend: 'network', connection: 'external', provider: 'external', status: 'unavailable', status_reason: 'missing_context_window', available: true, capabilities: {} }], active_local_threads: 0 } }));
	for (const width of [1920, 393]) {
		await page.setViewportSize({ width, height: 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		await loginByUrl(page, { waitForComposer: false });
		await openSettings(page);
		await openSettingsSection(page, 'Providers');
		const card = page.locator('[data-server-card="copy-external"]');
		await card.locator('.pr-expand').click();
		await expect(card.getByText('Not shown in the model list: set its context window in this model’s details.')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`external-window-${width}.png`), fullPage: true, animations: 'disabled' });
	}
});
