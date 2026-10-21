import { once } from 'node:events';
import { createServer } from 'node:http';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { loginByUrl, openSettings, openSettingsSection, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';

test('@flow:settings @surface:settings network registration shows its endpoint and removes the provider', async ({ page, request }, testInfo) => {
	// Only the external daemon is fake. The UI, HTTP routes and SQLite writes are real.
	const payloads: Record<string, unknown> = {
		'/health': { status: 'ok' },
		'/v1/models': { data: [] },
		'/v1/models/status': { models: [] },
		'/api/status': { status: 'ok', models_loaded: 0, loaded_models: [] },
		'/api/models': { models: [] },
	};
	const daemon = createServer((req, res) => {
		const payload = payloads[req.url ?? ''];
		res.writeHead(payload === undefined ? 404 : 200, { 'Content-Type': 'application/json' });
		res.end(JSON.stringify(payload ?? { error: 'No such test endpoint' }));
	});
	daemon.listen(0, '127.0.0.1');
	await once(daemon, 'listening');
	const bound = daemon.address();
	if (!bound || typeof bound === 'string') throw new Error('Missing isolated test port');
	const address = `127.0.0.1:${bound.port}`;
	const api = `/api/settings/network-servers?token=${encodeURIComponent(TEST_TOKEN)}`;
	let serverId: string | undefined;
	try {
		await loginByUrl(page, { waitForComposer: false });
		await openSettings(page);
		await openSettingsSection(page, 'Providers'); // the phone opens Settings on its section list
		// The header button exists whether or not an earlier spec left a provider; the empty state's own button does not.
		await page.getByRole('button', { name: 'Add provider', exact: true }).click();
		await page.getByRole('button', { name: /^oMLX on another Mac/ }).click();
		const dialog = page.getByRole('dialog').last();
		await dialog.getByPlaceholder('100.64.1.5  ·  macbook-m4.local').fill('127.0.0.1');
		await dialog.getByPlaceholder('8000', { exact: true }).fill(String(bound.port));
		await dialog.getByPlaceholder('(optional — uses the address)').fill('G3 disposable provider');
		const [added] = await Promise.all([
			page.waitForResponse(response => response.request().method() === 'POST'
				&& new URL(response.url()).pathname === '/api/settings/network-servers'),
			dialog.getByRole('button', { name: 'Test & add', exact: true }).click(),
		]);
		expect(added.status()).toBe(200);
		const receipt = await added.json();
		serverId = receipt.server_id;
		expect(receipt).toEqual({ ok: true, server_id: expect.any(String), status: 'online' });

		const provider = page.locator('.pr').filter({ has: page.getByText(`port ${bound.port}`, { exact: true }) });
		await expect(provider).toHaveCount(1);
		await expect(provider.locator('.dot.online')).toHaveCount(1);
		const listed = await request.get(api);
		expect(listed.ok()).toBe(true);
		const stored = (await listed.json()).servers.find((row: { id: string }) => row.id === serverId);
		expect(stored).toMatchObject({ instance_name: 'G3 disposable provider', status: 'online', active_host: '127.0.0.1' });
		expect(stored.endpoints).toEqual([expect.objectContaining({ host: '127.0.0.1', kind: 'local' })]);
		await provider.getByRole('button', { name: 'Settings', exact: true }).click();
		await page.getByRole('button', { name: 'Endpoints', exact: true }).click();
		await expect(page.getByRole('dialog').last().getByText(address, { exact: true })).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'registered-provider-endpoint');

		await page.getByRole('button', { name: 'Overview', exact: true }).click();
		// ProviderSettingsModal.svelte confirms removal in-app (an alertdialog), not with window.confirm.
		await page.getByRole('button', { name: 'Remove provider…', exact: true }).click();
		await page.getByRole('alertdialog').getByRole('button', { name: 'Remove', exact: true }).click();
		await expect(provider).toHaveCount(0);
		const remaining = await request.get(api);
		expect(remaining.ok()).toBe(true);
		expect((await remaining.json()).servers.some((row: { id: string }) => row.id === serverId)).toBe(false);
		serverId = undefined;
	} finally {
		try {
			if (serverId) {
				const removed = await request.delete(`/api/settings/network-servers/${serverId}?token=${encodeURIComponent(TEST_TOKEN)}`);
				expect(removed.ok()).toBe(true);
			}
		} finally {
			daemon.closeAllConnections();
			await new Promise<void>((resolve, reject) => daemon.close(error => error ? reject(error) : resolve()));
		}
	}
});
