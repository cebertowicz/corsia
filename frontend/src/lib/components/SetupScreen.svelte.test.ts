import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SetupScreen from './SetupScreen.svelte';
import { connect } from '$lib/ws';
import { token, user } from '$lib/stores/auth';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));

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

describe('SetupScreen', () => {
	it('reads only setup status before showing the Welcome decision', async () => {
		await settle();
		expect(host.textContent).toContain('Welcome to CORSIA');
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
		expect(host.textContent).toContain('This computer can’t run a local model.');
		expect(host.textContent).not.toContain('This Mac can’t run a local model.');
	});

	it('keeps the setup shell mounted while Get started moves the local download into the rail', async () => {
		await settle();
		expect(host.textContent).toContain('Download Example model now');
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

	it('persists a protected native folder choice before creating the account', async () => {
		if (component) await unmount(component);
		const invoke = vi.fn(async (command: string) => command === 'choose_work_folder'
			? { state: 'ready', path: '/Users/me/Documents/corsia', protected_folder: 'Documents' }
			: undefined);
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke });
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync();
		await settle();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Change'))?.click();
		await settle();
		expect(host.textContent).toContain('/Users/me/Documents/corsia');
		expect(host.textContent).toContain('macOS will ask before CORSIA can use Documents.');
		for (const [id, value] of [['setup-name', 'Ada'], ['setup-username', 'ada'], ['setup-password', 'password1'], ['setup-confirm-password', 'password1']] as const) {
			const input = host.querySelector(`#${id}`) as HTMLInputElement;
			input.value = value;
			input.dispatchEvent(new Event('input', { bubbles: true }));
		}
		await settle();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Continue')?.click();
		await settle();
		expect(invoke).toHaveBeenNthCalledWith(1, 'choose_work_folder');
		expect(invoke).toHaveBeenNthCalledWith(2, 'configure_work_folder', { path: '/Users/me/Documents/corsia' });
		vi.mocked(connect).mockClear();
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

	it('prefills the one data owner read-only with the matching-data helper text', async () => {
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
		expect(host.textContent).toContain('This matches the existing data on this computer.');
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
			if (url === '/api/providers/sources/recheck') return json({ sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup' }] });
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
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Download')?.click();
		await settle();

		expect(host.textContent).toContain('CORSIA will read:');
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Use it')?.click();
		await settle();
		expect(host.textContent).toContain('Choose an AI model');
		expect(host.textContent).not.toContain('Create your account');
	});

	it('renders Claude Code and Codex beside an inert local download state', async () => {
		if (component) await unmount(component);
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, model_name: 'Qwen 3.8', model_size_gb: 30 });
			if (url === '/api/setup/local-download') return json({ in_progress: true });
			if (url === '/api/setup/first-account') return json({ token: 'setup-token', user: { id: 'setup-user', name: 'First Person' } });
			if (url === '/api/providers/sources/recheck') return json({ sources: [
				{ source: 'omlx', name: 'Local model', state: 'needs_setup', setup_downloading: true, download_percent: 42 },
				{ source: 'claude-code', name: 'Claude Code', state: 'needs_auth' },
				{ source: 'codex', name: 'Codex', state: 'needs_auth' },
			] });
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
		expect(host.textContent).toContain('Ready to use.');
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
});
