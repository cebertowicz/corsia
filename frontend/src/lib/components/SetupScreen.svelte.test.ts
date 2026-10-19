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
let calls: Array<{ url: string; method: string }>;

async function settle() { await tick(); await new Promise((done) => setTimeout(done)); await tick(); }
function button(label: string) { return Array.from(host.querySelectorAll('button')).find((item) => item.textContent?.trim() === label); }
function setInput(id: string, value: string) {
	const input = host.querySelector<HTMLInputElement>(`#${id}`)!;
	input.value = value;
	input.dispatchEvent(new Event('input', { bubbles: true }));
}
function fillAccount() {
	setInput('setup-name', 'Ada Lovelace');
	setInput('setup-username', 'ada');
	setInput('setup-password', 'password1');
	setInput('setup-confirm-password', 'password1');
}

beforeEach(() => {
	token.set(null); user.set(null); calls = [];
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input); const method = init?.method ?? 'GET'; calls.push({ url, method });
		if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: false, work_folder: '~/CORSIA' });
		if (url === '/api/setup/first-account') return json({ token: 'setup-token', user: { id: 'user', name: 'Ada Lovelace' } });
		if (url.includes('/sources')) return json({ sources: [] });
		if (url.includes('/corsia-agent')) return json({ target: { thread_id: 'corsia-thread' } });
		if (url.includes('/models')) return json({ models: [] });
		return json({});
	}));
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
	flushSync();
});

afterEach(async () => {
	if (component) await unmount(component);
	host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe('SetupScreen', () => {
	it('shows the account screen after status and uses the approved account copy', async () => {
		await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Welcome to CORSIA');
		expect(host.textContent).toContain('Create your account. It stays on this computer.');
		expect(host.textContent).toContain('Your account');
		expect(host.textContent).toContain('Work folder');
		expect(host.textContent).toContain('Providers & models');
		expect(calls[0]).toEqual({ url: '/api/setup/status', method: 'GET' });
	});

	it('keeps account fields when Back returns from the work-folder screen', async () => {
		await settle(); fillAccount(); await settle();
		button('Continue')?.click(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Choose your work folder');
		button('Back')?.click(); await settle();
		expect((host.querySelector('#setup-name') as HTMLInputElement).value).toBe('Ada Lovelace');
		expect((host.querySelector('#setup-username') as HTMLInputElement).value).toBe('ada');
	});

	it('creates the account only when Continue is pressed on Work folder, then opens provider rows', async () => {
		await settle(); fillAccount(); await settle();
		button('Continue')?.click(); await settle();
		expect(calls.some((call) => call.url === '/api/setup/first-account')).toBe(false);
		button('Continue')?.click(); await settle();
		expect(calls.filter((call) => call.url === '/api/setup/first-account')).toHaveLength(1);
		expect(connect).toHaveBeenCalledOnce();
		expect(host.querySelector('h1')?.textContent).toBe('Providers & models');
		expect(host.textContent).toContain('Connect at least one to start chatting.');
		expect(button('Start using CORSIA')?.disabled).toBe(true);
	});

	it('routes a completed setup status to sign in', async () => {
		if (component) await unmount(component);
		const onExistingAccount = vi.fn();
		vi.stubGlobal('fetch', vi.fn(async () => json({ code: 'setup_complete' }, 409)));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount, onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync(); await settle();
		expect(onExistingAccount).toHaveBeenCalledOnce();
	});

	it('shows Retry and Save a report when startup status fails', async () => {
		if (component) await unmount(component);
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke: vi.fn(async () => undefined) });
		vi.stubGlobal('fetch', vi.fn(async () => json({}, 500)));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('CORSIA couldn’t start');
		expect(button('Retry')).toBeTruthy();
		expect(button('Save a report')).toBeTruthy();
	});
});
