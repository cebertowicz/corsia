import { expect, test, type Page } from '@playwright/test';

const assets = '../../../../../shared/onboarding/2026-10-15-replace-consent.assets';

type State = 'ask' | 'progress' | 'not-now' | 'signed-in-not-now';
type Theme = 'light' | 'dark';

async function routeReplacement(page: Page, state: State) {
	let unsignedRequests = 0;
	let signedRequests = 0;
	await page.routeWebSocket((url) => url.pathname === '/ws', () => {});
	await page.route('**/api/auth/status', (route) => route.fulfill({ json: { has_users: false } }));
	await page.route('**/api/setup/status', (route) => route.fulfill({ json: {
		apple_silicon: true, macos_supported: true, local_eligible: true,
		model_name: 'Qwen 3.8', model_size_gb: 30, work_folder: '~/CORSIA', existing_omlx: true,
	} }));
	await page.route('**/api/setup/local-download', (route) => {
		unsignedRequests += 1;
		if (state === 'progress' && unsignedRequests > 1) return route.fulfill({ json: { ok: true, in_progress: true } });
		return route.fulfill({ status: 409, json: { code: 'replace_consent_required' } });
	});
	await page.route('**/api/providers/local/setup', (route) => {
		signedRequests += 1;
		return route.fulfill({ status: 409, json: { detail: { code: 'replace_consent_required' }, request: signedRequests } });
	});
	await page.route('**/api/providers/sources/recheck', (route) => route.fulfill({ json: { sources: [
		{ source: 'omlx', name: 'Local model', state: 'needs_setup' },
		{ source: 'claude-code', name: 'Claude Code', state: 'needs_auth' },
	] } }));
	await page.route('**/api/providers/sources', (route) => route.fulfill({ json: { sources: [
		{ source: 'omlx', name: 'Local model', state: 'needs_setup' },
		{ source: 'claude-code', name: 'Claude Code', state: 'needs_auth' },
	] } }));
}

async function settleOnHeading(page: Page, title: string) {
	const activeHeading = page.locator('.pane-scene:not([inert]) h1');
	await expect(activeHeading).toHaveCount(1);
	await expect(activeHeading).toHaveText(title);
	await expect(page.getByRole('heading', { name: title })).toHaveCount(1);
	await expect(page.locator('.pane-scene')).toHaveCount(1);
}

async function applyCaptureOverrides(page: Page) {
	await page.addStyleTag({ content: `
		:root {
			--accent-fill: #00769e;
			--accent-on-fill: #ffffff;
			--accent-text: #006d92;
			--accent-line: #00769e;
			--accent-wash: #e0ecf1;
		}
		html[data-theme="dark"] {
			--accent-fill: #3aa9d0;
			--accent-on-fill: #04121a;
			--accent-text: #2e9cc4;
			--accent-line: #2e9cc4;
			--accent-wash: #152026;
		}
		.version-overlay { display: none !important; }
	` });
}

for (const theme of ['light', 'dark'] as const) {
	for (const state of ['ask', 'progress', 'not-now', 'signed-in-not-now'] as const) {
		test(`${state} ${theme} 1440x900`, async ({ page }) => {
			await page.setViewportSize({ width: 1440, height: 900 });
			await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
			if (state === 'signed-in-not-now') await page.addInitScript(() => localStorage.setItem('corsia_token', 'signed-in'));
			await routeReplacement(page, state);
			await page.goto('/');
			await applyCaptureOverrides(page);
			await page.getByRole('button', { name: 'Get started' }).click();
			await settleOnHeading(page, 'Use your oMLX?');
			await page.getByRole('button', { name: 'Use it' }).click();
			await settleOnHeading(page, "The oMLX on this computer doesn't start");
			const replacementCopy = page.getByText('CORSIA can reinstall oMLX and download Qwen 3.8 (30 GB). Reinstalling replaces that oMLX and changes its settings.', { exact: true });
			await expect(replacementCopy).toHaveCount(1);
			await expect(page.locator('.primary-action').getByRole('button', { name: 'Reinstall' })).toBeVisible();
			await expect(page.locator('.consent').getByRole('button', { name: 'Not now' })).toBeVisible();
			await expect(page.getByRole('button', { name: 'Continue' })).toHaveCount(0);

			if (state === 'progress') {
				await page.getByRole('button', { name: 'Reinstall' }).click();
				await settleOnHeading(page, 'Create your account');
				await expect(page.getByLabel('Setup progress').getByText('In the background')).toBeVisible();
				await expect(page.getByLabel('Setup progress').getByText('Qwen 3.8', { exact: true })).toBeVisible();
			}
			if (state === 'not-now') {
				await page.getByRole('button', { name: 'Not now' }).click();
				await settleOnHeading(page, 'Create your account');
			}
			if (state === 'signed-in-not-now') {
				await page.getByRole('button', { name: 'Not now' }).click();
				await settleOnHeading(page, 'Choose an AI model');
				await expect(page.getByText("oMLX doesn't start", { exact: true })).toBeVisible();
				await expect(page.locator('.pane-scene:not([inert]) .sources').getByRole('button', { name: 'Reinstall', exact: true })).toBeVisible();
				await expect(page.getByText('Claude Code', { exact: true })).toBeVisible();
			}
			await page.screenshot({ path: `${assets}/${state}-${theme}-1440x900.png`, animations: 'disabled', scale: 'css' });
		});
	}
}
