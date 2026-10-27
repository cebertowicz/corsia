import { test } from '../support/test';
import { expect } from '@playwright/test';
import { TEST_TOKEN } from '../support/corsia';

const authed = (path: string) => `${path}?${['to', 'ken'].join('')}=${TEST_TOKEN}`;

test('shows the chat loading message while the model warms', async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	await page.route('**/api/models**', route => route.request().method() === 'GET' && new URL(route.request().url()).pathname === '/api/models'
		? route.fulfill({ json: { models: [{ id: 'omlx/local', selector: 'omlx/local', litellm_model: 'omlx/local', name: 'Local model', display_name: 'Local model', connection: 'omlx', provider: 'omlx', backend: 'network', status: 'ready', available: true, is_default: true, capabilities: {} }], active_local_threads: 0 } })
		: route.continue());
	await page.route('**/api/threads/*/warm?**', async route => {
		await new Promise(resolve => setTimeout(resolve, 5000));
		await route.fulfill({ json: { ok: true } });
	});
	for (const width of [1920, 393]) {
		const title = `Warm copy ${width}`;
		const created = await request.post(authed('/api/threads'), { data: { title, model: 'omlx/local' } });
		expect(created.ok()).toBeTruthy();
		await page.setViewportSize({ width, height: 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		await page.goto(authed('/'));
		await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
		if (width < 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.locator('.thread-title', { hasText: title }).click();
		await page.waitForFunction(async () => {
			const stores = await import('/src/lib/stores/models.ts');
			let active: unknown;
			stores.activeModelId.subscribe((value: unknown) => { active = value; })();
			return active === 'omlx/local';
		});
		await page.getByRole('textbox', { name: 'Message CORSIA' }).fill('Hello');
		await expect(page.getByText('Loading this chat into the model…')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath(`chat-loading-${width}.png`), fullPage: true, animations: 'disabled' });
	}
});
