import { test, expect, type Page, type Route } from '@playwright/test';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createSession, openAppByUrl, TEST_TOKEN } from '../support/corsia';
import { injectMessage } from '../support/mock';
import { setFocusMode } from '../support/focusMode';

type Fixture = { name: string; element: string; screen?: 'checking' | 'error' | 'disk-image' | 'old-macos' | 'account' | 'invalid' | 'mismatch' | 'short-password' | 'folder' | 'protected' | 'unwritable' | 'creating' | 'create-failed' | 'account-exists'; cli?: string; local?: string; api?: 'ready' | 'failed'; other?: boolean; classifier?: 'downloading' | 'failed'; requiredUsername?: string };
const fixtures: Fixture[] = [
	{ name: 'checking', screen: 'checking', element: 'Checking this computer' },
	{ name: 'status-error', screen: 'error', element: 'CORSIA couldn’t start; Retry and Save a report' },
	{ name: 'disk-image', screen: 'disk-image', element: 'Move CORSIA to Applications' },
	{ name: 'old-macos', screen: 'old-macos', element: 'Update macOS to open CORSIA' },
	{ name: 'account-empty', screen: 'account', element: 'Welcome to CORSIA and five account fields' },
	{ name: 'account-early-download-failed', screen: 'account', local: 'connection', element: 'Local model rail job: Couldn’t download' },
	{ name: 'account-invalid', screen: 'invalid', element: 'Use up to 32 lowercase letters, numbers, dashes or underscores.' },
	{ name: 'account-short-password', screen: 'short-password', element: 'Use at least 8 characters.' },
	{ name: 'account-mismatch', screen: 'mismatch', element: 'Passwords don’t match.' },
	{ name: 'account-fixed-username', screen: 'account', requiredUsername: 'alex-morgan', element: 'Username fixed by this install' },
	{ name: 'account-classifier-downloading', screen: 'account', classifier: 'downloading', element: 'Finishing setup: Downloading · 60%' },
	{ name: 'account-classifier-failed', screen: 'account', classifier: 'failed', element: 'Finishing setup: Couldn’t download · Try again' },
	{ name: 'account-exists', screen: 'account-exists', element: 'This computer already has a CORSIA account. Sign in instead.' },
	{ name: 'folder', screen: 'folder', element: 'Choose your work folder; Back' },
	{ name: 'folder-protected', screen: 'protected', element: 'Choose Allow when it does.' },
	{ name: 'folder-unwritable', screen: 'unwritable', element: 'CORSIA can’t write to this folder. Choose another one.' },
	{ name: 'creating', screen: 'creating', element: 'Starting CORSIA; Continue spinner' },
	{ name: 'create-failed', screen: 'create-failed', element: 'Couldn’t create your account. Retry.' },
	...['ready', 'signed-out', 'not-installed', 'installing', 'signing-in', 'install-connection', 'install-failed', 'signin-failed', 'unusable', 'shadow', 'limited', 'both-ready', 'codex-ready', 'claude-ready-codex-signed-out', 'bind-failed'].map((cli): Fixture => ({ name: `cli-${cli}`, cli, element: `Claude Code ${cli} row` })),
	{ name: 'cli-ready-background-download', cli: 'ready', local: 'downloading', element: 'Claude Code Ready; Local model Downloading 42%' },
	...['downloading', 'paused', 'starting', 'ready', 'stopped', 'disk-space', 'pick-failed', 'connection', 'disk-full', 'start-failed', 'memory', 'not-loaded'].map((local): Fixture => ({ name: `local-${local}`, local, element: `Local model ${local} row` })),
	{ name: 'other-omlx', other: true, element: 'Stop your oMLX while CORSIA runs?; Stop mine; Keep both running' },
	{ name: 'api-ready', api: 'ready', element: 'Anthropic · passed a test call; Ready' },
	{ name: 'api-ready-background-download', api: 'ready', local: 'downloading', element: 'Anthropic Ready; Local model Downloading 42%' },
	{ name: 'api-sheet-failed', api: 'failed', element: 'Add provider sheet: Test connection failure' },
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
			json = { apple_silicon: true, macos_supported: fixture.screen !== 'old-macos', app_location: fixture.screen === 'disk-image' ? 'disk_image' : 'applications', local_eligible: fixture.local !== 'memory', work_folder: '~/CORSIA', free_disk_gb: fixture.local === 'disk-space' ? 12 : 500, existing_omlx: fixture.other, required_username: fixture.requiredUsername, classifier: fixture.classifier === 'downloading' ? { state: 'downloading', bytes_done: 60, bytes_total: 100 } : fixture.classifier === 'failed' ? { state: 'failed', reason: 'Couldn’t download' } : undefined };
		} else if (path === '/api/setup/local-model') json = { state: fixture.local === 'pick-failed' ? 'failed' : 'picked', model: fixture.local === 'pick-failed' ? null : { name: 'Qwen 3.8', repository: 'example/model', download_size_bytes: 30000000000, download_size_gb: 30, context_window: 65536 }, message: null };
		else if (path === '/api/setup/local-download') json = method === 'GET' ? { state: fixture.local === 'connection' ? 'failed' : 'downloading', cause: 'connection', model: 'Qwen 3.8', percent: 42, eta_seconds: 720 } : fixture.other ? { ok: false, code: 'consent_required' } : { ok: true };
		else if (path === '/api/setup/classifier') json = fixture.classifier === 'downloading' ? { state: 'downloading', bytes_done: 60, bytes_total: 100 } : fixture.classifier === 'failed' ? { state: 'failed', reason: 'Couldn’t download' } : { state: 'ready' };
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
		else if (path.endsWith('/anthropic/test')) { apiTested = fixture.api === 'ready'; json = fixture.api === 'ready' ? { ok: true, message: 'Key is valid' } : { ok: false, error: 'Couldn’t test this key. Check it and try again.' }; }
		else if (path.includes('/install')) {
			installed = true;
			json = fixture.cli === 'installing' ? { state: 'running' } : fixture.cli === 'shadow' ? { state: 'done', path_shadowed_by: '/usr/local/bin/claude' } : fixture.cli === 'install-connection' ? { state: 'failed', reason: 'no internet connection' } : fixture.cli === 'install-failed' ? { state: 'failed', reason: 'the installer stopped', ref: 'cli-fixture-123' } : { state: 'done' };
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
	if (fixture.screen === 'short-password') { await page.getByLabel('Password', { exact: true }).fill('short'); await page.getByLabel('Name', { exact: true }).focus(); return; }
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
		if (fixture.api === 'ready') await page.getByRole('button', { name: 'Close', exact: true }).last().click();
	}
}
async function assertState(page: Page, fixture: Fixture) {
	const text = (value: string | RegExp) => page.getByText(value, { exact: typeof value === 'string' });
	if (fixture.screen === 'checking') { await expect(page.getByRole('heading', { name: 'Checking this computer' })).toBeVisible(); return; }
	if (fixture.screen === 'error') { await expect(page.getByRole('heading', { name: 'CORSIA couldn’t start' })).toBeVisible(); await expect(page.getByRole('button', { name: 'Save a report' })).toBeVisible(); return; }
	if (fixture.screen === 'disk-image') { await expect(page.getByRole('heading', { name: 'Move CORSIA to Applications' })).toBeVisible(); return; }
	if (fixture.screen === 'old-macos') { await expect(page.getByRole('heading', { name: 'Update macOS to open CORSIA' })).toBeVisible(); return; }
	if (fixture.screen === 'invalid') { await expect(text('Use up to 32 lowercase letters, numbers, dashes or underscores.')).toBeVisible(); return; }
	if (fixture.screen === 'short-password') { await expect(text('Use at least 8 characters.')).toBeVisible(); return; }
	if (fixture.screen === 'mismatch') { await expect(text('Passwords don’t match.')).toBeVisible(); return; }
	if (fixture.screen === 'account-exists') { await expect(text('This computer already has a CORSIA account. Sign in instead.')).toBeVisible(); return; }
	if (fixture.screen === 'protected') { await expect(text('macOS asks you once to let CORSIA use your Documents folder. Choose Allow when it does.')).toBeVisible(); return; }
	if (fixture.screen === 'unwritable') { await expect(text('CORSIA can’t write to this folder. Choose another one.')).toBeVisible(); await expect(page.getByRole('button', { name: 'Continue', exact: true })).toBeDisabled(); return; }
	if (fixture.screen === 'creating') { await expect(text('Starting CORSIA')).toBeVisible(); return; }
	if (fixture.screen === 'create-failed') { await expect(text('Couldn’t create your account. Retry.')).toBeVisible(); await expect(page.getByRole('button', { name: 'Save a report', exact: true })).toBeVisible(); return; }
	if (fixture.requiredUsername) { await expect(page.getByLabel('Username', { exact: true })).toHaveAttribute('readonly'); }
	if (fixture.classifier === 'downloading') { await expect(text('Finishing setup')).toBeVisible(); await expect(text('Downloading · 60%')).toBeVisible(); }
	if (fixture.classifier === 'failed') { await expect(text('Finishing setup')).toBeVisible(); await expect(page.getByRole('button', { name: /^Try again/ })).toBeVisible(); }
	if (fixture.screen === 'account') { await expect(page.getByRole('heading', { name: 'Welcome to CORSIA' })).toBeVisible(); if (fixture.local === 'connection') await expect(text('Couldn’t download')).toBeVisible(); return; }
	if (fixture.screen === 'folder') { await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeVisible(); return; }
	if (fixture.other) { await expect(page.getByRole('heading', { name: 'Stop your oMLX while CORSIA runs?' })).toBeVisible(); return; }
	if (fixture.api === 'ready') { await expect(page.getByRole('region', { name: 'API provider' }).getByText('Anthropic · passed a test call')).toBeVisible(); return; }
	if (fixture.api === 'failed') { await expect(text('Couldn’t test this key. Check it and try again.')).toBeVisible(); return; }
	if (fixture.cli === 'bind-failed') { await expect(page.getByRole('button', { name: 'Start using CORSIA', exact: true })).toBeDisabled(); return; }
	if (fixture.cli === 'codex-ready') { await expect(page.getByRole('region', { name: 'Codex' }).getByText('Ready')).toBeVisible(); return; }
	if (fixture.cli === 'claude-ready-codex-signed-out') { await expect(page.getByRole('region', { name: 'Codex' }).getByRole('button', { name: 'Sign in', exact: true })).toBeVisible(); return; }
	if (fixture.cli === 'install-connection') { const row = page.getByRole('region', { name: 'Claude Code' }); await expect(row).toContainText('Couldn’t download Claude Code. Check your internet connection, then retry.'); await expect(row.getByRole('button', { name: 'Save a report', exact: true })).toBeVisible(); }
	if (fixture.cli === 'install-failed') { const row = page.getByRole('region', { name: 'Claude Code' }); await expect(row).toContainText('Couldn’t install Claude Code. Retry, or save a report. Reference: cli-fixture-123'); await expect(row.getByRole('button', { name: 'Save a report', exact: true })).toBeVisible(); }
	if (fixture.cli && !['install-connection', 'install-failed'].includes(fixture.cli)) { await expect(page.getByRole('region', { name: 'Claude Code' })).toContainText(fixture.cli === 'ready' || fixture.cli === 'both-ready' ? 'Ready' : fixture.cli === 'signed-out' ? 'Sign in' : fixture.cli === 'not-installed' ? 'Set up' : fixture.cli === 'installing' ? 'Installing Claude Code…' : fixture.cli === 'signing-in' ? 'Finish signing in in your browser.' : fixture.cli === 'limited' ? 'usage limit' : fixture.cli === 'unusable' ? 'can’t use' : fixture.cli === 'shadow' ? 'older Claude Code' : 'Couldn’t'); }
	if (fixture.local) { const row = page.getByRole('region', { name: 'Local model' }); await expect(row).toContainText(fixture.local === 'ready' ? 'Ready' : fixture.local === 'memory' ? 'at least 24 GB' : fixture.local === 'not-loaded' ? 'isn’t enough free memory' : fixture.local === 'downloading' ? 'Downloading 42%' : fixture.local === 'paused' ? 'Download paused' : fixture.local === 'starting' ? 'Starting the local model' : fixture.local === 'disk-space' ? 'Needs 30 GB free' : fixture.local === 'disk-full' ? 'disk is full' : fixture.local === 'connection' ? 'internet connection' : fixture.local === 'pick-failed' ? 'Couldn’t find a local model' : fixture.local === 'start-failed' ? 'couldn’t start' : 'Download'); if (fixture.local === 'start-failed') await expect(row.getByRole('button', { name: 'Save a report', exact: true })).toBeVisible(); }
}
async function capture(page: Page, fixture: Fixture, theme: string, width: number, height: number) {
	page.setDefaultTimeout(10_000);
	const errors: string[] = [];
	page.on('pageerror', (error) => { errors.push(error.stack ?? error.message); });
	await page.setViewportSize({ width, height });
	await page.emulateMedia({ colorScheme: theme as 'light' | 'dark', reducedMotion: 'reduce' });
	await serve(page, fixture); await navigate(page, fixture);
	await page.waitForTimeout(350);
	await assertState(page, fixture);
	expect(errors, `${fixture.name}: browser runtime errors`).toEqual([]);
	const file = `${fixture.name}-${theme}-${width}x${height}.png`;
	await page.screenshot({ path: resolve(assets!, file), scale: 'css' });
	records.push({ file, state: fixture.name, element: fixture.element });
	console.log(`FRAME ${file}: ${fixture.element}`);
}

test('setup redesign complete state frames @setup-redesign', async ({ browser, request }) => {
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
	await setFocusMode(request, false);
	try {
	for (const theme of ['light', 'dark']) {
		const context = await browser.newContext({ colorScheme: theme as 'light' | 'dark', reducedMotion: 'reduce', viewport: { width: 1280, height: 900 } });
		const page = await context.newPage();
		const title = `First-run opening fixture ${theme} ${Date.now()}`;
		const threadId = await createSession(request, title);
		const opening = 'Fixture first-run opening. This is rendered test content; Agent Substrate’s prompt is not represented.';
		await injectMessage(request, threadId, { role: 'assistant', content: opening }, TEST_TOKEN);
		await openAppByUrl(page);
		await page.getByText(title).first().click();
		await expect(page.getByText(opening)).toBeVisible();
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
		await page.waitForFunction((message) => {
			const composer = document.querySelector<HTMLElement>('[aria-label="Message CORSIA"]');
			const openingNode = [...document.querySelectorAll<HTMLElement>('*')].find((element) => element.textContent?.trim() === message);
			const opaque = (element: HTMLElement | null) => {
				for (let current = element; current; current = current.parentElement) if (Number.parseFloat(getComputedStyle(current).opacity) < 1) return false;
				return Boolean(element);
			};
			return opaque(composer) && opaque(openingNode) && document.getAnimations().every((animation) => animation.playState !== 'running');
		}, opening);
		await expect(page.getByText('Select Model', { exact: true })).toHaveCount(0);
		const file = `first-run-opening-${theme}-1280x900.png`;
		await page.screenshot({ path: resolve(assets!, file), scale: 'css' });
		records.push({ file, state: 'first-run-opening-fixture', element: 'Fixture opening message and bound composer model (not Select Model)' });
		console.log(`FRAME ${file}: first-run fixture opening and bound composer model`);
		await context.close();
	}
	} finally {
		await setFocusMode(request, true);
	}
	const manifest = resolve(assets!, 'frames.json');
	const previous: typeof records = existsSync(manifest) ? JSON.parse(readFileSync(manifest, 'utf8')) : [];
	const replacements = new Set(records.map((record) => record.file));
	writeFileSync(manifest, JSON.stringify([...previous.filter((record) => !replacements.has(record.file)), ...records], null, 2));
});
