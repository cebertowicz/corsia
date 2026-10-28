import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, type Locator, type Page } from '@playwright/test';
import { test } from './support/test';
import { loginByUrl, openSettings, TEST_TOKEN } from './support/corsia';
import { injectMessage } from './support/mock';

// Review frames for signing an account in again from Provider settings: the real app on the mock backend,
// with the vendor-account routes served from a fixture. No real vendor sign-in runs.
const outputDir = process.env.CORSIA_CAPTURE_DIR ?? 'test-results/relogin';

type State = 'signed_in' | 'signed_out';
interface Fixture { id: string; provider: 'claude-code' | 'codex'; name: string; state: State; is_default?: boolean }

const row = (f: Fixture) => ({
	id: f.id, provider: f.provider, name: f.name, is_default: !!f.is_default, state: f.state, revision: 1, identity: {},
	label: { name: f.name, email: null, text: f.name, custom: f.is_default ? null : f.name },
	...(f.state === 'signed_in' ? { usage: { state: 'allowed', usable: true, checking: false, check: null, windows: [
		{ kind: 'five_hour', state: 'reported', used_percent: f.is_default ? 41 : 18, window_minutes: 300, resets_at: new Date(Date.now() + 3 * 3600_000).toISOString() },
		{ kind: 'seven_day', state: 'reported', used_percent: f.is_default ? 63 : 27, window_minutes: 10080, resets_at: new Date(Date.now() + 3 * 86400_000).toISOString() },
	] } } : {}),
});

const CC_ERROR = 'FOMRRT1 is signed out of Claude Code. Sign it in again under Settings › Providers.';
const CODEX_ERROR = 'Couldn’t start Codex on FOMRRT1: its model list is out of date. Use Sign in again on FOMRRT1 under Settings › Providers. Reference: R-7K2M';

const harnessRows = [
	{ id: 'claude-opus', harness: 'claude-code', available: true, enabled: true, display_name: 'Opus 5.5' },
	{ id: 'claude-sonnet', harness: 'claude-code', available: true, enabled: true, display_name: 'Sonnet 5.5' },
	{ id: 'claude-haiku', harness: 'claude-code', available: true, enabled: true, display_name: 'Haiku 5.5' },
	{ id: 'codex-sol', harness: 'codex', available: true, enabled: true, display_name: 'Sol 6.1' },
	{ id: 'codex-terra', harness: 'codex', available: true, enabled: true, display_name: 'Terra 6.1' },
];
const catalogRow = (r: { id: string; harness: string; display_name: string }) => ({
	id: r.id, selector: r.id, litellm_model: r.id, name: r.display_name, display_name: r.display_name,
	connection: r.harness, provider: r.harness, backend: r.harness === 'codex' ? 'codex' : 'cc',
	status: 'ready', available: true, desc: '', size: '', speed: '',
	capabilities: { context_window_max: 200000, supports_vision: true, supports_tools: true, supports_reasoning: false, reasoning_config: null },
});

interface Scenario {
	accounts: Fixture[];
	/** What POST …/signin answers, and what it does to the fixture. */
	signin?: (account: Fixture, accounts: Fixture[]) => Record<string, unknown>;
	/** What POST …/signin/check answers; absent means no sign-in is in progress. */
	check?: (account: Fixture, accounts: Fixture[]) => Record<string, unknown> | null;
}

async function mockBackend(page: Page, scenario: Scenario) {
	const accounts = scenario.accounts;
	await page.route('**/api/models**', (route) => new URL(route.request().url()).pathname === '/api/models' && route.request().method() === 'GET'
		? route.fulfill({ json: { models: harnessRows.map(catalogRow), active_local_threads: 0 } }) : route.continue());
	await page.route('**/api/settings/models**', (route) => route.fulfill({ json: { models: harnessRows } }));
	await page.route('**/api/settings/providers**', (route) => route.fulfill({ json: { providers: [] } }));
	await page.route('**/api/settings/network-servers**', (route) => route.fulfill({ json: { servers: [] } }));
	await page.route('**/api/provider-policy**', (route) => route.fulfill({ json: { providers: [] } }));
	await page.route('**/api/vendor-accounts**', async (route) => {
		const { pathname } = new URL(route.request().url());
		const match = /^\/api\/vendor-accounts\/([^/]+)\/signin(\/check)?$/.exec(pathname);
		if (!match) return route.fulfill({ json: { accounts: accounts.map(row) } });
		const account = accounts.find((candidate) => candidate.id === match[1])!;
		const body = match[2] ? scenario.check?.(account, accounts) : scenario.signin?.(account, accounts);
		if (!body) return route.fulfill({ status: 404, json: { detail: 'no sign-in is in progress for this account' } });
		return route.fulfill({ json: body });
	});
}

const waiting = (account: Fixture) => ({ account_id: account.id, route: 'browser', status: 'waiting', cause: null });
const completed = (account: Fixture) => ({ account_id: account.id, route: 'browser', status: 'completed', cause: null });
/** What #1616's sign-in answers for an account the vendor still has signed in. */
const alreadySignedIn = (account: Fixture) => ({ account_id: account.id, route: null, status: 'already_signed_in', cause: null });
const modelRefreshFailed = (account: Fixture) => ({ account_id: account.id, route: null, status: 'model_refresh_failed', cause: 'account_refresh_failed', ref: 'R-7K2M' });

const claudeAccounts = (overrides: Record<string, State> = {}): Fixture[] => [
	{ id: 'cc-personal', provider: 'claude-code', name: 'Personal', state: 'signed_in', is_default: true },
	...[1, 2, 3, 4, 5].map((n) => ({ id: `cc-${n}`, provider: 'claude-code' as const, name: `FOMRRT${n}`, state: overrides[`cc-${n}`] ?? 'signed_in' })),
];
const codexAccounts = (): Fixture[] => [
	{ id: 'cx-personal', provider: 'codex', name: 'Personal', state: 'signed_in', is_default: true },
	{ id: 'cx-1', provider: 'codex', name: 'FOMRRT1', state: 'signed_in' },
];

/** `rowId` centers that account's row in the dialog first, so every frame of a pair shows the same stretch of the list. */
async function shot(page: Page, name: string, tag: string, rowId?: string) {
	if (rowId) await page.locator(`#vendor-account-${rowId}`).evaluate((row) => row.scrollIntoView({ block: 'center' }));
	// The developer build stamp is not part of the product.
	await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
	await page.waitForTimeout(500);
	mkdirSync(outputDir, { recursive: true });
	await page.screenshot({ path: join(outputDir, `${name}-${tag}.png`), animations: 'disabled' });
}

async function openProviderDialog(page: Page, provider: 'claude-code' | 'codex') {
	await openSettings(page);
	await page.getByRole('button', { name: 'Providers' }).click();
	const gear = page.locator(`[data-provider-card="${provider}"]`).getByRole('button', { name: 'Settings' });
	await gear.scrollIntoViewIfNeeded();
	await gear.click();
	await expect(page.getByRole('dialog')).toBeVisible();
}

const THREAD_TITLE = 'Summarise open pull requests';
type Request = Parameters<typeof injectMessage>[0];

/** A thread bound to a catalog model, so the composer reads a ready model instead of an unknown one. */
async function createThread(request: Request, title: string, model: string) {
	const response = await request.post(`/api/threads?token=${encodeURIComponent(TEST_TOKEN)}`, { data: { title, model } });
	expect(response.ok()).toBeTruthy();
	return (await response.json()).thread_id as string;
}

/** A thread that holds one failed turn's error, opened the way a person reaches it; the caller deletes it afterwards. */
async function openErrorThread(page: Page, request: Request, text: string, accountId: string, model: string, created: string[]) {
	const title = THREAD_TITLE;
	const threadId = await createThread(request, title, model);
	created.push(threadId);
	await injectMessage(request, threadId, { role: 'user', content: 'Summarise the open pull requests.' }, TEST_TOKEN);
	await injectMessage(request, threadId, { role: 'system', content: text, metadata: { type: 'harness_error', account_id: accountId } }, TEST_TOKEN);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').getByText(title, { exact: true }).click();
	await expect(page.getByRole('link', { name: 'Settings › Providers' })).toBeVisible();
}

/** The Accounts/Overview tab row must keep its natural height, however tall the dialog body is. */
async function expectTabRowIntact(page: Page) {
	const probe = await page.locator('nav.tabs').evaluate((nav) => {
		const height = () => nav.getBoundingClientRect().height;
		const style = getComputedStyle(nav);
		const parent = getComputedStyle(nav.parentElement!);
		const shown = { nav: height(), tab: nav.querySelector('.tab')!.getBoundingClientRect().height, flexShrink: style.flexShrink, parentDisplay: parent.display, parentDirection: parent.flexDirection, parentOverflow: parent.overflowY, body: nav.nextElementSibling ? nav.nextElementSibling.getBoundingClientRect().height : null };
		// Natural height: what the row measures when it is not allowed to shrink.
		const inline = nav.style.flexShrink;
		nav.style.flexShrink = '0';
		const natural = height();
		nav.style.flexShrink = inline;
		return { ...shown, natural };
	});
	console.log(`TABROW ${JSON.stringify(probe)}`);
	expect(probe.nav).toBeGreaterThanOrEqual(probe.natural - 0.5);
}

async function longPress(page: Page, target: Locator) {
	const box = (await target.boundingBox())!;
	const cdp = await page.context().newCDPSession(page);
	const point = { x: box.x + Math.min(60, box.width / 3), y: box.y + 12 };
	await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [point] });
	await page.waitForTimeout(700);
	return () => cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
}

test.describe('Sign in again review frames', () => {
	test.skip(process.env.CORSIA_CAPTURE_RELOGIN !== '1', 'Capture-only proof; enable explicitly when producing the review frames.');
	test.setTimeout(120_000);

	for (const theme of ['light', 'dark'] as const) {
		test.describe(theme, () => {
			let tag = '';
			let mobile = false;
			const created: string[] = [];
			test.afterEach(async ({ request }) => {
				for (const id of created.splice(0)) await request.delete(`/api/threads/${id}?token=${encodeURIComponent(TEST_TOKEN)}`);
			});
			async function start(page: Page, scenario: Scenario, height?: number) {
				await page.emulateMedia({ colorScheme: theme });
				await mockBackend(page, scenario);
				await page.setViewportSize(mobile ? { width: 393, height: height ?? 852 } : { width: 1920, height: height ?? 1080 });
				await loginByUrl(page, { waitForComposer: false });
				await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
			}
			test.beforeEach(({}, testInfo) => {
				mobile = testInfo.project.name === 'chromium-mobile';
				tag = `${mobile ? '393' : '1920'}-${theme}`;
			});

			test('a and b: a failed Claude Code turn links to the signed-out row with Sign in focused', async ({ page, request }) => {
				await start(page, { accounts: claudeAccounts({ 'cc-1': 'signed_out' }) });
				await openErrorThread(page, request, CC_ERROR, 'cc-1', 'claude-sonnet', created);
				await shot(page, 'a-failed-turn', tag);
				await page.getByRole('link', { name: 'Settings › Providers' }).click();
				const signIn = page.locator('#vendor-account-cc-1 [data-chat-link-focus] .btn');
				await expect(signIn).toBeFocused();
				await shot(page, 'b-providers-after-link', tag, 'cc-1');
			});

			test('c: sign-in running, then signed in again', async ({ page }) => {
				let started = false;
				const scenario: Scenario = {
					accounts: claudeAccounts({ 'cc-1': 'signed_out' }),
					signin: (account) => { started = true; return waiting(account); },
					check: (account, accounts) => {
						if (!started) return null;
						accounts.find((a) => a.id === account.id)!.state = 'signed_in';
						return completed(account);
					},
				};
				await start(page, scenario);
				await openProviderDialog(page, 'claude-code');
				await page.locator('#vendor-account-cc-1').getByRole('button', { name: 'Sign in', exact: true }).click();
				await expect(page.locator('#vendor-account-cc-1 .signin-state')).toContainText('Waiting for sign-in in your browser');
				await expectTabRowIntact(page);
				await shot(page, 'c1-sign-in-running', tag, 'cc-1');
				await page.locator('#vendor-account-cc-1').getByRole('button', { name: 'Check', exact: true }).click();
				await expect(page.locator('#vendor-account-cc-1 .signin-state')).toHaveCount(0);
				await shot(page, 'c2-signed-in-again', tag, 'cc-1');
			});

			test('c: Sign in again on an account that is still signed in', async ({ page }) => {
				await start(page, {
					accounts: claudeAccounts(),
					signin: alreadySignedIn,
				});
				await openProviderDialog(page, 'claude-code');
				await page.locator('#vendor-account-cc-1').click({ button: 'right', position: { x: 60, y: 12 } });
				await page.getByRole('menuitem', { name: 'Sign in again' }).click();
				await expect(page.locator('#vendor-account-cc-1 .signin-state')).toHaveText('FOMRRT1 is still signed in. Its model list is up to date.');
				await shot(page, 'c3-still-signed-in', tag, 'cc-1');
			});

			test('c: Sign in again when the model list could not be reloaded', async ({ page }) => {
				await start(page, {
					accounts: claudeAccounts(),
					signin: modelRefreshFailed,
				});
				await openProviderDialog(page, 'claude-code');
				await page.locator('#vendor-account-cc-1').click({ button: 'right', position: { x: 60, y: 12 } });
				await page.getByRole('menuitem', { name: 'Sign in again' }).click();
				await expect(page.locator('#vendor-account-cc-1 .signin-state')).toContainText('couldn’t be reloaded');
				await shot(page, 'c4-reload-failed', tag, 'cc-1');
			});

			test('d: the row menu on a signed-in account, by right-click or long-press', async ({ page }) => {
				await start(page, { accounts: claudeAccounts() });
				await openProviderDialog(page, 'claude-code');
				const target = page.locator('#vendor-account-cc-1');
				if (mobile) {
					const release = await longPress(page, target);
					await expect(page.getByRole('menu')).toBeVisible();
					await shot(page, 'd1-menu-long-press', tag);
					await release();
				} else {
					await target.evaluate((row) => row.scrollIntoView({ block: 'center' }));
					await target.click({ button: 'right', position: { x: 60, y: 12 } });
					await expect(page.getByRole('menu')).toBeVisible();
					await shot(page, 'd1-menu-right-click', tag);
				}
				await expect(page.getByRole('menuitem')).toHaveText(['Rename', 'Sign in again', 'Sign out', 'Remove']);
			});

			test('d: a Codex error links to the signed-in row with its menu open and Sign in again focused', async ({ page, request }) => {
				await start(page, { accounts: codexAccounts() });
				await openErrorThread(page, request, CODEX_ERROR, 'cx-1', 'codex-terra', created);
				await shot(page, 'd2-codex-error', tag);
				await page.getByRole('link', { name: 'Settings › Providers' }).click();
				await expect(page.getByRole('menuitem', { name: 'Sign in again' })).toBeFocused();
				await shot(page, 'd3-link-lands-on-menu', tag);
			});

			test('e: sign-in failed, with Sign in again', async ({ page }) => {
				await start(page, {
					accounts: claudeAccounts({ 'cc-1': 'signed_out' }),
					signin: (account) => ({ account_id: account.id, route: 'browser', status: 'failed', cause: 'vendor_login_failed' }),
				});
				await openProviderDialog(page, 'claude-code');
				await page.locator('#vendor-account-cc-1').getByRole('button', { name: 'Sign in', exact: true }).click();
				await expect(page.locator('#vendor-account-cc-1').getByRole('button', { name: 'Sign in again', exact: true })).toBeVisible();
				await shot(page, 'e-sign-in-failed', tag, 'cc-1');
			});

			test('f: five added accounts in mixed states', async ({ page }) => {
				const accounts = claudeAccounts({ 'cc-3': 'signed_out', 'cc-4': 'signed_out', 'cc-5': 'signed_out' });
				// One signed-out row has a long name: it truncates and its Sign in keeps its width.
				accounts.find((a) => a.id === 'cc-4')!.name = 'Montclair Rail Trail operations and maintenance';
				await start(page, {
					accounts,
					check: (account) => account.id === 'cc-5' ? waiting(account) : null,
				});
				await openProviderDialog(page, 'claude-code');
				await expect(page.locator('#vendor-account-cc-5 .signin-state')).toContainText('Waiting for sign-in in your browser');
				await shot(page, 'f-five-accounts', tag);
				// The same list scrolled to its end: the waiting row with Check and Cancel, then the footer.
				// A phone frame 25px shorter ends the scroll where the first visible row starts, so no row is cut at the top edge.
				if (mobile) await page.setViewportSize({ width: 393, height: 827 });
				await page.getByRole('button', { name: 'Add account' }).evaluate((footer) => footer.scrollIntoView({ block: 'end' }));
				await shot(page, 'f2-five-accounts-scrolled', tag);
			});
		});
	}
});
