import { test, expect, type Page, type Route } from '@playwright/test';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { openAppByUrl, TEST_TOKEN } from '../support/corsia';
import { injectMessage } from '../support/mock';
import { setFocusMode } from '../support/focusMode';

type LocalFixture = 'downloading' | 'paused' | 'starting' | 'ready' | 'stopped' | 'disk-space' | 'pick-failed' | 'connection' | 'disk-full' | 'memory' | 'other-ref' | 'other-no-ref' | 'setup-failed' | 'start-failed' | 'other-omlx';
type Fixture = { name: string; element: string; screen?: 'checking' | 'error' | 'disk-image' | 'old-macos' | 'account' | 'invalid' | 'mismatch' | 'short-password' | 'folder' | 'protected' | 'unwritable' | 'creating' | 'create-failed' | 'account-exists'; cli?: string; local?: LocalFixture; finish?: 'no-model' | 'progress-save'; api?: 'ready' | 'failed'; other?: boolean; classifier?: 'downloading' | 'failed'; requiredUsername?: string };
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
	...['downloading', 'paused', 'starting', 'ready', 'stopped', 'disk-space', 'pick-failed', 'connection', 'disk-full', 'memory', 'other-ref', 'other-no-ref', 'setup-failed', 'start-failed', 'other-omlx'].map((local): Fixture => ({ name: `local-${local}`, local, element: `Local model ${local} row` })),
	{ name: 'other-omlx', other: true, element: 'Stop your oMLX while CORSIA runs?; Stop mine; Keep both running' },
	{ name: 'finish-no-model', cli: 'ready', finish: 'no-model', element: 'CORSIA couldn’t find a working model; Retry' },
	{ name: 'finish-progress-save', cli: 'ready', finish: 'progress-save', element: 'CORSIA couldn’t save setup progress; Retry' },
	{ name: 'api-ready', api: 'ready', element: 'Anthropic · passed a test call; Ready' },
	{ name: 'api-ready-background-download', api: 'ready', local: 'downloading', element: 'Anthropic Ready; Local model Downloading 42%' },
	{ name: 'api-sheet-failed', api: 'failed', element: 'Add provider sheet: Test connection failure' },
];
const selectedFixtures = fixtures.filter((fixture) => !process.env.CORSIA_SETUP_FRAME_STATES || process.env.CORSIA_SETUP_FRAME_STATES.split(',').includes(fixture.name));
const records: { file: string; state: string; element: string; assertion: string; sha256: string }[] = [];
const assets = process.env.CORSIA_SETUP_FRAME_DIR;
const fileSha256 = (file: string) => createHash('sha256').update(readFileSync(file)).digest('hex');
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
function localFlight(state: LocalFixture | undefined) {
	if (state === 'connection') return { state: 'failed', cause: 'connection' };
	if (state === 'disk-full') return { state: 'failed', cause: 'disk_full', bytes_to_free: 5_000_000_000 };
	if (state === 'memory') return { state: 'failed', cause: 'memory' };
	if (state === 'other-ref') return { state: 'failed', cause: 'other', ref: 'setup-report-123' };
	if (state === 'other-no-ref') return { state: 'failed', cause: 'other' };
	if (state === 'setup-failed') return { state: 'setup_failed', cause: 'other' };
	if (state === 'start-failed') return { state: 'start_failed', cause: 'connection' };
	return { state: state === 'other-omlx' ? 'stopped' : state ?? 'stopped' };
}
function localSource(state?: LocalFixture) {
	const flight = localFlight(state);
	return { source: 'omlx', name: 'Local model', state: state === 'ready' ? 'ready' : 'needs_setup', local_state: flight.state,
		download_size: '30 GB', download_percent: 42, download_eta_seconds: 720, setup_downloading: ['downloading', 'paused'].includes(state ?? ''), download_stalled: state === 'paused',
		setup_stage: state === 'starting' ? 'starting' : undefined, setup_error: ['failed', 'setup_failed', 'start_failed'].includes(flight.state) ? 'failed' : undefined,
		setup_error_code: 'cause' in flight ? flight.cause : undefined, setup_error_reference: 'ref' in flight ? flight.ref : undefined, bytes_to_free: 'bytes_to_free' in flight ? flight.bytes_to_free : undefined };
}
function catalogRow(connection: string) {
	return { id: `${connection}/vendor-default`, litellm_model: `${connection}/vendor-default`, name: 'Vendor default', connection, provider: connection, backend: connection === 'omlx' ? 'network' : 'api', status: 'ready', is_default: true,
		...(['claude-code', 'codex'].includes(connection) ? { runs_on: [{ account_id: 'disposable-account', selected: true, status: 'ready' }] } : {}) };
}
async function serve(page: Page, fixture: Fixture) {
	let installed = false, apiTested = false, statusReads = 0, bindFailed = false, modelReads = 0;
	let releasePending: (() => void) | undefined;
	let pendingSettled: Promise<void> | undefined;
	const flight = localFlight(fixture.local);
	// Keep server-sent source updates out of a fixture that controls those states directly.
	// This fully replaces the browser transport, avoiding a live or intercepted socket
	// that can outlast an otherwise disposable capture context.
	await page.addInitScript(() => {
		class InertWebSocket extends EventTarget {
			static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
			readonly url: string; readyState = InertWebSocket.OPEN;
			onopen: ((event: Event) => void) | null = null;
			onclose: ((event: Event) => void) | null = null;
			onerror: ((event: Event) => void) | null = null;
			onmessage: ((event: MessageEvent) => void) | null = null;
			constructor(url: string) {
				super(); this.url = url;
				queueMicrotask(() => { const event = new Event('open'); this.onopen?.(event); this.dispatchEvent(event); });
			}
			send() { /* Fixture transport intentionally discards outbound events. */ }
			close() {
				if (this.readyState === InertWebSocket.CLOSED) return;
				this.readyState = InertWebSocket.CLOSED;
				const event = new CloseEvent('close'); this.onclose?.(event); this.dispatchEvent(event);
			}
		}
		Object.defineProperty(window, 'WebSocket', { configurable: true, writable: true, value: InertWebSocket });
	});
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
			if (fixture.screen === 'error') { await route.fulfill({ status: 500, json: {} }); return; }
			json = { apple_silicon: true, macos_supported: fixture.screen !== 'old-macos', app_location: fixture.screen === 'disk-image' ? 'disk_image' : 'applications', local_eligible: fixture.local !== 'memory', work_folder: '~/CORSIA', free_disk_gb: fixture.local === 'disk-space' ? 12 : 500, existing_omlx: fixture.other, required_username: fixture.requiredUsername, classifier: fixture.classifier === 'downloading' ? { state: 'downloading', bytes_done: 60, bytes_total: 100 } : fixture.classifier === 'failed' ? { state: 'failed', reason: 'Couldn’t download' } : undefined };
			if (fixture.screen === 'checking' && statusReads > 1) {
				const release = new Promise<void>((resolve) => { releasePending = resolve; });
				pendingSettled = release.then(() => route.fulfill({ json }));
				await pendingSettled;
				return;
			}
		} else if (path === '/api/setup/local-model') json = { state: fixture.local === 'pick-failed' ? 'failed' : 'picked', model: fixture.local === 'pick-failed' ? null : { name: 'Qwen 3.8', repository: 'example/model', download_size_bytes: 30000000000, download_size_gb: 30, context_window: 65536 }, message: null };
		else if (path === '/api/setup/local-download') {
			if (method === 'GET') json = { ...flight, model: 'Qwen 3.8', percent: 42, eta_seconds: 720 };
			else if (fixture.local === 'other-omlx') { await route.fulfill({ status: 409, json: { code: 'foreign_omlx_changed' } }); return; }
			else if (fixture.other) { await route.fulfill({ status: 409, json: { code: 'consent_required' } }); return; }
			else json = { ok: true };
		}
		else if (path === '/api/setup/classifier') json = fixture.classifier === 'downloading' ? { state: 'downloading', bytes_done: 60, bytes_total: 100 } : fixture.classifier === 'failed' ? { state: 'failed', reason: 'Couldn’t download' } : { state: 'ready' };
		else if (path === '/api/setup/first-account') {
			if (fixture.screen === 'creating') {
				const release = new Promise<void>((resolve) => { releasePending = resolve; });
				pendingSettled = release.then(() => route.fulfill({ json: {} }));
				await pendingSettled;
				return;
			}
			if (fixture.screen === 'account-exists') { await route.fulfill({ status: 409, json: {} }); return; }
			if (fixture.screen === 'create-failed') { await route.fulfill({ status: 500, json: {} }); return; }
			json = { token: 'disposable-frame-session', user: { id: 'frame-person', name: 'Alex Morgan' } };
		} else if (path === '/api/setup/progress') {
			if (method === 'PUT' && fixture.finish === 'progress-save') { await route.fulfill({ status: 500, json: {} }); return; }
			json = method === 'GET' ? { setup_completed: false, step: 'models' } : { ok: true };
		}
		else if (path === '/api/corsia-agent') json = { target: { thread_id: 'frame-corsia-agent' }, first_run_pending: true };
		else if (path === '/api/threads/frame-corsia-agent' && method === 'PATCH') {
			if (fixture.cli === 'bind-failed') { bindFailed = true; await route.fulfill({ status: 409, json: { detail: 'Model no longer ready' } }); return; }
			json = { model: 'claude-code/vendor-default', model_updated_at: '2026-10-19T17:00:00Z' };
		}
		else if (path.includes('/providers/sources')) json = { sources: [cliSource(bindFailed ? 'signed-out' : fixture.cli === 'bind-failed' ? 'ready' : fixture.cli === 'codex-ready' ? 'not-installed' : fixture.cli === 'claude-ready-codex-signed-out' ? 'ready' : installed && fixture.cli === 'signing-in' ? 'signed-out' : fixture.cli), cliSource(['both-ready', 'codex-ready'].includes(fixture.cli ?? '') ? 'ready' : fixture.cli === 'claude-ready-codex-signed-out' ? 'signed-out' : 'not-installed', 'codex'), localSource(fixture.local)] };
		else if (path === '/api/models') {
			modelReads += 1;
			json = { models: fixture.finish === 'no-model' && modelReads > 1 ? [] : [...(!bindFailed && ['ready', 'both-ready', 'claude-ready-codex-signed-out', 'bind-failed'].includes(fixture.cli ?? '') ? [catalogRow('claude-code')] : []), ...(['both-ready', 'codex-ready'].includes(fixture.cli ?? '') ? [catalogRow('codex')] : []), ...(fixture.local === 'ready' ? [catalogRow('omlx')] : []), ...(apiTested ? [catalogRow('anthropic')] : [])] };
		}
		else if (path === '/api/settings/providers') json = { providers: [{ name: 'anthropic', label: 'Anthropic', type: 'cloud', has_key: false, models: [], api_base: null, masked_key: null, docs_url: null }] };
		else if (path.endsWith('/anthropic/test')) { apiTested = fixture.api === 'ready'; json = fixture.api === 'ready' ? { ok: true, message: 'Key is valid' } : { ok: false, error: 'Couldn’t test this key. Check it and try again.' }; }
		else if (path.includes('/install')) {
			installed = true;
			json = fixture.cli === 'installing' ? { state: 'running' } : fixture.cli === 'shadow' ? { state: 'done', path_shadowed_by: '/usr/local/bin/claude' } : fixture.cli === 'install-connection' ? { state: 'failed', cause: 'connection' } : fixture.cli === 'install-failed' ? { state: 'failed', cause: 'download_unverified', ref: 'cli-fixture-123' } : { state: 'done' };
		} else if (path.includes('/signin') || path.includes('/sign-in')) json = fixture.cli === 'signin-failed' ? { status: 'failed' } : { status: 'waiting', account_id: 'frame-account' };
		else if (path.includes('/accounts')) json = { accounts: [], id: 'frame-account', account_id: 'frame-account' };
		else if (path === '/api/threads') json = { threads: [] };
		else if (path === '/api/projects') json = { projects: [] };
		await route.fulfill({ json });
	});
	return async () => {
		releasePending?.();
		if (pendingSettled) await pendingSettled;
	};
}
async function corsiaAgentThread(request: import('@playwright/test').APIRequestContext) {
	const response = await request.get('/api/corsia-agent', { params: { token: TEST_TOKEN } });
	expect(response.ok(), await response.text()).toBeTruthy();
	const body = await response.json();
	expect(body.target?.thread_id).toEqual(expect.any(String));
	return body.target.thread_id as string;
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
	if (fixture.finish) await page.getByRole('button', { name: 'Start using CORSIA', exact: true }).click();
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
	if (fixture.finish === 'no-model') { await expect(text('CORSIA couldn’t find a working model. Connect a provider or choose one that is ready, then Retry.')).toBeVisible(); return; }
	if (fixture.finish === 'progress-save') { await expect(text('CORSIA couldn’t save setup progress. Retry.')).toBeVisible(); return; }
	if (fixture.cli === 'codex-ready') { await expect(page.getByRole('region', { name: 'Codex' }).getByText('Ready')).toBeVisible(); return; }
	if (fixture.cli === 'claude-ready-codex-signed-out') { await expect(page.getByRole('region', { name: 'Codex' }).getByRole('button', { name: 'Sign in', exact: true })).toBeVisible(); return; }
	if (fixture.cli === 'install-connection') { const row = page.getByRole('region', { name: 'Claude Code' }); await expect(row).toContainText('Couldn’t download Claude Code. Check your internet connection, then retry.'); await expect(row.getByRole('button', { name: 'Save a report', exact: true })).toBeVisible(); }
	if (fixture.cli === 'install-failed') { const row = page.getByRole('region', { name: 'Claude Code' }); await expect(row).toContainText('Couldn’t install Claude Code. Retry, or save a report. Reference: cli-fixture-123'); await expect(row.getByRole('button', { name: 'Save a report', exact: true })).toBeVisible(); }
	if (fixture.cli && !['install-connection', 'install-failed'].includes(fixture.cli)) { await expect(page.getByRole('region', { name: 'Claude Code' })).toContainText(fixture.cli === 'ready' || fixture.cli === 'both-ready' ? 'Ready' : fixture.cli === 'signed-out' ? 'Sign in' : fixture.cli === 'not-installed' ? 'Set up' : fixture.cli === 'installing' ? 'Installing Claude Code…' : fixture.cli === 'signing-in' ? 'Finish signing in in your browser.' : fixture.cli === 'limited' ? 'usage limit' : fixture.cli === 'unusable' ? 'can’t use' : fixture.cli === 'shadow' ? 'older Claude Code' : 'Couldn’t'); }
	if (fixture.local) {
		const row = page.getByRole('region', { name: 'Local model' });
		const expected = fixture.local === 'ready' ? 'Ready'
			: fixture.local === 'memory' ? 'Local models need at least 24 GB of memory, and this computer has less. Connect a subscription or an API provider. Smaller local models may come in a future update.'
			: fixture.local === 'downloading' ? 'Downloading 42%'
			: fixture.local === 'paused' ? 'Download paused. It continues when the connection returns.'
			: fixture.local === 'starting' ? 'Starting the local model…'
			: fixture.local === 'disk-space' ? 'Needs 30 GB free, and this computer has 12 GB. Free up space, then check again.'
			: fixture.local === 'pick-failed' ? 'Couldn’t find a local model that fits this computer. Retry.'
			: fixture.local === 'connection' ? 'Couldn’t download the local model because the connection was lost. Check your internet connection, then Retry.'
			: fixture.local === 'disk-full' ? 'Couldn’t download the local model: the disk is full. Free up 5 GB, then Retry.'
			: fixture.local === 'memory' ? 'This computer doesn’t have enough memory to run the local model. Choose a subscription or API provider instead.'
			: fixture.local === 'other-ref' ? 'CORSIA couldn’t set up the local model. Retry. Reference: setup-report-123'
			: fixture.local === 'other-no-ref' ? 'CORSIA couldn’t set up the local model. Retry.'
			: fixture.local === 'setup-failed' ? 'CORSIA couldn’t set up the local model. Retry, or save a report.'
			: fixture.local === 'start-failed' ? 'The local model downloaded but couldn’t start. Retry, or save a report.'
			: fixture.local === 'other-omlx' ? 'Another oMLX is running. Quit your other oMLX, then Retry.'
			: 'Free and private. Runs on this computer, even offline. 30 GB download.';
		await expect(row).toContainText(expected);
		if (fixture.local === 'other-no-ref' || fixture.local === 'start-failed') await expect(row.getByText(/Reference:/)).toHaveCount(0);
		if (fixture.local === 'setup-failed' || fixture.local === 'start-failed') await expect(row.getByRole('button', { name: 'Save a report', exact: true })).toBeVisible();
	}
}
async function capture(page: Page, fixture: Fixture, theme: string, width: number, height: number) {
	page.setDefaultTimeout(10_000);
	const errors: string[] = [];
	page.on('pageerror', (error) => { errors.push(error.stack ?? error.message); });
	await page.setViewportSize({ width, height });
	await page.emulateMedia({ colorScheme: theme as 'light' | 'dark', reducedMotion: 'reduce' });
	const settleMockRequests = await serve(page, fixture); await navigate(page, fixture);
	await page.waitForTimeout(350);
	await assertState(page, fixture);
	await page.waitForFunction(() => document.getAnimations().every((animation) => animation.playState !== 'running'));
	expect(errors, `${fixture.name}: browser runtime errors`).toEqual([]);
	const file = `${fixture.name}-${theme}-${width}x${height}.png`;
	const path = resolve(assets!, file);
	await page.screenshot({ path, scale: 'css' });
	await settleMockRequests();
	records.push({ file, state: fixture.name, element: fixture.element, assertion: fixture.element, sha256: fileSha256(path) });
	console.log(`FRAME ${file}: ${fixture.element}`);
}

// The first-run frames turn Focus off; restore it even when a frame fails, so later specs render with it on.
test.afterEach(async ({ request }) => {
	await setFocusMode(request, true);
});

test('setup redesign complete state frames @setup-redesign', async ({ browser, request }, testInfo) => {
	test.skip(testInfo.project.name === 'chromium-mobile', 'The evidence pack fixes its own 1280×900 and 800×600 viewports; the mobile project duplicates the same captures.');
	test.setTimeout(600000);
	for (const theme of ['light', 'dark']) {
		for (const fixture of selectedFixtures) {
			const context = await browser.newContext(); const page = await context.newPage();
			await capture(page, fixture, theme, 1280, 900); await page.close(); await context.close();
		}
		for (const fixture of selectedFixtures.filter((row) => ['account-empty', 'folder', 'cli-ready'].includes(row.name))) {
			const context = await browser.newContext(); const page = await context.newPage();
			await capture(page, fixture, theme, 800, 600); await page.close(); await context.close();
		}
	}
	await setFocusMode(request, false);
	const emailQuestion = 'Which email do you use: Gmail (a work Gmail counts), Outlook, or something else?';
	const emailScenarios = [
		{ name: 'first-run-email-gmail', answer: 'Gmail', next: 'I can look after your email and calendar. To connect them, I\'ll set up your own private Google project in a browser you can watch, and you sign in and click Allow. Google will also ask for your Drive, Docs and Sheets, so you won\'t have to sign in again later. Two things to expect: Google will warn that the app isn\'t verified. That\'s normal for a private project like yours; choose "Advanced", then "Go to CORSIA (unsafe)". It\'s your own project, so you\'re the only one it can reach. And the project gets published as "In production", because a project left in Testing stops letting me in after 7 days. Go ahead?', profile: 'none' },
		{ name: 'first-run-email-outlook', answer: 'Outlook', next: 'I can\'t connect Outlook or other email yet. I\'ve noted yours, and I\'ll offer it here as soon as I can.', profile: '- **Email app:** Outlook — Outlook' },
		{ name: 'first-run-email-both', answer: 'Gmail and Outlook', next: 'I can look after your email and calendar. To connect them, I\'ll set up your own private Google project in a browser you can watch, and you sign in and click Allow. Google will also ask for your Drive, Docs and Sheets, so you won\'t have to sign in again later. Two things to expect: Google will warn that the app isn\'t verified. That\'s normal for a private project like yours; choose "Advanced", then "Go to CORSIA (unsafe)". It\'s your own project, so you\'re the only one it can reach. And the project gets published as "In production", because a project left in Testing stops letting me in after 7 days. Go ahead?', profile: '- **Email app:** Outlook — Gmail and Outlook' },
		{ name: 'first-run-email-other', answer: 'Proton Mail', next: 'I can\'t connect that email, but I\'ve noted which one you use.', profile: '- **Email app:** Other — Proton Mail' },
	] as const;
	const threadId = await corsiaAgentThread(request);
	const binding = await request.patch(`/api/threads/${threadId}`, { params: { token: TEST_TOKEN }, data: { model: 'mock/test-model', source: 'setup' } });
	expect(binding.ok(), await binding.text()).toBeTruthy();
	const opening = `I'm CORSIA, your assistant. My job is to give you back your time. I'm a bit different from most AI assistants: I measure how much of your time your work here takes, the AI agents working for you included, and I work to bring that number down. You can see it any time in Measurement. I keep everything here set up and running, and I take care of the everyday, like email, your calendar and research.

Think of it as a team you build around your problems. For a quick question, just start a chat. When something keeps taking your time, give it to an agent: it owns that problem, answers for the result and learns how you want it done. Some agents own a check that runs across the others, like reviewing work before it ships, so one shared process catches what any single agent would miss. When work needs its own context that nothing else should see, like a client or a set of rules, or its own way of working across several agents, it becomes a project: its agents share that context, and a conductor keeps them working together. Everything else stays with me. You don't have to work out which: tell me what's taking your time and I'll suggest the right help.

First, a few things to set up here, one at a time. Any of them can wait: say "not now" to one, or "skip the rest" to stop here.`;
	const openingLead = "I'm CORSIA, your assistant. My job is to give you back your time. I'm a bit different from most AI assistants: I measure how much of your time your work here takes, the AI agents working for you included, and I work to bring that number down. You can see it any time in Measurement. I keep everything here set up and running, and I take care of the everyday, like email, your calendar and research.";
	for (const theme of ['light', 'dark']) {
		const context = await browser.newContext({ colorScheme: theme as 'light' | 'dark', reducedMotion: 'reduce', viewport: { width: 1280, height: 900 } });
		const page = await context.newPage();
		await injectMessage(request, threadId, { role: 'assistant', content: opening }, TEST_TOKEN);
		await openAppByUrl(page);
		await expect(page.getByText(openingLead, { exact: true }).first()).toBeVisible();
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
		await page.waitForFunction((message) => {
			const composer = document.querySelector<HTMLElement>('[aria-label="Message CORSIA"]');
			const openingNode = [...document.querySelectorAll<HTMLElement>('*')].find((element) => element.textContent?.trim() === message);
			const opaque = (element: HTMLElement | null) => {
				for (let current = element; current; current = current.parentElement) if (Number.parseFloat(getComputedStyle(current).opacity) < 1) return false;
				return Boolean(element);
			};
			return opaque(composer) && opaque(openingNode) && document.getAnimations().every((animation) => animation.playState !== 'running');
		}, openingLead);
		await expect(page.getByText('Select Model', { exact: true })).toHaveCount(0);
		const file = `first-run-opening-${theme}-1280x900.png`;
		const path = resolve(assets!, file);
		await page.screenshot({ path, scale: 'css' });
		records.push({ file, state: 'first-run-opening-scripted', element: 'Scripted first-run opening and bound composer model (not Select Model)', assertion: 'scripted opening visible; bound composer visible; Select Model absent', sha256: fileSha256(path) });
		console.log(`FRAME ${file}: scripted first-run opening and bound composer model`);
		await context.close();
		for (const scenario of emailScenarios) {
			const emailContext = await browser.newContext({ colorScheme: theme as 'light' | 'dark', reducedMotion: 'reduce', viewport: { width: 1280, height: 900 } });
			const emailPage = await emailContext.newPage();
			await injectMessage(request, threadId, { role: 'assistant', content: emailQuestion }, TEST_TOKEN);
			await injectMessage(request, threadId, { role: 'user', content: scenario.answer }, TEST_TOKEN);
			await injectMessage(request, threadId, { role: 'assistant', content: scenario.next }, TEST_TOKEN);
			await openAppByUrl(emailPage);
			await expect(emailPage.getByText(emailQuestion, { exact: true }).last()).toBeVisible();
			await expect(emailPage.getByText(scenario.answer, { exact: true }).last()).toBeVisible();
			await expect(emailPage.getByText(scenario.next, { exact: true }).last()).toBeVisible();
			await emailPage.getByText(scenario.next, { exact: true }).last().scrollIntoViewIfNeeded();
			const emailFile = `${scenario.name}-${theme}-1280x900.png`;
			const emailPath = resolve(assets!, emailFile);
			await emailPage.screenshot({ path: emailPath, scale: 'css' });
			records.push({ file: emailFile, state: scenario.name, element: `Email app profile: ${scenario.profile}`, assertion: 'email step-5 question, answer, and next line visible', sha256: fileSha256(emailPath) });
			console.log(`FRAME ${emailFile}: ${scenario.profile}`);
			await emailContext.close();
		}
	}
	const manifest = resolve(assets!, 'frames.json');
	const previous: typeof records = existsSync(manifest) ? JSON.parse(readFileSync(manifest, 'utf8')) : [];
	const replacements = new Set(records.map((record) => record.file));
	writeFileSync(manifest, JSON.stringify([...previous.filter((record) => !replacements.has(record.file)), ...records], null, 2));
});
