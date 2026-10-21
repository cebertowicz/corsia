import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { loginByUrl, openSettings } from '../support/corsia';

// Synthetic accounts only: the disposable backend has no vendor CLI, so the
// harness inventory and account list are served here and every write is observed.
const ACCOUNTS = [
	{ id: 'codex-default', provider: 'codex', name: 'Default', is_default: true, state: 'legacy', revision: 0,
		identity: {}, label: { name: 'Plus account 1', email: null, text: 'Plus account 1' }, duplicate_of: null, allow_overage: false,
		usage: { state: 'limited', usable: false, reason: 'five_hour_limit', resets_at: null, windows: [], checking: false, check: { error: 'offline' } } },
	{ id: 'codex-work', provider: 'codex', name: 'Work', is_default: false, state: 'signed_in', revision: 2,
		identity: { email: 'work@example.test' }, label: { name: 'Work', email: 'work@example.test', text: 'Work · work@example.test' }, duplicate_of: null, allow_overage: true },
	{ id: 'claude-default', provider: 'claude-code', name: 'Default', is_default: true, state: 'legacy', revision: 0,
		identity: {}, label: { name: 'account 1', email: null, text: 'account 1' }, duplicate_of: null, allow_overage: false,
		usage: { state: 'limited', usable: false, reason: 'five_hour_limit', resets_at: null, windows: [], checking: false, check: { error: 'offline' } } },
];

// The Claude Code and Codex cards list the models the picker catalog offers that Settings also
// holds (harnessMembership.ts settingsHarnessModelIds), so the picker catalog is served with them.
const catalogRow = (row: { id: string; harness: string; display_name: string }) => ({
	id: row.id, selector: row.id, litellm_model: row.id, name: row.display_name, display_name: row.display_name,
	connection: row.harness, provider: row.harness, backend: row.harness === 'codex' ? 'codex' : 'cc',
	status: 'ready', available: true, desc: '', size: '', speed: '',
	capabilities: { context_window_max: 200000, supports_vision: true, supports_tools: true, supports_reasoning: false, reasoning_config: null },
});

const MODELS = [
	{ id: 'codex/test', harness: 'codex', available: true, enabled: true, display_name: 'Codex test' },
	{ id: 'claude-code/test', harness: 'claude-code', available: true, enabled: true, display_name: 'Claude test' },
];

async function serveAccounts(page: Page, writes: { url: string; body: unknown }[]) {
	const accounts = ACCOUNTS.map((account) => ({ ...account }));
	await page.route('**/api/settings/models?**', (route) => route.fulfill({ json: { models: MODELS } }));
	await page.route('**/api/settings/providers?**', (route) => route.fulfill({ json: { providers: [] } }));
	await page.route('**/api/settings/network-servers?**', (route) => route.fulfill({ json: { servers: [] } }));
	await page.route('**/api/models**', (route) => new URL(route.request().url()).pathname === '/api/models' && route.request().method() === 'GET'
		? route.fulfill({ json: { models: MODELS.map(catalogRow), active_local_threads: 0 } }) : route.continue());
	await page.route('**/api/vendor-accounts?**', (route) => route.fulfill({ json: { accounts } }));
	await page.route('**/api/vendor-accounts/*/usage?**', async (route) => {
		const id = decodeURIComponent(new URL(route.request().url()).pathname.split('/')[3]);
		const body = route.request().postDataJSON() as { allow_overage: boolean };
		writes.push({ url: new URL(route.request().url()).pathname, body });
		const account = accounts.find((row) => row.id === id)!;
		account.allow_overage = body.allow_overage;
		await route.fulfill({ json: account });
	});
}

async function openHarnessAccounts(page: Page, provider: string, account: string) {
	await loginByUrl(page, { waitForComposer: false });
	await openSettings(page);
	await page.getByRole('button', { name: 'Providers' }).click();
	const card = page.locator(`[data-provider-card="${provider}"]`);
	await card.getByRole('button', { name: 'Settings' }).click();
	await expect(page.getByText(account, { exact: true })).toBeVisible();
}

test.describe('@surface:providers accounts live in settings modal', () => {
	test('the harness gear opens canonical account rows and a flip writes only that account', async ({ page }, testInfo) => {
		// Four viewport/theme evidence passes plus the final write exceed mock mode's suite-wide 30s ceiling.
		test.setTimeout(60_000);
		const writes: { url: string; body: unknown }[] = [];
		await serveAccounts(page, writes);
		for (const [name, width, colorScheme] of [
			['accounts-overage-desktop-light', 1440, 'light'], ['accounts-overage-desktop-dark', 1440, 'dark'],
			['accounts-overage-393-light', 393, 'light'], ['accounts-overage-393-dark', 393, 'dark'],
		] as const) {
			await page.setViewportSize({ width, height: 1100 });
			await page.emulateMedia({ colorScheme });
			await openHarnessAccounts(page, 'codex', 'Plus account 1');
			await expect(page.getByText('Plus account 1')).toBeVisible();
			await expect(page.getByText('Usage limit reached · reset time unknown')).toBeVisible();
			await expect(page.getByText("Couldn't verify account")).toBeVisible();
			await expect(page.getByText('Work · work@example.test')).toBeVisible();
			await expect(page.getByRole('button', { name: 'Spend past plan limit for Plus account 1' })).toHaveAttribute('aria-pressed', 'false');
			await expect(page.getByRole('button', { name: 'Spend past plan limit for Work · work@example.test' })).toHaveAttribute('aria-pressed', 'true');
			// Wait out the finite animations; the online status dot pulses forever by design (the screenshot freezes it).
			await page.waitForFunction(() => document.getAnimations().every((a) => a.playState !== 'running' || a.effect?.getComputedTiming().iterations === Infinity));
			await page.getByText('Work · work@example.test').scrollIntoViewIfNeeded();
			await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true, animations: 'disabled' });
			await page.getByRole('button', { name: 'Overview' }).click();
			await page.screenshot({ path: testInfo.outputPath(`${name.replace('accounts', 'overview')}.png`), fullPage: true, animations: 'disabled' });
		}

		await openHarnessAccounts(page, 'codex', 'Plus account 1');
		await page.getByRole('button', { name: 'Spend past plan limit for Plus account 1' }).click();
		await expect(page.getByRole('button', { name: 'Spend past plan limit for Plus account 1' })).toHaveAttribute('aria-pressed', 'true');
		expect(writes).toEqual([{ url: '/api/vendor-accounts/codex-default/usage', body: { allow_overage: true } }]);
		await expect(page.getByRole('button', { name: 'Spend past plan limit for Work · work@example.test' })).toHaveAttribute('aria-pressed', 'true');
	});

	test('renders Claude Code account limits and verification failures in both tabs', async ({ page }, testInfo) => {
		const writes: { url: string; body: unknown }[] = [];
		await serveAccounts(page, writes);
		for (const [name, width, colorScheme] of [
			['claude-accounts-desktop-light', 1440, 'light'], ['claude-accounts-desktop-dark', 1440, 'dark'],
			['claude-accounts-393-light', 393, 'light'], ['claude-accounts-393-dark', 393, 'dark'],
		] as const) {
			await page.setViewportSize({ width, height: 1100 });
			await page.emulateMedia({ colorScheme });
			await openHarnessAccounts(page, 'claude-code', 'account 1');
			await expect(page.getByText('Usage limit reached · reset time unknown')).toBeVisible();
			await expect(page.getByText("Couldn't verify account")).toBeVisible();
			await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true, animations: 'disabled' });
			await page.getByRole('button', { name: 'Overview' }).click();
			await page.screenshot({ path: testInfo.outputPath(`${name.replace('accounts', 'overview')}.png`), fullPage: true, animations: 'disabled' });
		}
		expect(writes).toEqual([]);
	});
});
