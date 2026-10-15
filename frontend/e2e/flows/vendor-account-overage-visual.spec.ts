import { expect, test, type Page } from '@playwright/test';
import { loginByUrl, openSettings } from '../support/corsia';

// Synthetic accounts only: the disposable backend has no vendor CLI, so the
// harness inventory and account list are served here and every write is observed.
const ACCOUNTS = [
	{ id: 'codex-default', provider: 'codex', name: 'Default', is_default: true, state: 'legacy', revision: 0,
		identity: {}, label: { name: 'Plus account 1', email: null, text: 'Plus account 1' }, duplicate_of: null, allow_overage: false,
		usage: { state: 'limited', usable: false, reason: 'five_hour_limit', resets_at: null, windows: [], checking: false, check: { error: 'offline' } } },
	{ id: 'codex-work', provider: 'codex', name: 'Work', is_default: false, state: 'signed_in', revision: 2,
		identity: { email: 'work@example.test' }, label: { name: 'Work', email: 'work@example.test', text: 'Work · work@example.test' }, duplicate_of: null, allow_overage: true },
	{ id: 'work', provider: 'codex', name: 'Work', is_default: false, state: 'signed_out', revision: 2,
		identity: { email: 'work@example.test' }, label: { name: 'Work', email: 'work@example.test', text: 'Work · work@example.test', custom: 'Work' }, duplicate_of: null, allow_overage: false },
	{ id: 'claude-default', provider: 'claude-code', name: 'Default', is_default: true, state: 'legacy', revision: 0,
		identity: {}, label: { name: 'account 1', email: null, text: 'account 1' }, duplicate_of: null, allow_overage: false,
		usage: { state: 'limited', usable: false, reason: 'five_hour_limit', resets_at: null, windows: [], checking: false, check: { error: 'offline' } } },
];

const MODELS = [
	{ id: 'codex/test', harness: 'codex', connection: 'codex', available: true, enabled: true, status: 'ready', display_name: 'Codex test' },
	{ id: 'claude-code/test', harness: 'claude-code', connection: 'claude-code', available: true, enabled: true, status: 'ready', display_name: 'Claude test' },
];

async function serveAccounts(page: Page, writes: { url: string; body: unknown }[]) {
	const accounts = ACCOUNTS.map((account) => ({ ...account }));
	await page.route('**/api/settings/models?**', (route) => route.fulfill({ json: { models: MODELS } }));
	await page.route('**/api/models**', (route) => route.fulfill({ json: { models: MODELS } }));
	await page.route('**/api/vendor-accounts?**', (route) => route.fulfill({ json: { accounts } }));
	await page.route('**/api/vendor-accounts/*/signin/check**', (route) => route.fulfill({ status: 409, json: { detail: 'no sign-in is in progress for this account' } }));
	await page.route('**/api/vendor-accounts/*/usage?**', async (route) => {
		const id = decodeURIComponent(new URL(route.request().url()).pathname.split('/')[3]);
		const body = route.request().postDataJSON() as { allow_overage: boolean };
		writes.push({ url: new URL(route.request().url()).pathname, body });
		const account = accounts.find((row) => row.id === id)!;
		account.allow_overage = body.allow_overage;
		await route.fulfill({ json: account });
	});
}

async function openHarnessAccounts(page: Page, provider: string, accountId: string) {
	await loginByUrl(page, { waitForComposer: false });
	await openSettings(page);
	await page.getByRole('button', { name: 'Providers' }).click();
	const card = page.locator(`[data-provider-card="${provider}"]`);
	await card.getByRole('button', { name: 'Settings' }).click();
	await expect(page.locator(`#vendor-account-${accountId}`)).toBeVisible();
}

test.describe('@surface:providers accounts live in settings modal', () => {
	test('the harness gear opens canonical account rows and a flip writes only that account', async ({ page }, testInfo) => {
		const writes: { url: string; body: unknown }[] = [];
		await serveAccounts(page, writes);
		for (const [name, width, colorScheme] of [
			['accounts-overage-desktop-light', 1440, 'light'], ['accounts-overage-desktop-dark', 1440, 'dark'],
			['accounts-overage-393-light', 393, 'light'], ['accounts-overage-393-dark', 393, 'dark'],
		] as const) {
			await page.setViewportSize({ width, height: 1100 });
			await page.emulateMedia({ colorScheme });
			await openHarnessAccounts(page, 'codex', 'codex-default');
			await expect(page.getByText('Plus account 1')).toBeVisible();
			await expect(page.getByText('Usage limit reached · reset time unknown')).toBeVisible();
			await expect(page.getByText("Couldn't verify account")).toBeVisible();
			await expect(page.locator('#vendor-account-codex-work').getByText('Work · work@example.test', { exact: true })).toBeVisible();
			await expect(page.getByRole('button', { name: 'Spend past plan limit for Plus account 1' })).toHaveAttribute('aria-pressed', 'false');
			await expect(page.locator('#vendor-account-codex-work').getByRole('button', { name: 'Spend past plan limit for Work · work@example.test' })).toHaveAttribute('aria-pressed', 'true');
			await page.locator('#vendor-account-codex-work').scrollIntoViewIfNeeded();
			await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true, animations: 'disabled' });
			await page.getByRole('button', { name: 'Overview' }).click();
			await page.screenshot({ path: testInfo.outputPath(`${name.replace('accounts', 'overview')}.png`), fullPage: true, animations: 'disabled' });
		}

		await openHarnessAccounts(page, 'codex', 'codex-default');
		await page.getByRole('button', { name: 'Spend past plan limit for Plus account 1' }).click();
		await expect(page.getByRole('button', { name: 'Spend past plan limit for Plus account 1' })).toHaveAttribute('aria-pressed', 'true');
		expect(writes).toEqual([{ url: '/api/vendor-accounts/codex-default/usage', body: { allow_overage: true } }]);
		await expect(page.locator('#vendor-account-codex-work').getByRole('button', { name: 'Spend past plan limit for Work · work@example.test' })).toHaveAttribute('aria-pressed', 'true');
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
			await openHarnessAccounts(page, 'claude-code', 'claude-default');
			await expect(page.getByText('Usage limit reached · reset time unknown')).toBeVisible();
			await expect(page.getByText("Couldn't verify account")).toBeVisible();
			await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true, animations: 'disabled' });
			await page.getByRole('button', { name: 'Overview' }).click();
			await page.screenshot({ path: testInfo.outputPath(`${name.replace('accounts', 'overview')}.png`), fullPage: true, animations: 'disabled' });
		}
		expect(writes).toEqual([]);
	});

	test('captures the signed-out account row with its available actions in both themes', async ({ page }, testInfo) => {
		const writes: { url: string; body: unknown }[] = [];
		await serveAccounts(page, writes);
		for (const colorScheme of ['light', 'dark'] as const) {
			await page.setViewportSize({ width: 800, height: 600 });
			await page.emulateMedia({ colorScheme, reducedMotion: 'reduce' });
			await openHarnessAccounts(page, 'codex', 'work');
			const row = page.locator('#vendor-account-work');
			await expect(row).toBeVisible();
			await expect(row.getByRole('button', { name: 'Sign in' })).toBeVisible();
			await row.getByRole('button', { name: 'More actions for Work · work@example.test' }).click();
			await expect(row.getByRole('menu')).toBeVisible();
			await row.scrollIntoViewIfNeeded();
			await page.screenshot({ path: testInfo.outputPath(`accounts-signed-out-menu-head-${colorScheme}-800x600.png`), fullPage: false, animations: 'disabled', scale: 'css' });
		}
		expect(writes).toEqual([]);
	});
});
