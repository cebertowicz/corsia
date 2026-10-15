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
		expect(calls).toEqual(['/api/setup/status']);

		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Not now')?.click();
		await settle();
		expect(calls).toEqual(['/api/setup/status']);
		expect(host.textContent).toContain('Create your account');
	});

	it('returns a signed-in person to another model source after Not now', async () => {
		if (component) await unmount(component);
		calls = [];
		token.set('signed-in');
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input); calls.push(url);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, existing_omlx: true, model_name: 'Example model', model_size_gb: 30 });
			if (url === '/api/providers/local/setup') return json({ code: 'replace_consent_required' }, 409);
			if (url === '/api/providers/sources/recheck') return json({ sources: [
				{ source: 'omlx', name: 'Local model', state: 'needs_setup' },
				{ source: 'claude-code', name: 'Claude Code', state: 'needs_auth' },
			] });
			return json({ sources: [] });
		}));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync();
		await settle();

		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();
		expect(Array.from(host.querySelectorAll('.row.done')).some((row) => row.textContent?.includes('Your account'))).toBe(true);
		expect(host.querySelectorAll('[aria-current="step"]')).toHaveLength(1);
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Use it')?.click();
		await settle();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Not now')?.click();
		await settle();

		expect(calls).toContain('/api/providers/local/setup');
		expect(host.textContent).toContain('Choose an AI model');
		expect(host.textContent).toContain('Claude Code');
		expect(host.textContent).toContain("oMLX doesn't start");
		expect(host.textContent).toContain('Reinstall');
		expect(host.textContent).not.toContain('Create your account');
	});

	it('keeps one current rail mark and shows repair in the rail as soon as Reinstall starts', async () => {
		if (component) await unmount(component);
		let downloads = 0;
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, existing_omlx: true, model_name: 'Example model', model_size_gb: 30 });
			if (url === '/api/setup/local-download') {
				downloads += 1;
				return downloads === 1 ? json({ code: 'replace_consent_required' }, 409) : json({ ok: true, in_progress: true });
			}
			return json({ sources: [] });
		}));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync();
		await settle();

		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();
		expect(Array.from(host.querySelectorAll('.row.done')).some((row) => row.textContent?.includes('Welcome'))).toBe(true);
		expect(host.querySelectorAll('.row.current')).toHaveLength(1);

		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Use it')?.click();
		await settle();
		expect(host.querySelectorAll('.row.current')).toHaveLength(1);
		expect(host.querySelector('.row.current')?.textContent).toContain('AI model');

		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Reinstall')?.click();
		await settle();
		expect(host.textContent).toContain('Create your account');
		expect(host.querySelector('.job-region')?.textContent).toContain('Example model');
		expect(host.querySelectorAll('.row.current')).toHaveLength(1);
		expect(host.querySelector('.row.current')?.textContent).toContain('Your account');
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
		expect(calls).toEqual(['/api/setup/status', '/api/setup/local-download']);
		expect(bodies).toEqual(['{"consent_existing":true}']);
	});

	it('shows replacement consent copy exactly once after reuse finds a broken oMLX', async () => {
		if (component) await unmount(component);
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input); calls.push(url);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, existing_omlx: true, model_name: 'Example model', model_size_gb: 30 });
			if (url === '/api/setup/local-download') return json({ code: 'replace_consent_required' }, 409);
			return json({ sources: [] });
		}));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync();
		await settle();

		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'))?.click();
		await settle();
		Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Use it')?.click();
		await settle();

		const replacementCopy = 'CORSIA can reinstall oMLX and download Example model (30 GB). Reinstalling replaces that oMLX and changes its settings.';
		expect(Array.from(host.querySelectorAll('p')).filter((paragraph) => paragraph.textContent?.trim() === replacementCopy)).toHaveLength(1);
		expect(host.querySelector('.consent-actions button')?.textContent?.trim()).toBe('Not now');
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

		expect(calls).toEqual(['/api/setup/status', '/api/setup/local-download']);
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
		expect(text).toContain('Downloading');
		expect(text).toContain('Ready when the download finishes. You can close this window; CORSIA keeps going.');
		expect(Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Start using CORSIA'))).toHaveProperty('disabled', true);
		expect(Array.from(host.querySelectorAll('button')).some((button) => button.textContent?.trim() === 'Downloading')).toBe(false);
	});

	it('enables Start only when a source is ready beside a local download', async () => {
		if (component) await unmount(component);
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, model_name: 'Qwen 3.8', model_size_gb: 30 });
			if (url === '/api/setup/local-download') return json({ in_progress: true });
			if (url === '/api/setup/first-account') return json({ token: '[REDACTED]', user: { id: 'setup-user', name: 'First Person' } });
			if (url === '/api/providers/sources/recheck') return json({ sources: [
				{ source: 'omlx', name: 'Local model', state: 'needs_setup', setup_stage: 'Installing', download_percent: 42 },
				{ source: 'claude-code', name: 'Claude Code', state: 'ready', accounts: [{ state: 'ready' }] },
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

		expect(host.textContent).toContain('Installing');
		expect(host.textContent).toContain('Ready to use.');
		expect(Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Start using CORSIA'))).toHaveProperty('disabled', false);
	});
});
