import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';

const listeners = vi.hoisted(() => new Map<string, () => void>());
vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/ws/events', () => ({
	on: vi.fn((name: string, listener: () => void) => {
		listeners.set(name, listener);
		return () => listeners.delete(name);
	}),
}));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));

const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let installState: 'running' | 'done' | 'failed' = 'running';
let sourceState: 'not_installed' | 'ready' | 'unverified' | 'needs_auth' = 'not_installed';
let calls: string[];
let signinState: 'waiting' | 'completed' | 'failed' = 'waiting';
let localFailure = false;
let catalogModels: Record<string, unknown>[] = [];
let intervalSpy: ReturnType<typeof vi.spyOn>, clearIntervalSpy: ReturnType<typeof vi.spyOn>;

async function settle() { await vi.advanceTimersByTimeAsync(10); await tick(); }
function sourceCalls() { return calls.filter((call) => call === 'GET /api/providers/sources').length; }
function catalogCalls() { return calls.filter((call) => call.startsWith('GET /api/models?')).length; }
function installReads() { return calls.filter((call) => call === 'GET /api/providers/cli/codex/install').length; }
function signinReads() { return calls.filter((call) => call === 'POST /api/vendor-accounts/account-1/signin/check').length; }

function mountResume() {
	token.set('setup-token'); user.set({ id: 'user', name: 'Ada' });
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input), method = init?.method ?? 'GET'; calls.push(`${method} ${url}`);
		if (url === '/api/setup/progress') return json({ setup_completed: false, step: 'models', local_ai: false, status: { local_eligible: true, work_folder: '~/CORSIA' } });
		if (url === '/api/providers/sources' || url === '/api/providers/sources/recheck') return json({ sources: [
			{ source: 'codex', name: 'Codex', state: sourceState },
			...(localFailure ? [{ source: 'omlx', name: 'Local model', state: 'needs_setup', local_state: 'start_failed', setup_error_code: 'start_failed', fix: 'CORSIA couldn’t set up the local model.' }] : []),
		] });
		if (url.startsWith('/api/corsia-agent')) return json({ target: { thread_id: 'corsia-thread' } });
		if (url.startsWith('/api/models?')) return json({ models: catalogModels });
		if (url === '/api/settings/network-servers') return json({}, 500);
		if (url.startsWith('/api/vendor-accounts?')) return json({ accounts: [] });
		if (url === '/api/vendor-accounts' && method === 'POST') return json({ id: 'account-1' });
		if (url === '/api/vendor-accounts/account-1/signin/check' && method === 'POST') return json(signinState === 'failed' ? { status: 'failed', cause: 'vendor_status_failed', ref: 'signin-0123456789abcdef' } : { status: signinState });
		if (url === '/api/vendor-accounts/account-1/signin' && method === 'POST') return json({ status: 'waiting' });
		if (url === '/api/providers/cli/codex/install' && method === 'POST') return json({ state: 'running', reason: null, ref: null, path_shadowed_by: null });
		if (url === '/api/providers/cli/codex/install' && method === 'DELETE') return json({ state: 'cancelled', reason: null, ref: null, path_shadowed_by: null });
		if (url === '/api/providers/cli/codex/install') return json({ state: installState, reason: installState === 'failed' ? 'the installer stopped' : null, ref: installState === 'failed' ? 'cli-test' : null, path_shadowed_by: null });
		return json({});
	}));
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(SetupScreen, { target: host, props: { resume: true, onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
	flushSync();
}

function click(label: string) {
	const button = Array.from(host.querySelectorAll('button')).find((candidate) => candidate.textContent?.trim() === label);
	expect(button, host.textContent ?? '').toBeTruthy(); button?.click();
}

beforeEach(() => {
	vi.useFakeTimers(); listeners.clear(); calls = []; installState = 'running'; sourceState = 'not_installed'; signinState = 'waiting'; localFailure = false; catalogModels = [];
	intervalSpy = vi.spyOn(globalThis, 'setInterval'); clearIntervalSpy = vi.spyOn(globalThis, 'clearInterval');
});
afterEach(async () => {
	if (component) await unmount(component);
	component = null; host?.remove(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); token.set(null); user.set(null);
});

describe('SetupScreen owned-job polling', () => {
	it('does not create a poller when no setup-owned job is active', async () => {
		mountResume(); await settle();
		const sources = sourceCalls(), catalog = catalogCalls();
		expect(intervalSpy).not.toHaveBeenCalled();
		expect(vi.getTimerCount()).toBe(0);
		await vi.advanceTimersByTimeAsync(6000); await tick();
		expect(installReads()).toBe(0);
		expect(sourceCalls()).toBe(sources);
		expect(catalogCalls()).toBe(catalog);
	});

	it.each(['done', 'failed'] as const)('stops after a CLI job reaches %s', async (terminal) => {
		mountResume(); await settle(); click('Sign in'); await settle();
		expect(intervalSpy).toHaveBeenCalledOnce();
		installState = terminal; if (terminal === 'done') sourceState = 'unverified';
		await vi.advanceTimersByTimeAsync(3000); await tick();
		const readsAtTerminal = installReads();
		expect(clearIntervalSpy).toHaveBeenCalledOnce();
		expect(vi.getTimerCount()).toBe(0);
		await vi.advanceTimersByTimeAsync(6000); await tick();
		expect(installReads()).toBe(readsAtTerminal);
	});

	it('runs Save a report from a failed CLI row', async () => {
		const invoke = vi.fn(async () => undefined);
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke });
		mountResume(); await settle(); click('Sign in'); await settle();
		installState = 'failed';
		await vi.advanceTimersByTimeAsync(3000); await tick();
		click('Save a report'); await settle();
		expect(calls).toContain('POST /api/setup/report');
		// The stub service answers without details, and the report says so instead of saving blank.
		expect(invoke).toHaveBeenCalledWith('save_setup_report', { details: expect.stringContaining('couldn’t read the setup details') });
	});

	it.each([
		['completed', 'ready'],
		['failed', 'needs_auth'],
	] as const)('stops after sign-in reaches %s', async (terminal, nextSourceState) => {
		mountResume(); await settle(); click('Sign in'); await settle();
		installState = 'done'; sourceState = 'needs_auth';
		await vi.advanceTimersByTimeAsync(3000); await tick();
		expect(vi.getTimerCount()).toBe(1);
		signinState = terminal; sourceState = nextSourceState;
		await vi.advanceTimersByTimeAsync(3000); await tick();
		expect(signinReads()).toBe(1);
		expect(clearIntervalSpy).toHaveBeenCalledOnce();
		expect(vi.getTimerCount()).toBe(0);
		// A failed sign-in shows the reference its service-log line carries.
		if (terminal === 'failed') expect(host.textContent).toContain('Reference: signin-0123456789abcdef');
	});

	it('cleans up an active sign-in timer when the screen unmounts', async () => {
		mountResume(); await settle(); click('Sign in'); await settle();
		installState = 'done'; sourceState = 'needs_auth';
		await vi.advanceTimersByTimeAsync(3000); await tick();
		expect(vi.getTimerCount()).toBe(1);
		await unmount(component!); component = null;
		expect(clearIntervalSpy).toHaveBeenCalledOnce();
		expect(vi.getTimerCount()).toBe(0);
	});

	it('stops an install poller on unmount', async () => {
		mountResume(); await settle(); click('Sign in'); await settle();
		expect(vi.getTimerCount()).toBe(1);
		await unmount(component!); component = null;
		const readsAtUnmount = installReads();
		expect(clearIntervalSpy).toHaveBeenCalledOnce();
		expect(vi.getTimerCount()).toBe(0);
		await vi.advanceTimersByTimeAsync(3000); await tick();
		expect(installReads()).toBe(readsAtUnmount);
	});

	it('refreshes authenticated sources on models_updated, never from the CLI job timer', async () => {
		mountResume(); await settle(); click('Sign in'); await settle();
		const sourcesBeforeTimer = sourceCalls(), catalogBeforeTimer = catalogCalls();
		await vi.advanceTimersByTimeAsync(3000); await tick();
		expect(sourceCalls()).toBe(sourcesBeforeTimer);
		expect(catalogCalls()).toBe(catalogBeforeTimer);
		listeners.get('models_updated')?.(); await settle();
		expect(sourceCalls()).toBe(sourcesBeforeTimer + 1);
		expect(catalogCalls()).toBe(catalogBeforeTimer + 1);
	});

	it.each(['ready', 'failed', 'cancelled', 'start_failed'] as const)('stops after a pre-account local download reaches %s', async (terminal) => {
		token.set(null); user.set(null);
		let localState: string = 'downloading';
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input), method = init?.method ?? 'GET'; calls.push(`${method} ${url}`);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, work_folder: '~/CORSIA' });
			if (url === '/api/setup/local-model' && method === 'POST') return json({ state: 'picked', model: { name: 'Qwen', repository: 'qwen', download_size_bytes: 30_000_000_000, download_size_gb: 30, context_window: 8192 } });
			if (url === '/api/setup/local-download' && method === 'POST') return json({ state: 'downloading' });
			if (url === '/api/setup/local-download') return json({ state: localState, model: 'Qwen', ...(localState === 'start_failed' ? { cause: 'other', fix: 'CORSIA couldn’t set up the local model.' } : {}) });
			return json({});
		}));
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync(); await settle();
		expect(intervalSpy).toHaveBeenCalledOnce();
		localState = terminal;
		await vi.advanceTimersByTimeAsync(3000); await tick();
		if (terminal === 'start_failed') expect(host.textContent).toContain('CORSIA couldn’t set up the local model.');
		expect(clearIntervalSpy).toHaveBeenCalledOnce();
		expect(vi.getTimerCount()).toBe(0);
	});

	it('runs Save a report from a local start failure', async () => {
		const invoke = vi.fn(async () => undefined);
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke }); localFailure = true;
		mountResume(); await settle();
		click('Save a report'); await settle();
		// The stub service answers without details, and the report says so instead of saving blank.
		expect(invoke).toHaveBeenCalledWith('save_setup_report', { details: expect.stringContaining('couldn’t read the setup details') });
	});

	it('keeps a failed local turn-off on screen after the sources refresh', async () => {
		catalogModels = [{ id: 'local', connection: 'omlx', status: 'ready' }, { id: 'cloud', connection: 'anthropic', status: 'ready' }];
		mountResume(); await settle();
		(host.querySelector('button[aria-label="Local model"]') as HTMLButtonElement | null)?.click(); await settle();
		expect(calls).toContain('GET /api/settings/network-servers');
		// On the row whose switch retries it, never in the sources slot whose Retry only rechecks.
		expect(host.querySelector('#setup-row-detail-omlx')?.textContent).toBe('CORSIA couldn’t turn off the local model. Turn it off again, or save a report.');
	});

	it('cleans up a pre-account local download timer when the screen unmounts', async () => {
		token.set(null); user.set(null);
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input), method = init?.method ?? 'GET';
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, work_folder: '~/CORSIA' });
			if (url === '/api/setup/local-model' && method === 'POST') return json({ state: 'picked', model: { name: 'Qwen', repository: 'qwen', download_size_bytes: 30_000_000_000, download_size_gb: 30, context_window: 8192 } });
			if (url === '/api/setup/local-download' && method === 'POST') return json({ state: 'downloading' });
			if (url === '/api/setup/local-download') return json({ state: 'downloading', model: 'Qwen' });
			return json({});
		}));
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync(); await settle();
		expect(vi.getTimerCount()).toBe(1);
		await unmount(component); component = null;
		expect(clearIntervalSpy).toHaveBeenCalledOnce();
		expect(vi.getTimerCount()).toBe(0);
	});
});
