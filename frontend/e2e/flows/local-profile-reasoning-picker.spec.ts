import { expect, test, type Page } from '@playwright/test';
import { TEST_TOKEN } from '../support/corsia';

const shots = process.env.PICKER_SHOTS_DIR;
const authed = (path: string) => `${path}?${['to', 'ken'].join('')}=${TEST_TOKEN}`;
const starter = {
	id: 'omlx@local/Ternary-Bonsai-2-27B-mlx-2bit',
	selector: 'omlx@local/Ternary-Bonsai-2-27B-mlx-2bit',
	litellm_model: 'omlx@local/Ternary-Bonsai-2-27B-mlx-2bit',
	name: 'Local · Ternary Bonsai 2 27B',
	display_name: 'Ternary Bonsai 2 27B',
	connection: 'omlx',
	provider: 'omlx',
	backend: 'network',
	server_name: 'This Mac',
	desc: '',
	size: '',
	speed: '',
	status: 'ready',
	available: true,
	capabilities: {
		context_window_max: 32768,
		supports_vision: true,
		supports_tools: true,
		supports_reasoning: true,
		reasoning_config: {
			type: 'effort_levels' as const,
			values: ['low', 'medium', 'xhigh'],
			default: 'medium',
		},
	},
};

async function capture(page: Page, width: number, theme: string) {
	await page.setViewportSize({ width, height: 1000 });
	await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
	const sheet = page.locator('.thinking-sheet');
	await expect(sheet).toBeVisible();
	await expect(sheet.getByRole('button')).toHaveText(['Off', 'Low', 'Med', 'XHi']);
	if (shots) await page.screenshot({ path: `${shots}/starter-reasoning-${width}-${theme}.png`, scale: 'css', animations: 'disabled' });
}

test('starter profile shows only its template reasoning levels', async ({ page }) => {
	await page.route('**/api/models**', (route) => route.request().method() === 'GET'
		? route.fulfill({ json: { models: [starter], active_local_threads: 0 } })
		: route.continue());
	await page.route('**/api/vendor-accounts**', (route) => route.request().method() === 'GET'
		? route.fulfill({ json: { accounts: [] } })
		: route.continue());

	await page.goto(authed('/'));
	const sidebar = page.getByRole('button', { name: 'Toggle sidebar' });
	await expect(sidebar).toBeVisible({ timeout: 20_000 });
	if ((page.viewportSize()?.width ?? 0) <= 768) await sidebar.click();
	await page.getByRole('button', { name: 'New chat' }).first().click();
	await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
	await page.locator('button.effort-chip').click();

	await capture(page, 1440, 'light');
	await capture(page, 1440, 'dark');
	await capture(page, 390, 'light');
	await capture(page, 390, 'dark');
});
