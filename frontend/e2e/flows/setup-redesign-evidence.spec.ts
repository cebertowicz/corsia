import { test, expect, type Page, type Route } from '@playwright/test';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

type Fixture = { name: string; element: string; screen?: 'checking' | 'error' | 'disk-image' | 'old-macos' | 'account' | 'invalid' | 'mismatch' | 'folder' | 'protected' | 'unwritable' | 'creating' | 'create-failed' | 'account-exists'; cli?: string; local?: string; api?: boolean; other?: boolean };
const fixtures: Fixture[] = [
	{ name: 'checking', screen: 'checking', element: 'Checking this computer' },
	{ name: 'status-error', screen: 'error', element: 'CORSIA couldn’t start; Retry and Save a report' },
	{ name: 'disk-image', screen: 'disk-image', element: 'Move CORSIA to Applications' },
	{ name: 'old-macos', screen: 'old-macos', element: 'Update macOS to open CORSIA' },
	{ name: 'account-empty', screen: 'account', element: 'Welcome to CORSIA and five account fields' },
	{ name: 'account-early-download-failed', screen: 'account', local: 'connection', element: 'Local model rail job: Couldn’t download' },
	{ name: 'account-invalid', screen: 'invalid', element: 'Use up to 32 lowercase letters, numbers, dashes or underscores.' },
	{ name: 'account-mismatch', screen: 'mismatch', element: 'Passwords don’t match.' },
	{ name: 'account-exists', screen: 'account-exists', element: 'This computer already has a CORSIA account. Sign in instead.' },
	{ name: 'folder', screen: 'folder', element: 'Choose your work folder; Back' },
	{ name: 'folder-protected', screen: 'protected', element: 'Choose Allow when it does.' },
	{ name: 'folder-unwritable', screen: 'unwritable', element: 'CORSIA can’t write to this folder. Choose another one.' },
	{ name: 'creating', screen: 'creating', element: 'Starting CORSIA; Continue spinner' },
	{ name: 'create-failed', screen: 'create-failed', element: 'Couldn’t create your account. Retry.' },
	...['ready', 'signed-out', 'not-installed', 'installing', 'signing-in', 'install-connection', 'install-failed', 'signin-failed', 'unusable', 'shadow', 'limited', 'both-ready', 'codex-ready', 'claude-ready-codex-signed-out', 'bind-failed'].map((cli): Fixture => ({ name: `cli-${cli}`, cli, element: `Claude Code ${cli} row` })),
	...['downloading', 'paused', 'starting', 'ready', 'stopped', 'disk-space', 'pick-failed', 'connection', 'disk-full', 'start-failed', 'memory', 'not-loaded'].map((local): Fixture => ({ name: `local-${local}`, local, element: `Local model ${local} row` })),
	{ name: 'other-omlx', other: true, element: 'Stop your oMLX while CORSIA runs?; Stop mine; Keep both running' },
	{ name: 'api-ready', api: true, element: 'anthropic · passed a test call; Ready' },
];
const selectedFixtures = fixtures.filter((fixture) => !process.env.CORSIA_SETUP_FRAME_STATES || process.env.CORSIA_SETUP_FRAME_STATES.split(',').includes(fixture.name));
const records: { file: string; state: string; element: string }[] = [];
const assets = process.env.CORSIA_SETUP_FRAME_DIR;
if (!assets) throw new Error('CORSIA_SETUP_FRAME_DIR must name the review pack assets directory');
mkdirSync(assets, { recursive: true });

function cliSource(cli?: string, vendor = 'claude-code') {
	if (!cli || cli === 'not-installed') {
		const proof = JSON.parse(readFileSync(process.env.CORSIA_SETUP_CLI_PROOF!, 'utf8'));
		return proof.sources.find((row: { source: string }) => row.source === vendor);
	}
	const state = cli === 'ready' || cli === 'both-ready' || cli === 'limited' ? 'ready' : cli === 'signed-out' || cli === 'signing-in' || cli === 'signin-failed' ? 'needs_auth' : cli === 'unusable' ? 'unverified' : 'not_installed';
	return { source: vendor, name: vendor === 'codex' ? 'Codex' : 'Claude Code', state, limited: cli === 'limited', resets_at: '2026-10-20T01:00:00Z', checked_at: '2026-10-19T17:00:00Z', ...(cli === 'unusable' ? { cli_package_manager: 'homebrew' } : {}) };
}
function localSource(state = 'stopped') {
	return { source: 'omlx', name: 'Local model', state: state === 'ready' ? 'ready' : 'needs_setup', local_state: state === 'not-loaded' ? 'not_loaded' : state,
		download_size: '30 GB', download_percent: 42, download_eta_seconds: 720, setup_downloading: ['downloading', 'paused'].includes(state), download_stalled: state === 'paused',
		setup_stage: state === 'starting' ? 'starting' : undefined, setup_error: ['connection', 'disk-full', 'start-failed'].includes(state) ? 'failed' : undefined,
		setup_error_code: state === 'disk-full' ? 'disk_full' : state === 'start-failed' ? 'start_failed' : state, setup_error_reference: 'setup-report-123', bytes_to_free: 5000000000 };
}
function catalogRow(connection: string) {
	return { id: `${connection}/vendor-default`, litellm_model: `${connection}/vendor-default`, name: 'Vendor default', connection, provider: connection, backend: connection === 'omlx' ? 'network' : 'api', status: 'ready', is_default: true,
		...(['claude-code', 'codex'].includes(connection) ? { runs_on: [{ account_id: 'disposable-account', selected: true, status: 'ready' }] } : {}) };
}
async function serve(page: Page, fixture: Fixture) {
	let installed = false, apiTested = false, statusReads = 0, bindFailed = false;
	await page.routeWebSocket((url) => url.pathname === '/ws', () => {});
	await page.addInitScript(({ screen }) => {
		if (screen === 'protected' || screen === 'unwritable') {
			(window as unknown as { __TAURI_INTERNALS__: unknown }).__TAURI_INTERNALS__ = { invoke: async () => screen === 'protected' ? { state: 'ready', path: '~/Documents/CORSIA', protected_folder: 'Documents' } : { state: 'unwritable', path: '/Volumes/Archive/CORSIA', reason: 'read-only' } };
		}
	}, { screen: fixture.screen });
	await page.route((url) => url.pathname.startsWith('/api/'), async (route: Route) => {
		const url = new URL(route.request().url()), path = url.pathname, method = route.request().method();
		let json: unknown = {};
		if (path === '/api/auth/status') json = { has_users: false };
		else if (path === '/api/setup/status') {
			statusReads += 1;
			if (fixture.screen === 'checking' && statusReads > 1) return;
			if (fixture.screen === 'error') { await route.fulfill({ status: 500, json: {} }); return; }
			json = { apple_silicon: true, macos_supported: fixture.screen !== 'old-macos', app_location: fixture.screen === 'disk-image' ? 'disk_image' : 'applications', local_eligible: fixture.local !== 'memory', work_folder: '~/CORSIA', free_disk_gb: fixture.local === 'disk-space' ? 12 : 500, existing_omlx: fixture.other };
		} else if (path === '/api/setup/local-model') json = { state: fixture.local === 'pick-failed' ? 'failed' : 'picked', model: fixture.local === 'pick-failed' ? null : { name: 'Qwen 3.8', repository: 'example/model', download_size_bytes: 30000000000, download_size_gb: 30, context_window: 65536 }, message: null };
		else if (path === '/api/setup/local-download') json = method === 'GET' ? { state: fixture.local === 'connection' ? 'failed' : 'downloading', cause: 'connection', model: 'Qwen 3.8', percent: 42, eta_seconds: 720 } : fixture.other ? { ok: false, code: 'consent_required' } : { ok: true };
		else if (path === '/api/setup/classifier') json = { state: 'ready' };
		else if (path === '/api/setup/first-account') {
			if (fixture.screen === 'creating') return;
			if (fixture.screen === 'account-exists') { await route.fulfill({ status: 409, json: {} }); return; }
			if (fixture.screen === 'create-failed') { await route.fulfill({ status: 500, json: {} }); return; }
			json = { token: 'disposable-frame-session', user: { id: 'frame-person', name: 'Alex Morgan' } };
		} else if (path === '/api/setup/progress') json = method === 'GET' ? { setup_completed: false, step: 'models' } : { ok: true };
		else if (path === '/api/corsia-agent') json = { target: { thread_id: 'frame-corsia-agent' }, first_run_pending: true };
		else if (path === '/api/threads/frame-corsia-agent' && method === 'PATCH') { bindFailed = true; await route.fulfill({ status: 409, json: { detail: 'Model no longer ready' } }); return; }
		else if (path.includes('/providers/sources')) json = { sources: [cliSource(bindFailed ? 'signed-out' : fixture.cli === 'bind-failed' ? 'ready' : fixture.cli === 'codex-ready' ? 'not-installed' : fixture.cli === 'claude-ready-codex-signed-out' ? 'ready' : installed && fixture.cli === 'signing-in' ? 'signed-out' : fixture.cli), cliSource(['both-ready', 'codex-ready'].includes(fixture.cli ?? '') ? 'ready' : fixture.cli === 'claude-ready-codex-signed-out' ? 'signed-out' : 'not-installed', 'codex'), localSource(fixture.local)] };
		else if (path === '/api/models') json = { models: [...(!bindFailed && ['ready', 'both-ready', 'claude-ready-codex-signed-out', 'bind-failed'].includes(fixture.cli ?? '') ? [catalogRow('claude-code')] : []), ...(['both-ready', 'codex-ready'].includes(fixture.cli ?? '') ? [catalogRow('codex')] : []), ...(fixture.local === 'ready' ? [catalogRow('omlx')] : []), ...(apiTested ? [catalogRow('anthropic')] : [])] };
		else if (path === '/api/settings/providers') json = { providers: [{ name: 'anthropic', label: 'Anthropic', type: 'cloud', has_key: false, models: [], api_base: null, masked_key: null, docs_url: null }] };
		else if (path.endsWith('/anthropic/test')) { apiTested = true; json = { ok: true, message: 'Key is valid' }; }
		else if (path.includes('/install')) {
			installed = true;
			json = fixture.cli === 'installing' ? { state: 'running' } : fixture.cli === 'shadow' ? { state: 'done', path_shadowed_by: '/usr/local/bin/claude' } : fixture.cli === 'install-connection' ? { state: 'failed', reason: 'internet connection' } : fixture.cli === 'install-failed' ? { state: 'failed', reason: 'installer exited' } : { state: 'done' };
		} else if (path.includes('/signin') || path.includes('/sign-in')) json = fixture.cli === 'signin-failed' ? { status: 'failed' } : { status: 'waiting', account_id: 'frame-account' };
		else if (path.includes('/accounts')) json = { accounts: [], id: 'frame-account', account_id: 'frame-account' };
		else if (path === '/api/threads') json = { threads: [] };
		else if (path === '/api/projects') json = { projects: [] };
		await route.fulfill({ json });
	});
}
async function fillAccount(page: Page) {
	await page.getByLabel('Name', { exact: true }).fill('Alex Morgan');
	await page.getByLabel('Password', { exact: true }).fill('setup-example-password');
	await page.getByLabel('Confirm password', { exact: true }).fill('setup-example-password');
}
async function navigate(page: Page, fixture: Fixture) {
	await page.goto('/');
	if (fixture.screen === 'checking') { await expect(page.getByRole('heading', { name: 'Checking this computer' })).toBeVisible(); return; }
	if (['error', 'disk-image', 'old-macos'].includes(fixture.screen ?? '')) return;
	await expect(page.getByRole('heading', { name: 'Welcome to CORSIA' })).toBeVisible();
	if (fixture.screen === 'account') return;
	if (fixture.screen === 'invalid') { await page.getByLabel('Username', { exact: true }).fill('Bad user!'); return; }
	if (fixture.screen === 'mismatch') { await fillAccount(page); await page.getByLabel('Confirm password', { exact: true }).fill('does-not-match'); await page.getByLabel('Name', { exact: true }).focus(); return; }
	await fillAccount(page);
	await page.getByRole('button', { name: 'Continue', exact: true }).click();
	await expect(page.getByRole('heading', { name: 'Choose your work folder' })).toBeVisible();
	if (fixture.screen === 'protected' || fixture.screen === 'unwritable') { await page.getByRole('button', { name: 'Change', exact: true }).click(); return; }
	if (fixture.screen === 'folder') return;
	await page.getByRole('button', { name: 'Continue', exact: true }).click();
	if (fixture.screen === 'creating' || fixture.screen === 'create-failed' || fixture.screen === 'account-exists') return;
	await expect(page.getByRole('heading', { name: 'Providers & models', exact: true })).toBeVisible();
	if (['installing', 'install-connection', 'install-failed', 'shadow'].includes(fixture.cli ?? '')) await page.getByRole('region', { name: 'Claude Code' }).getByRole('button', { name: 'Set up' }).click();
	if (['signing-in', 'signin-failed'].includes(fixture.cli ?? '')) await page.getByRole('region', { name: 'Claude Code' }).getByRole('button', { name: 'Sign in', exact: true }).click();
	if (fixture.cli === 'bind-failed') {
		await page.getByRole('button', { name: 'Start using CORSIA', exact: true }).click();
		await expect(page.getByRole('heading', { name: 'Providers & models', exact: true })).toBeVisible();
		await expect(page.getByRole('button', { name: 'Start using CORSIA', exact: true })).toBeDisabled();
		await expect(page.getByRole('region', { name: 'Claude Code' }).getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
	}
	if (fixture.api) {
		await page.getByRole('button', { name: 'Add provider', exact: true }).click();
		await page.getByRole('button', { name: /^Anthropic Cloud API/ }).click();
		await page.getByRole('button', { name: 'Test connection', exact: true }).click();
		await page.getByRole('button', { name: 'Close', exact: true }).last().click();
	}
}
async function capture(page: Page, fixture: Fixture, theme: string, width: number, height: number) {
	page.setDefaultTimeout(10_000);
	const errors: string[] = [];
	page.on('pageerror', (error) => { errors.push(error.stack ?? error.message); });
	await page.setViewportSize({ width, height });
	await page.emulateMedia({ colorScheme: theme as 'light' | 'dark', reducedMotion: 'reduce' });
	await serve(page, fixture); await navigate(page, fixture);
	await page.waitForTimeout(350);
	expect(errors, `${fixture.name}: browser runtime errors`).toEqual([]);
	const file = `${fixture.name}-${theme}-${width}x${height}.png`;
	await page.screenshot({ path: resolve(assets!, file), scale: 'css' });
	records.push({ file, state: fixture.name, element: fixture.element });
	console.log(`FRAME ${file}: ${fixture.element}`);
}

test('setup redesign complete state frames @setup-redesign', async ({ browser }) => {
	test.setTimeout(600000);
	for (const theme of ['light', 'dark']) {
		for (const fixture of selectedFixtures) {
			const context = await browser.newContext(); const page = await context.newPage();
			await capture(page, fixture, theme, 1280, 900); await context.close();
		}
		for (const fixture of selectedFixtures.filter((row) => ['account-empty', 'folder', 'cli-ready'].includes(row.name))) {
			const context = await browser.newContext(); const page = await context.newPage();
			await capture(page, fixture, theme, 800, 600); await context.close();
		}
	}
	writeFileSync(resolve(assets!, 'frames.json'), JSON.stringify(records, null, 2));
});
