import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';

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
		expect(calls).toEqual(['/api/setup/status']);
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

	it('starts the gated selected local download with Get started', async () => {
		await settle();
		expect(host.textContent).toContain('Download Example model now');
		const action = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'));
		action?.click();
		await settle();
		expect(calls).toContain('/api/setup/local-download');
		expect(host.textContent).toContain('Create your account');
	});

	it('shows the bootstrapped computer location without allowing it to change', async () => {
		await settle();
		const action = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'));
		action?.click();
		await settle();
		expect(host.textContent).toContain('This computer');
		expect(host.textContent).toContain('~/CORSIA');
		expect(host.textContent).not.toContain('Change');
		expect(host.querySelector('[aria-label="Work folder"]')).toBeNull();
		expect(host.textContent).not.toContain('work folder');
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
		expect(host.textContent).toContain('Free up space for 30 GB');
		const action = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.includes('Get started'));
		action?.click();
		await settle();
		expect(calls).not.toContain('/api/setup/local-download');
		expect(host.textContent).toContain('Create your account');
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
	});

});
