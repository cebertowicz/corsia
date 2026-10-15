import { expect, test, type Page } from '@playwright/test';

const assets = '../../../../../shared/onboarding/2026-10-15-replace-consent.assets';

type State = 'ask' | 'progress' | 'not-now';

async function routeReplacement(page: Page, state: State) {
	let requests = 0;
	await page.routeWebSocket((url) => url.pathname === '/ws', () => {});
	await page.route('**/api/auth/status', (route) => route.fulfill({ json: { has_users: false } }));
	await page.route('**/api/setup/status', (route) => route.fulfill({ json: {
		apple_silicon: true, macos_supported: true, local_eligible: true,
		model_name: 'Qwen 3.8', model_size_gb: 30, work_folder: '~/CORSIA', existing_omlx: true,
	} }));
	await page.route('**/api/setup/local-download', (route) => {
		requests += 1;
		if (state === 'progress' && requests > 1) return route.fulfill({ json: { ok: true, in_progress: true } });
		return route.fulfill({ status: 409, json: { code: 'replace_consent_required' } });
	});
	await page.route('**/api/providers/sources/recheck', (route) => route.fulfill({ json: { sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup', ...(state === 'progress' ? { setup_downloading: true, setup_stage: 'Reinstalling oMLX', download_percent: 42 } : {}) }] } }));
	await page.route('**/api/providers/sources', (route) => route.fulfill({ json: { sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup' }] } }));
}

for (const [name, width, height, dark] of [
	['light-1280x900', 1280, 900, false],
	['dark-800x600', 800, 600, true],
] as const) {
	for (const state of ['ask', 'progress', 'not-now'] as const) {
		test(`${state} ${name}`, async ({ page }) => {
			await page.setViewportSize({ width, height });
			await routeReplacement(page, state);
			await page.goto('/');
			if (dark) await page.emulateMedia({ colorScheme: 'dark' });
			await page.getByRole('button', { name: 'Get started' }).click();
			await page.getByRole('button', { name: 'Use it' }).click();
			await expect(page.getByText("The oMLX on this computer doesn't start")).toBeVisible();
			if (state === 'progress') await page.getByRole('button', { name: 'Reinstall' }).click();
			if (state === 'not-now') await page.getByRole('button', { name: 'Not now' }).click();
			await page.screenshot({ path: `${assets}/${state}-${name}.png`, fullPage: true, scale: 'css' });
		});
	}
}
