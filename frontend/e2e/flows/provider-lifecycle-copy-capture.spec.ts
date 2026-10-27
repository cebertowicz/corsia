import { test } from '../support/test';
import { expect } from '@playwright/test';
import { loginByUrl, openSettings, openSettingsSection } from '../support/corsia';

const server = {
	id: 'omlx-copy', provider: 'omlx', instance_id: 'this-mac', machine_id: 'this-mac',
	instance_name: 'Local model', kind: 'managed', port: 11434, status: 'online', source: 'manual',
	latency_ms: 1, memory_total_mb: 65536, memory_used_mb: 0, gpu_info: null,
	endpoints: [{ id: 'copy-endpoint', server_id: 'omlx-copy', host: '127.0.0.1', kind: 'local', last_ok_at: null, last_latency_ms: 1 }],
	active_host: '127.0.0.1', api_base: null, mgmt_base: null, corsia_omlx: true,
};
const provider = { name: 'omlx', label: 'oMLX', type: 'network', has_key: false, masked_key: null, api_base: null, docs_url: null, models: [], enabled: true };

test('shows model server action fallback copy', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	await page.route('**/api/settings/providers?**', route => route.fulfill({ json: { providers: [provider] } }));
	await page.route('**/api/settings/network-servers?**', route => route.fulfill({ json: { servers: [server] } }));
	await page.route('**/api/settings/network-servers/omlx-copy/models?**', route => route.fulfill({ json: { loaded: [], available: [] } }));
	await page.route('**/api/settings/providers/omlx/status?**', route => route.fulfill({ status: 500, json: {} }));
	await page.route('**/api/settings/network-servers/omlx-copy/lifecycle/**', route => route.fulfill({ json: { ok: false } }));
	for (const width of [1920, 393]) {
		await page.setViewportSize({ width, height: 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		await loginByUrl(page, { waitForComposer: false });
		await openSettings(page);
		await openSettingsSection(page, 'Providers');
		const card = page.locator('[data-server-card="omlx-copy"]');
		await card.getByRole('button', { name: 'Settings', exact: true }).click();
		const dialog = page.getByRole('dialog').last();
		await dialog.getByRole('button', { name: 'Test connection' }).click();
		await expect(dialog.getByText('Couldn’t test the connection. Try again.')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`server-test-${width}.png`), fullPage: true, animations: 'disabled' });
		await dialog.getByRole('button', { name: 'Reload models' }).click();
		await expect(dialog.getByText('Couldn’t refresh the model list. Try again.')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`server-refresh-${width}.png`), fullPage: true, animations: 'disabled' });
		await dialog.getByRole('button', { name: 'Stop', exact: true }).click();
		await expect(dialog.getByText('Couldn’t stop the model server. Try again.')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`server-stop-${width}.png`), fullPage: true, animations: 'disabled' });
	}
});

test('shows model server start fallback copy', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	let succeed = false;
	await page.route('**/api/settings/providers?**', route => route.fulfill({ json: { providers: [provider] } }));
	await page.route('**/api/settings/network-servers?**', route => route.fulfill({ json: { servers: [{ ...server, status: 'offline' }] } }));
	await page.route('**/api/settings/network-servers/omlx-copy/models?**', route => route.fulfill({ json: { loaded: [], available: [] } }));
	await page.route('**/api/settings/network-servers/omlx-copy/lifecycle/start?**', route => route.fulfill({ json: { ok: succeed } }));
	for (const width of [1920, 393]) {
		succeed = false;
		await page.setViewportSize({ width, height: 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		await loginByUrl(page, { waitForComposer: false });
		await openSettings(page);
		await openSettingsSection(page, 'Providers');
		await page.locator('[data-server-card="omlx-copy"]').getByRole('button', { name: 'Settings', exact: true }).click();
		const dialog = page.getByRole('dialog').last();
		await dialog.getByRole('button', { name: 'Start', exact: true }).click();
		await expect(dialog.getByText('Couldn’t start the model server. Try again.')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`server-start-failure-${width}.png`), fullPage: true, animations: 'disabled' });
		succeed = true;
		await dialog.getByRole('button', { name: 'Start', exact: true }).click();
		await expect(dialog.getByText('Model server started.')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`server-start-success-${width}.png`), fullPage: true, animations: 'disabled' });
	}
});
