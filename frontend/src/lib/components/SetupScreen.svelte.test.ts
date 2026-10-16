import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SetupScreen from './SetupScreen.svelte';
import { connect } from '$lib/ws';
import { token, user } from '$lib/stores/auth';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
	status, headers: { 'Content-Type': 'application/json' },
});

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let calls: string[];

beforeEach(() => {
	token.set(null); user.set(null); calls = [];
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input); calls.push(url);
		if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, model_name: 'Example model', model_size_gb: 30, work_folder: '~/CORSIA' });
		if (url === '/api/setup/local-download') return json({ in_progress: true });
		return json({ sources: [] });
	}));
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
	flushSync();
});

afterEach(async () => {
	if (component) await unmount(component);
	host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

async function settle() { await tick(); await new Promise((done) => setTimeout(done)); await tick(); }

async function createAccountFromWelcome() {
	Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
	await settle();
	for (const [input, value] of [
		[host.querySelector('input[autocomplete="name"]'), 'First Person'],
		[host.querySelector('input[autocomplete="username"]'), 'first'],
		...Array.from(host.querySelectorAll('input[autocomplete="new-password"]')).map((input) => [input, 'correct-password']),
	] as [HTMLInputElement, string][]) {
		input.value = value;
		input.dispatchEvent(new Event('input', { bubbles: true }));
	}
	await tick();
	const primary = Array.from(host.querySelectorAll('button')).find((button) => ['Continue', 'Start using CORSIA'].includes(button.textContent?.trim() ?? ''));
	primary?.click();
	await settle();
}

describe('SetupScreen', () => {
	it('reads only setup status before showing the Welcome decision', async () => {
		await settle();
		expect(host.textContent).toContain('Welcome to CORSIA');
		expect(host.textContent).toContain(
			'Your own AI team, on this computer. It remembers your projects, works with the tools and models you already use, and shows you results, not transcripts.'
		);
		const mark = host.querySelector('img[src="/icons/corsia-mark.svg"]');
		expect(mark?.getAttribute('alt')).toBe('');
		expect(calls).toEqual(['/api/setup/status']);
	});

	it('shows existing oMLX consent before any adoption request, and Not now leaves it untouched', async () => {
		if (component) await unmount(component);
		calls = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input); calls.push(url);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, existing_omlx: true, model_name: 'Example model', model_size_gb: 30 });
			if (url === '/api/setup/local-download') return json({ in_progress: true });
			return json({ sources: [] });
		}));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync();
		await settle();

		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();
		expect(host.textContent).toContain('CORSIA will read:');
		expect(host.textContent).toContain('your oMLX port setting');
		expect(calls).toEqual(['/api/setup/status', '/api/setup/classifier']);

		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Not now')?.click();
		await settle();
		expect(calls).toEqual(['/api/setup/status', '/api/setup/classifier']);
		expect(host.textContent).toContain('Create your account');
	});

	it('starts existing oMLX adoption only after Use it consents', async () => {
		if (component) await unmount(component);
		calls = [];
		const bodies: string[] = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input); calls.push(url);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, existing_omlx: true, model_name: 'Example model', model_size_gb: 30 });
			if (url === '/api/setup/local-download') { bodies.push(String(init?.body)); return json({ in_progress: true }); }
			return json({ sources: [] });
		}));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync();
		await settle();

		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Use it')?.click();
		await settle();
		expect(calls).toEqual(['/api/setup/status', '/api/setup/classifier', '/api/setup/local-download']);
		expect(bodies).toEqual(['{"consent_existing":true}']);
	});

	it('shows the existing oMLX choice when local download reports consent required', async () => {
		if (component) await unmount(component);
		calls = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input); calls.push(url);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, existing_omlx: false, model_name: 'Example model', model_size_gb: 30 });
			if (url === '/api/setup/local-download') return json({ code: 'consent_required' }, 409);
			return json({ sources: [] });
		}));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync();
		await settle();

		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();

		expect(calls).toEqual(['/api/setup/status', '/api/setup/classifier', '/api/setup/local-download']);
		expect(host.textContent).toContain('CORSIA will read:');
		expect(host.textContent).not.toContain('Create your account');
	});

	it('uses This computer for the ineligible local-model explanation', async () => {
		if (component) await unmount(component);
		calls = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input); calls.push(url);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: false, model_size_gb: 30, work_folder: '~/CORSIA' });
			return json({ sources: [] });
		}));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync();

		await settle();
		expect(host.textContent).toContain('This computer doesn’t have enough memory to run an AI model itself. CORSIA will use Claude Code or Codex with your subscription, or an API key.');
		expect(host.textContent).not.toContain('Also run AI on this computer');
	});

	it('keeps the setup shell mounted while Get started moves the local download into the rail', async () => {
		await settle();
		expect(host.textContent).toContain('Also run AI on this computer');
		expect(host.textContent).toContain('Free, private, and works offline.');
		expect(host.textContent).toContain('Example model · 30 GB · runs in oMLX');
		expect(host.textContent).toContain('The download starts in the background when you press Get started.');
		expect(host.querySelector('button[aria-label="Also run AI on this computer"], [role="switch"][aria-label="Also run AI on this computer"]')).not.toBeNull();
		const shell = host.querySelector('.setup-shell');
		expect(shell).not.toBeNull();
		const action = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'));
		action?.click();
		await settle();
		expect(host.querySelector('.setup-shell')).toBe(shell);
		expect(calls).toContain('/api/setup/local-download');
		expect(host.textContent).toContain('Create your account');
		expect(host.textContent).toContain('In the background');
		expect(host.textContent).toContain('Example model');
		expect(host.textContent).toContain('Downloading');
	});

	it('shows the bootstrapped work folder without allowing it to change', async () => {
		await settle();
		const action = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'));
		action?.click();
		await settle();
		expect(host.textContent).toContain('Work folder');
		expect(host.textContent).toContain('~/CORSIA');
		expect(host.textContent).not.toContain('Change');
		expect(host.querySelector('[aria-label="Work folder"]')).toBeNull();
	});

	it('binds every account input to its visible label', async () => {
		await settle();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();

		for (const [id, name] of [
			['setup-name', 'Name'],
			['setup-username', 'Username'],
			['setup-password', 'Password'],
			['setup-confirm-password', 'Confirm password'],
			['setup-email', 'Email (optional)'],
		]) {
			const input = host.querySelector(`#${id}`) as HTMLInputElement;
			expect(input.labels?.[0]?.textContent).toBe(name);
		}
	});

	it('does not request a local download when free disk is below the selected model size', async () => {
		if (component) await unmount(component);
		calls = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input); calls.push(url);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, free_disk_gb: 20, model_name: 'Example model', model_size_gb: 30, work_folder: '~/CORSIA' });
			if (url === '/api/setup/local-download') return json({ in_progress: true });
			return json({ sources: [] });
		}));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync();

		await settle();
		expect(host.textContent).toContain('Needs 30 GB free; 20 GB is free.');
		expect(host.textContent).toContain('Free up space, then check again');
		const action = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'));
		action?.click();
		await settle();
		expect(calls).not.toContain('/api/setup/local-download');
		expect(host.textContent).toContain('Create your account');
	});

	it('prefills the one data owner read-only, with no helper caption', async () => {
		if (component) await unmount(component);
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, model_size_gb: 30, work_folder: '~/CORSIA', required_username: 'legacy-owner' });
			return json({ sources: [] });
		}));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync();
		await settle();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();

		const username = host.querySelector('input[autocomplete="username"]') as HTMLInputElement;
		expect(username.value).toBe('legacy-owner');
		expect(username.readOnly).toBe(true);
		expect(host.textContent).not.toContain('This matches the existing data on this computer.');
	});

	it('leaves the username editable with no helper text on a fresh install', async () => {
		await settle();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();
		const username = host.querySelector('input[autocomplete="username"]') as HTMLInputElement;
		expect(username.value).toBe('');
		expect(username.readOnly).toBe(false);
		expect(host.textContent).not.toContain('This matches the existing data');
	});

	it('validates usernames before submitting the account form', async () => {
		await settle();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();
		const username = host.querySelector('input[autocomplete="username"]') as HTMLInputElement;
		username.value = 'Not valid';
		username.dispatchEvent(new Event('input', { bubbles: true }));
		await tick();

		expect(host.textContent).toContain("Use 1–32 lowercase letters, numbers, '-' or '_'.");
		expect(Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Continue'))).toHaveProperty('disabled', true);
	});

	it('keeps a safe server account error inline on the account form', async () => {
		await settle();
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input); calls.push(url);
			if (url === '/api/setup/first-account') return json({ detail: 'username is already in use' }, 422);
			if (url === '/api/setup/local-download') return json({ in_progress: true });
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, model_name: 'Example model', model_size_gb: 30, work_folder: '~/CORSIA' });
			return json({ sources: [] });
		}));
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();
		for (const [input, value] of [
			[host.querySelector('input[autocomplete="name"]'), 'First Person'],
			[host.querySelector('input[autocomplete="username"]'), 'first'],
			...Array.from(host.querySelectorAll('input[autocomplete="new-password"]')).map((input) => [input, 'correct-password']),
		] as [HTMLInputElement, string][]) {
			input.value = value;
			input.dispatchEvent(new Event('input', { bubbles: true }));
		}
		await tick();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Continue'))?.click();
		await settle();

		expect(host.textContent).toContain('username is already in use');
		expect(host.textContent).toContain('Create your account');
		expect(host.textContent).not.toContain('CORSIA couldn’t start');
		expect(Array.from(host.querySelectorAll('input[autocomplete="new-password"]')).map((input) => (input as HTMLInputElement).value)).toEqual(['correct-password', 'correct-password']);
	});

	it('opens the live socket once the first account holds a session', async () => {
		await settle();
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url === '/api/setup/first-account') return json({ token: 'session-token', user: { id: 'setup-user', name: 'First Person' } });
			if (url === '/api/setup/local-download') return json({ in_progress: true });
			return json({ sources: [] });
		}));
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();
		for (const [input, value] of [
			[host.querySelector('input[autocomplete="name"]'), 'First Person'],
			[host.querySelector('input[autocomplete="username"]'), 'first'],
			...Array.from(host.querySelectorAll('input[autocomplete="new-password"]')).map((input) => [input, 'correct-password']),
		] as [HTMLInputElement, string][]) {
			input.value = value;
			input.dispatchEvent(new Event('input', { bubbles: true }));
		}
		await tick();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Continue'))?.click();
		await settle();

		expect(connect).toHaveBeenCalledTimes(1);
	});

	it('returns to the existing oMLX choice when the signed-in source start needs consent', async () => {
		if (component) await unmount(component);
		let localSetupStarts = 0;
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: false, model_name: 'Example model', model_size_gb: 30 });
			if (url === '/api/setup/first-account') return json({ token: 'session-token', user: { id: 'setup-user', name: 'First Person' } });
			if (url === '/api/providers/sources/recheck') return json({ sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup', setup_error: 'Download failed' }] });
			if (url === '/api/providers/local/setup') {
				localSetupStarts += 1;
				return localSetupStarts === 1 ? json({ detail: { code: 'consent_required' } }, 409) : json({ ok: true });
			}
			return json({ sources: [] });
		}));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync();
		await settle();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();
		for (const [input, value] of [
			[host.querySelector('input[autocomplete="name"]'), 'First Person'],
			[host.querySelector('input[autocomplete="username"]'), 'first'],
			...Array.from(host.querySelectorAll('input[autocomplete="new-password"]')).map((input) => [input, 'correct-password']),
		] as [HTMLInputElement, string][]) {
			input.value = value;
			input.dispatchEvent(new Event('input', { bubbles: true }));
		}
		await tick();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Continue'))?.click();
		await settle();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Try again')?.click();
		await settle();

		expect(host.textContent).toContain('CORSIA will read:');
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Use it')?.click();
		await settle();
		expect(host.textContent).toContain('Connect an AI');
		expect(host.textContent).not.toContain('Create your account');
	});

	it('renders Claude Code and Codex beside an inert local download state', async () => {
		if (component) await unmount(component);
		const calls: string[] = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			calls.push(url);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, model_name: 'Qwen 3.8', model_size_gb: 30 });
			if (url === '/api/setup/local-download') return json({ in_progress: true });
			if (url === '/api/setup/first-account') return json({ token: 'setup-token', user: { id: 'setup-user', name: 'First Person' } });
			if (url === '/api/providers/sources/recheck') return json({ sources: [
				{ source: 'omlx', name: 'Local model', state: 'needs_setup', setup_downloading: true, download_percent: 42 },
				{ source: 'claude-code', name: 'Claude Code', state: 'needs_auth' },
				{ source: 'codex', name: 'Codex', state: 'needs_auth' },
			] });
			if (url.startsWith('/api/corsia-agent?token=')) return json({ target: { thread_id: 'corsia-agent' } });
			if (url === '/api/vendor-accounts?provider=claude-code') return json({ accounts: [{ id: 'account-1', is_default: false, state: 'signed_out' }] });
			if (url === '/api/vendor-accounts/account-1/signin') return json({ state: 'pending' });
			return json({ sources: [] });
		}));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync();
		await settle();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();
		for (const [input, value] of [
			[host.querySelector('input[autocomplete="name"]'), 'First Person'],
			[host.querySelector('input[autocomplete="username"]'), 'first'],
			...Array.from(host.querySelectorAll('input[autocomplete="new-password"]')).map((input) => [input, 'correct-password']),
		] as [HTMLInputElement, string][]) {
			input.value = value;
			input.dispatchEvent(new Event('input', { bubbles: true }));
		}
		await tick();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Continue'))?.click();
		await settle();
		const text = host.textContent ?? '';
		expect(text).toContain('Claude Code');
		expect(text).toContain('Codex');
		expect(text).toContain('Downloading in the background');
		expect(host.querySelector('[aria-label="Setup progress"]')?.textContent).toContain('42%');
		expect(text).toContain('Ready when the download finishes. You can close this window; CORSIA keeps going.');
		expect(Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Start using CORSIA'))).toHaveProperty('disabled', true);
		expect(Array.from(host.querySelectorAll('button')).some((button) => button.textContent?.trim() === 'Downloading')).toBe(false);
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Sign in')?.click();
		await settle();
		expect(calls.some((url) => url.startsWith('/api/corsia-agent?token='))).toBe(true);
		expect(calls).not.toContain('/api/conductor');
	});

	async function reachModels(classifier: unknown, options: { onRequest?: (url: string, init?: RequestInit) => void; sourceReady?: boolean; onFinished?: () => void; posts?: Array<unknown | (() => Promise<unknown>)> } = {}) {
		const { onRequest, sourceReady = true, onFinished = vi.fn(), posts = [] } = options;
		const queue = [...posts];
		if (component) await unmount(component);
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			onRequest?.(url, init);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, model_name: 'Qwen 3.8', model_size_gb: 30 });
			if (url === '/api/setup/local-download') return json({ in_progress: true });
			if (url === '/api/setup/classifier/recheck') return json(classifier);
			if (url === '/api/setup/classifier') {
				if (init?.method !== 'POST' || queue.length === 0) return json(classifier);
				const next = queue.shift();
				return json(typeof next === 'function' ? await (next as () => Promise<unknown>)() : next);
			}
			if (url === '/api/setup/first-account') return json({ token: 'session-token', user: { id: 'setup-user', name: 'First Person' } });
			if (url === '/api/providers/sources/recheck') return json({ sources: [
				{ source: 'omlx', name: 'Local model', state: 'needs_setup', setup_stage: 'Installing', download_percent: null },
				{ source: 'claude-code', name: 'Claude Code', state: sourceReady ? 'ready' : 'needs_auth', accounts: [{ state: sourceReady ? 'ready' : 'needs_auth' }] },
			] });
			return json({ sources: [] });
		}));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished } });
		flushSync();
		await settle();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();
		for (const [input, value] of [
			[host.querySelector('input[autocomplete="name"]'), 'First Person'],
			[host.querySelector('input[autocomplete="username"]'), 'first'],
			...Array.from(host.querySelectorAll('input[autocomplete="new-password"]')).map((input) => [input, 'correct-password']),
		] as [HTMLInputElement, string][]) {
			input.value = value;
			input.dispatchEvent(new Event('input', { bubbles: true }));
		}
		await tick();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Continue'))?.click();
		await settle();
	}
	const startButton = () => Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Start using CORSIA'));
	const rail = () => host.querySelector('[aria-label="Setup progress"]')?.textContent ?? '';

	it('enables Start when a source is ready beside a local download', async () => {
		await reachModels({ state: 'idle' });

		expect(host.textContent).toContain('Installing');
		expect(host.textContent).toContain('Ready');
		expect(startButton()).toHaveProperty('disabled', false);
	});

	it('shows the background download only in the rail, as Finishing setup with its percent', async () => {
		await reachModels({ state: 'downloading', bytes_done: 250, bytes_total: 1000 });

		expect(rail()).toContain('In the background');
		expect(rail()).toContain('Finishing setup');
		expect(rail()).toContain('Downloading · 25%');
		expect(host.textContent).not.toMatch(/classifier|learns from/i);
	});

	it('never makes Start wait for the download', async () => {
		const onFinished = vi.fn();
		await reachModels({ state: 'downloading', bytes_done: 250, bytes_total: 1000 }, { onFinished });

		expect(startButton()).toHaveProperty('disabled', false);
		startButton()?.click();
		await settle();
		expect(onFinished).toHaveBeenCalledOnce();
	});

	const failedSpace = { state: 'failed', code: 'disk_full', reason: 'Needs 0.9 GB more free space.', retryable: true, bytes_needed: 872206596 };
	const railButtons = () => Array.from(host.querySelectorAll('[aria-label="Setup progress"] button'));

	it('shows a failed download as a red dot and a message in secondary text, with Try again as the only click target', async () => {
		const posts: string[] = [];
		await reachModels(failedSpace, { onRequest: (url, init) => { if (url === '/api/setup/classifier' && init?.method === 'POST') posts.push(url); } });
		expect(rail()).toContain('Finishing setup');
		expect(rail()).toContain('Needs 0.9 GB more free space.');
		expect(rail()).not.toMatch(/try again.*try again|free space\.\s*Try again\./i);
		expect(host.textContent).not.toMatch(/classifier|learns from/i);
		expect(startButton()).toHaveProperty('disabled', false);
		expect(host.querySelector('.reddot')).not.toBeNull();
		expect(host.querySelector('[aria-label="Setup progress"] .row.error.has-action')).not.toBeNull();

		// The row is not a button; its fix is a real button with its own name and label.
		expect(railButtons().filter((button) => button.textContent?.includes('Finishing setup'))).toHaveLength(0);
		const retry = host.querySelector<HTMLButtonElement>('[aria-label="Try again: Finishing setup"]');
		expect(retry?.tagName).toBe('BUTTON');
		expect(retry?.textContent?.trim()).toBe('Try again');
		expect(railButtons().filter((button) => button.getAttribute('aria-label') === 'Try again: Finishing setup')).toHaveLength(1);
		const before = posts.length;
		retry?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
		await settle();

		expect(posts.length).toBe(before + 1);
	});

	it('goes back to the spinner and Downloading the moment Try again is pressed, never red while it runs', async () => {
		let release!: (state: unknown) => void;
		const held = new Promise<unknown>((resolve) => { release = resolve; });
		await reachModels(failedSpace, { posts: [failedSpace, () => held] });
		expect(host.querySelector('.reddot')).not.toBeNull();

		host.querySelector<HTMLButtonElement>('[aria-label="Try again: Finishing setup"]')?.click();
		await settle();

		expect(host.querySelector('.reddot')).toBeNull();
		expect(host.querySelector('[aria-label="Try again: Finishing setup"]')).toBeNull();
		expect(rail()).toContain('Downloading');
		release({ state: 'downloading', bytes_done: 20, bytes_total: 1000 });
		await settle();
		expect(rail()).toContain('Downloading · 2%');
		expect(host.querySelector('.reddot')).toBeNull();
	});

	it('looks again for room when the window regains focus, only while the download is stopped for space', async () => {
		const rechecks: string[] = [];
		const onRequest = (url: string) => { if (url === '/api/setup/classifier/recheck') rechecks.push(url); };
		await reachModels(failedSpace, { onRequest });
		window.dispatchEvent(new Event('focus'));
		await settle();
		expect(rechecks).toEqual(['/api/setup/classifier/recheck']);

		rechecks.length = 0;
		await reachModels({ state: 'failed', code: 'checksum_failed', reason: 'The download was damaged.', retryable: true }, { onRequest });
		window.dispatchEvent(new Event('focus'));
		await settle();
		await reachModels({ state: 'downloading', bytes_done: 1, bytes_total: 1000 }, { onRequest });
		window.dispatchEvent(new Event('focus'));
		await settle();
		expect(rechecks).toEqual([]);
	});

	it('leaves the rail free of it once the download is ready', async () => {
		await reachModels({ state: 'ready' });

		expect(rail()).not.toContain('Finishing setup');
		expect(host.textContent).not.toMatch(/classifier|learns from/i);
	});

	it('skips the AI step when a source is already Ready, with Start using CORSIA on the account step', async () => {
		if (component) await unmount(component);
		const onFinished = vi.fn();
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, model_name: 'Qwen 3.8', model_size_gb: 30, ready_ai: 'Claude Code' });
			if (url === '/api/setup/local-download') return json({ in_progress: true });
			if (url === '/api/setup/first-account') return json({ token: 'session-token', user: { id: 'setup-user', name: 'First Person' } });
			if (url === '/api/providers/sources/recheck') return json({ sources: [
				{ source: 'omlx', name: 'Local model', state: 'needs_setup', setup_stage: 'Installing', download_percent: null },
				{ source: 'claude-code', name: 'Claude Code', state: 'ready', accounts: [{ state: 'ready' }] },
			] });
			return json({ sources: [] });
		}));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished } });
		flushSync();
		await settle();
		await createAccountFromWelcome();

		expect(onFinished).toHaveBeenCalledTimes(1);
		expect(host.textContent).not.toContain('Connect an AI');
	});

	it('names the ready source in the sidebar and reads Start using CORSIA before the account exists', async () => {
		if (component) await unmount(component);
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, model_name: 'Qwen 3.8', model_size_gb: 30, ready_ai: 'Claude Code' });
			if (url === '/api/setup/local-download') return json({ in_progress: true });
			return json({ sources: [] });
		}));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync();
		await settle();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();

		expect(host.querySelector('[aria-label="Setup progress"]')?.textContent).toContain('Claude Code');
		const buttons = Array.from(host.querySelectorAll('button'));
		expect(buttons.some((button) => button.textContent?.trim() === 'Start using CORSIA')).toBe(true);
		expect(buttons.some((button) => button.textContent?.trim() === 'Continue')).toBe(false);
	});

	describe('Connect an AI', () => {
		type Fake = {
			sources: Record<string, unknown>[];
			installPost: { status: number; body: unknown };
			install: { state: string; reason: string | null };
			signin: string;
			posted: string[];
		};
		let fake: Fake;
		const claude = (state: string, extra: Record<string, unknown> = {}) => ({ source: 'claude-code', name: 'Claude Code', state, ...extra });

		async function openConnectStep(sources: Record<string, unknown>[]) {
			if (component) await unmount(component);
			fake = { sources, installPost: { status: 200, body: { ok: true } }, install: { state: 'running', reason: null }, signin: 'waiting', posted: [] };
			vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
				const url = String(input);
				const method = init?.method ?? 'GET';
				fake.posted.push(`${method} ${url}`);
				if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: false, model_name: 'Qwen 3.8', model_size_gb: 30 });
				if (url === '/api/setup/first-account') return json({ token: 'session-token', user: { id: 'setup-user', name: 'First Person' } });
				if (url === '/api/providers/sources/recheck' || url === '/api/providers/sources') return json({ sources: fake.sources });
				if (url === '/api/providers/cli/claude-code/install' && method === 'POST') return json(fake.installPost.body, fake.installPost.status);
				if (url === '/api/providers/cli/claude-code/install') return json(fake.install);
				if (url === '/api/providers/cli/claude-code/link') return json({ ok: true, outcome: 'linked' });
				if (url.startsWith('/api/corsia-agent?token=')) return json({ target: { thread_id: 'conductor-1' } });
				if (url.startsWith('/api/vendor-accounts?provider=')) return json({ accounts: [] });
				if (url === '/api/vendor-accounts') return json({ id: 'acct-1' });
				if (url === '/api/vendor-accounts/acct-1/signin') return json({ account_id: 'acct-1', status: 'waiting' });
				if (url === '/api/vendor-accounts/acct-1/signin/check') return json({ account_id: 'acct-1', status: fake.signin });
				return json({ sources: [] });
			}));
			component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
			flushSync();
			await settle();
			await createAccountFromWelcome();
		}

		const row = (name: string) => host.querySelector(`section[aria-label="${name}"]`) as HTMLElement;
		const press = async (scope: ParentNode, label: string) => {
			Array.from(scope.querySelectorAll('button')).find((button) => button.textContent?.trim() === label)?.click();
			await settle();
		};
		const refocus = async () => { window.dispatchEvent(new Event('focus')); await settle(); };
		const start = () => Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Start using CORSIA') as HTMLButtonElement;

		it('shows one action per row with no Check again, and Start stays off until a source is Ready', async () => {
			await openConnectStep([claude('not_installed'), { source: 'codex', name: 'Codex', state: 'not_installed' }]);

			expect(host.textContent).toContain('Connect an AI');
			expect(row('Claude Code').textContent).toContain('CORSIA installs Claude Code for you.');
			expect(Array.from(row('Claude Code').querySelectorAll('button')).map((button) => button.textContent?.trim())).toEqual(['Set up']);
			expect(Array.from(row('Codex').querySelectorAll('button')).map((button) => button.textContent?.trim())).toEqual(['Set up']);
			expect(host.textContent).not.toContain('Check again');
			expect(host.querySelector('[aria-label="Setup progress"]')?.textContent).toContain('Your AI');
			expect(start().disabled).toBe(true);
		});

		it('installs through the official installer after a confirm line, waits with up to 3 minutes, then signs in in place', async () => {
			await openConnectStep([claude('not_installed')]);

			await press(row('Claude Code'), 'Set up');
			expect(row('Claude Code').textContent).toContain('Set up Claude Code? This runs Anthropic’s official installer, downloaded from claude.ai, and then you sign in to your Claude account.');
			expect(fake.posted).not.toContain('POST /api/providers/cli/claude-code/install');

			await press(row('Claude Code'), 'Set up');
			expect(fake.posted).toContain('POST /api/providers/cli/claude-code/install');
			expect(row('Claude Code').textContent).toContain('This can take up to 3 minutes.');
			expect(row('Claude Code').querySelectorAll('button').length).toBe(0);

			// The installer finishes; coming back to the window notices it and opens sign-in in place.
			fake.install = { state: 'done', reason: null };
			fake.sources = [claude('needs_auth')];
			await refocus();
			expect(fake.posted).toContain('POST /api/vendor-accounts/acct-1/signin');
			expect(row('Claude Code').textContent).toContain('Installed. Finish signing in to your Claude account in the browser window that just opened.');
			expect(Array.from(row('Claude Code').querySelectorAll('button')).map((button) => button.textContent?.trim())).toEqual(['Open sign-in again']);
			expect(start().disabled).toBe(true);

			fake.signin = 'completed';
			fake.sources = [claude('ready', { accounts: [{ state: 'ready' }] })];
			await refocus();
			expect(row('Claude Code').textContent).toContain('Ready');
			expect(start().disabled).toBe(false);
		});

		it('offers Sign in for a signed-out CLI, and signs in without installing', async () => {
			await openConnectStep([claude('needs_auth')]);

			expect(row('Claude Code').textContent).toContain('Claude Code is installed. Sign in to use it.');
			expect(Array.from(row('Claude Code').querySelectorAll('button')).map((button) => button.textContent?.trim())).toEqual(['Sign in']);

			await press(row('Claude Code'), 'Sign in');
			expect(fake.posted.some((call) => call.includes('/install'))).toBe(false);
			expect(fake.posted).toContain('POST /api/vendor-accounts/acct-1/signin');
			expect(row('Claude Code').textContent).toContain('Finish signing in to your Claude account in the browser window that just opened.');
			expect(row('Claude Code').textContent).not.toContain('Installed.');
		});

		it('shows an install failure with its reason, Try again and the install page', async () => {
			const { openExternalUrl } = await import('$lib/linkTargetResolver');
			await openConnectStep([claude('not_installed')]);
			await press(row('Claude Code'), 'Set up');
			fake.install = { state: 'failed', reason: 'no internet connection' };
			await press(row('Claude Code'), 'Set up');

			expect(row('Claude Code').textContent).toContain('Couldn’t install Claude Code: no internet connection. Check your connection, then choose Try again.');
			expect(Array.from(row('Claude Code').querySelectorAll('button')).map((button) => button.textContent?.trim())).toEqual(['Try again', 'Open the install page']);

			await press(row('Claude Code'), 'Open the install page');
			expect(openExternalUrl).toHaveBeenCalledWith('https://code.claude.com/docs/en/setup');

			fake.install = { state: 'running', reason: null };
			await press(row('Claude Code'), 'Try again');
			expect(row('Claude Code').textContent).toContain('This can take up to 3 minutes.');
		});

		it('treats an already installed CLI as a stale row, not a failure', async () => {
			await openConnectStep([claude('not_installed')]);
			fake.installPost = { status: 409, body: { reason: 'already_installed' } };
			fake.sources = [claude('needs_auth')];
			await press(row('Claude Code'), 'Set up');
			await press(row('Claude Code'), 'Set up');

			expect(fake.posted).toContain('POST /api/vendor-accounts/acct-1/signin');
			expect(row('Claude Code').textContent).not.toContain('Couldn’t install');
		});

		it('links a CLI the service cannot see, then goes on to sign in', async () => {
			await openConnectStep([claude('not_installed', { invisible_binary: '/opt/example/bin/claude' })]);
			fake.installPost = { status: 409, body: { reason: 'invisible_binary' } };
			fake.sources = [claude('needs_auth')];
			await press(row('Claude Code'), 'Set up');
			await press(row('Claude Code'), 'Set up');

			expect(fake.posted).toContain('POST /api/providers/cli/claude-code/link');
			expect(fake.posted).toContain('POST /api/vendor-accounts/acct-1/signin');
		});

		it('rechecks the sources when the window regains focus', async () => {
			await openConnectStep([claude('needs_auth')]);
			const before = fake.posted.filter((call) => call === 'POST /api/providers/sources/recheck').length;
			fake.sources = [claude('ready', { accounts: [{ state: 'ready' }] })];
			await refocus();

			expect(fake.posted.filter((call) => call === 'POST /api/providers/sources/recheck').length).toBe(before + 1);
			expect(row('Claude Code').textContent).toContain('Ready');
		});
	});
});
