import { createServer } from 'node:http';
import { test, expect } from '../support/test';
import { TEST_SESSION_TOKEN } from '../support/corsia';

test('@surface:first-run setup counts a declared model only when selectable', async ({ page }) => {
	const server = createServer((req, res) => {
		res.setHeader('Content-Type', 'application/json');
		res.end(JSON.stringify({ data: [] }));
	});
	await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
	const address = server.address();
	if (!address || typeof address === 'string') throw new Error('No test endpoint');
	try {
		await page.route('**/api/setup/progress', (route) => route.fulfill({ json: { setup_completed: false, status: { local_started: true, local_eligible: false, macos_supported: null } } }));
		await page.goto(`/?token=${TEST_SESSION_TOKEN}`);
		await expect(page.locator('[data-setup-page="models"]')).toBeVisible();
		await page.getByRole('button', { name: 'Add provider' }).click();
		await page.getByRole('button', { name: /Other provider by address/ }).click();
		await page.getByPlaceholder('https://host.example.com/compatible-mode/v1').fill(`http://127.0.0.1:${address.port}/v1`);
		await page.getByRole('button', { name: 'More options' }).click();
		await page.getByPlaceholder('model-one, model-two').fill('test-model');
		const registration = page.waitForResponse((response) => response.url().includes('/api/settings/network-servers') && response.request().method() === 'POST');
		await page.getByRole('button', { name: 'Test & add' }).click();
		const receipt = await (await registration).json();
		expect(receipt.models).toBe(0);
		let modelId = '';
		await expect.poll(async () => {
			const rows = await page.evaluate(async () => (await (await fetch('/api/models')).json()).models);
			const row = rows.find((item: { litellm_model?: string }) => item.litellm_model?.startsWith(`external@${receipt.server_id}/`));
			modelId = row?.id ?? '';
			return row?.status_reason;
		}).toBe('missing_context_window');
		await expect(page.getByRole('button', { name: 'Start using CORSIA' })).toBeDisabled();
		// Model details is outside setup. Use its existing API to establish a selectable row.
		const saved = await page.request.patch(`/api/models/${encodeURIComponent(modelId)}/profile`, {
			headers: { Authorization: `Bearer ${TEST_SESSION_TOKEN}` }, data: { context_window: 8192 },
		});
		expect(saved.ok()).toBe(true);
		await expect.poll(async () => {
			const rows = await page.evaluate(async () => (await (await fetch('/api/models')).json()).models);
			return rows.find((item: { id: string }) => item.id === modelId)?.status;
		}, { timeout: 15000 }).toBe('ready');
		await expect(page.getByRole('button', { name: 'Start using CORSIA' })).toBeEnabled({ timeout: 15000 });
		await expect(page.getByText(/127\.0\.0\.1.* · added/)).toBeVisible();
	} finally {
		await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
	}
});
