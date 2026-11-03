import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const LINE = 'This helps improve CORSIA. It sends counts of what you use, tagged with a random number that changes every week. It never sends your name, email or anything you write. Turn it off to stop.';

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let createStatus = 200, putStatus = 200;
let sharingDefault: boolean | undefined = true;
/** A held account create waits on this before answering; null answers at once. */
let holdCreate: Promise<void> | null = null;

async function settle() { await tick(); await new Promise((done) => setTimeout(done)); await tick(); }
const button = (label: string) => Array.from(host.querySelectorAll('button')).find((item) => item.textContent?.trim() === label);
const toggle = () => host.querySelector<HTMLButtonElement>('button[aria-label="Share usage counts"]')!;
const heading = () => host.querySelector('h1')?.textContent;
function setInput(id: string, value: string) {
	const input = host.querySelector<HTMLInputElement>(`#${id}`)!;
	input.value = value;
	input.dispatchEvent(new Event('input', { bubbles: true }));
}
async function openAccount() {
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
	flushSync(); await settle(); button('Continue')?.click(); await settle();
	setInput('setup-name', 'Ada Lovelace'); setInput('setup-username', 'ada'); setInput('setup-password', 'password1'); setInput('setup-confirm-password', 'password1');
	await settle();
}
const bodies = (path: string, method: string) => vi.mocked(fetch).mock.calls
	.filter(([url, init]) => String(url) === path && init?.method === method)
	.map(([, init]) => JSON.parse(String(init?.body)));
const creates = () => bodies('/api/setup/first-account', 'POST');
const puts = () => bodies('/api/setup/account', 'PUT');
const settingsWrites = () => bodies('/api/settings', 'PATCH');

beforeEach(() => {
	token.set(null); user.set(null); createStatus = 200; putStatus = 200; sharingDefault = true; holdCreate = null;
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input); const method = init?.method ?? 'GET';
		if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: false, work_folder: '~/CORSIA', usage_sharing_default: sharingDefault });
		if (url === '/api/setup/first-account') {
			if (holdCreate) await holdCreate;
			return createStatus === 500
				? json({ code: 'setup_failed', detail: 'Your account was not created. Try again.', ref: 'ref-7' }, 500)
				: json({ token: 'session-one', user: { id: 'ada', name: 'Ada Lovelace' } });
		}
		if (url === '/api/setup/account' && method === 'PUT') {
			return putStatus === 500 ? json({ code: 'setup_failed', detail: 'Your account was not changed. Try again.', ref: 'ref-8' }, 500) : json({ ok: true });
		}
		if (url.includes('/sources')) return json({ sources: [] });
		if (url.includes('/models')) return json({ models: [] });
		return json({});
	}));
	host = document.createElement('div'); document.body.appendChild(host);
});

afterEach(async () => {
	if (component) await unmount(component);
	component = null;
	host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe('usage sharing on the account step', () => {
	it('shows the switch on, with its label and line', async () => {
		await openAccount();
		expect(toggle().getAttribute('aria-pressed')).toBe('true');
		expect(host.textContent).toContain('Share usage counts');
		expect(host.querySelector('#setup-sharing-line')?.textContent).toBe(LINE);
	});

	it('creating the account with it off sends false with the account, and nothing else writes it', async () => {
		await openAccount();
		toggle().click(); await settle();
		expect(toggle().getAttribute('aria-pressed')).toBe('false');
		button('Continue')?.click(); await settle();
		expect(creates()).toHaveLength(1);
		expect(creates()[0].usage_sharing).toBe(false);
		expect(settingsWrites()).toHaveLength(0);
		expect(get(token)).toBe('session-one');
		expect(heading()).toBe('Your subscriptions');
	});

	it('starts off where the region needs opt-in, and leaving it off sends false', async () => {
		sharingDefault = false;
		await openAccount();
		expect(toggle().getAttribute('aria-pressed')).toBe('false');
		button('Continue')?.click(); await settle();
		expect(creates()[0].usage_sharing).toBe(false);
	});

	it('starts off when the status carries no default', async () => {
		sharingDefault = undefined;
		await openAccount();
		expect(toggle().getAttribute('aria-pressed')).toBe('false');
	});

	it('sends true explicitly when it was left on', async () => {
		await openAccount();
		button('Continue')?.click(); await settle();
		expect(creates()[0].usage_sharing).toBe(true);
	});

	it('holds the switch still while the account is being made', async () => {
		let release!: () => void;
		holdCreate = new Promise((done) => { release = done; });
		await openAccount();
		button('Continue')?.click(); await settle();
		expect(toggle().disabled).toBe(true);
		release(); await settle();
		expect(heading()).toBe('Your subscriptions');
	});

	it('a failed create shows its reference and stays, and a retry sends the choice again', async () => {
		await openAccount();
		toggle().click(); await settle();
		createStatus = 500;
		button('Continue')?.click(); await settle();
		expect(host.querySelector('[role="alert"]')?.textContent).toBe('Couldn’t create your account. Try again. Reference: ref-7');
		expect(toggle().disabled).toBe(false);
		createStatus = 200;
		button('Continue')?.click(); await settle();
		expect(creates().map((body) => body.usage_sharing)).toEqual([false, false]);
		expect(heading()).toBe('Your subscriptions');
	});

	describe('Back to the account step', () => {
		async function backToAccount() {
			await openAccount();
			button('Continue')?.click(); await settle();
			button('Back')?.click(); await settle();
			expect(heading()).toBe('Your account');
		}

		it('sends only the switch when only it changed', async () => {
			await backToAccount();
			toggle().click(); await settle();
			button('Continue')?.click(); await settle();
			expect(puts()).toEqual([{ usage_sharing: false }]);
			expect(heading()).toBe('Your subscriptions');
		});

		it('sends nothing when nothing changed', async () => {
			await backToAccount();
			button('Continue')?.click(); await settle();
			expect(puts()).toHaveLength(0);
			expect(heading()).toBe('Your subscriptions');
		});

		it('a failed save keeps the change unsaved, so the retry sends it again', async () => {
			await backToAccount();
			toggle().click(); await settle();
			putStatus = 500;
			button('Continue')?.click(); await settle();
			expect(host.querySelector('[role="alert"]')?.textContent).toBe('Couldn’t save your account. Try again. Reference: ref-8');
			putStatus = 200;
			button('Continue')?.click(); await settle();
			expect(puts()).toEqual([{ usage_sharing: false }, { usage_sharing: false }]);
			expect(heading()).toBe('Your subscriptions');
		});
	});
});
