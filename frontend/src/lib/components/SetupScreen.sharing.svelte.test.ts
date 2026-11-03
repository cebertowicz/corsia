import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const LINE = 'This helps improve CORSIA. It sends counts and a random number for this install, never your name, email or anything you write. Turn it off to stop.';
const ERROR = 'Couldn’t save your usage sharing choice. Try again.';

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let settingsStatus = 200;

async function settle() { await tick(); await new Promise((done) => setTimeout(done)); await tick(); }
const button = (label: string) => Array.from(host.querySelectorAll('button')).find((item) => item.textContent?.trim() === label);
const toggle = () => host.querySelector<HTMLButtonElement>('button[aria-label="Share usage counts"]')!;
function setInput(id: string, value: string) {
	const input = host.querySelector<HTMLInputElement>(`#${id}`)!;
	input.value = value;
	input.dispatchEvent(new Event('input', { bubbles: true }));
}
async function openAccount() {
	await settle(); button('Continue')?.click(); await settle();
	setInput('setup-name', 'Ada Lovelace'); setInput('setup-username', 'ada'); setInput('setup-password', 'password1'); setInput('setup-confirm-password', 'password1');
	await settle();
}
/** The writes to the settings route: its body and the session it carried. */
const settingsWrites = () => vi.mocked(fetch).mock.calls
	.filter(([url, init]) => String(url) === '/api/settings' && init?.method === 'PATCH')
	.map(([, init]) => ({ body: JSON.parse(String(init?.body)), auth: (init?.headers as Record<string, string>).Authorization }));
const accountPuts = () => vi.mocked(fetch).mock.calls.filter(([url, init]) => String(url) === '/api/setup/account' && init?.method === 'PUT');

beforeEach(() => {
	token.set(null); user.set(null); settingsStatus = 200;
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input); const method = init?.method ?? 'GET';
		if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: false, work_folder: '~/CORSIA' });
		if (url === '/api/setup/first-account') return json({ token: 'session-one', user: { id: 'ada', name: 'Ada Lovelace' } });
		if (url === '/api/settings' && method === 'PATCH') return json({}, settingsStatus);
		if (url.includes('/sources')) return json({ sources: [] });
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

describe('usage sharing on the account step', () => {
	it('shows the switch on, with its label and line', async () => {
		await openAccount();
		expect(toggle().getAttribute('aria-pressed')).toBe('true');
		expect(host.textContent).toContain('Share usage counts');
		expect(host.querySelector('#setup-sharing-line')?.textContent).toBe(LINE);
	});

	it('creating the account with it off writes false with the new session, then moves on', async () => {
		await openAccount();
		toggle().click(); await settle();
		expect(toggle().getAttribute('aria-pressed')).toBe('false');
		button('Continue')?.click(); await settle();
		expect(settingsWrites()).toEqual([{ body: { usage_sharing: false }, auth: 'Bearer session-one' }]);
		expect(get(token)).toBe('session-one');
		expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions');
	});

	it('writes true explicitly when it was left on', async () => {
		await openAccount();
		button('Continue')?.click(); await settle();
		expect(settingsWrites().map((write) => write.body)).toEqual([{ usage_sharing: true }]);
	});

	it('shows the error and stays on the account step when the write fails, and a retry writes it again', async () => {
		await openAccount();
		toggle().click(); await settle();
		settingsStatus = 500;
		button('Continue')?.click(); await settle();
		expect(host.querySelector('[role="alert"]')?.textContent).toBe(ERROR);
		expect(host.querySelector('h1')?.textContent).toBe('Your account');
		settingsStatus = 200;
		button('Continue')?.click(); await settle();
		expect(settingsWrites().map((write) => write.body)).toEqual([{ usage_sharing: false }, { usage_sharing: false }]);
		expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions');
		expect(accountPuts()).toHaveLength(0);
	});

	describe('Back to the account step', () => {
		async function backToAccount() {
			await openAccount();
			button('Continue')?.click(); await settle();
			button('Back')?.click(); await settle();
			expect(host.querySelector('h1')?.textContent).toBe('Your account');
		}

		it('writes the switch when only it changed', async () => {
			await backToAccount();
			toggle().click(); await settle();
			button('Continue')?.click(); await settle();
			expect(settingsWrites().map((write) => write.body)).toEqual([{ usage_sharing: true }, { usage_sharing: false }]);
			expect(accountPuts()).toHaveLength(0);
			expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions');
		});

		it('writes nothing when nothing changed', async () => {
			await backToAccount();
			button('Continue')?.click(); await settle();
			expect(settingsWrites()).toHaveLength(1);
			expect(accountPuts()).toHaveLength(0);
			expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions');
		});
	});
});
