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
function getByRole(role: string) {
	const match = host.querySelector(`[role="${role}"]`);
	if (!match) throw new Error(`Expected an element with role ${role}`);
	return match;
}
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

// The approved Welcome copy (onboarding story section 2), spelled out here so a change to the shipped strings fails this test.
const APPROVED_WELCOME = {
	title: 'Welcome to CORSIA',
	lines: [
		'Your agents learn how you like things done and keep their own instructions and skills up to date, so you don’t have to.',
		'You can check that yourself. CORSIA measures how much of your time their work takes, and you can see it any time.',
	],
	rows: [
		'CORSIA handles: the setup steps for connecting your tools, and checking work against what you asked before it reaches you.',
		'You decide: what gets done. When a choice is yours to make, agents bring it to you with a recommendation.',
		'Asks you first: sending messages or posting anything for you, making purchases, or opening files outside your work folder. You can let agents go ahead on things you approve often.',
	],
	account: 'Create your account. It stays on this computer.',
};
const flat = (text: string | null | undefined) => (text ?? '').replace(/\s+/g, ' ').trim();
function expectWelcomeCopy(accountLine = true) {
	const welcome = host.querySelector('.welcome');
	expect(welcome).toBeTruthy();
	expect(Array.from(welcome!.querySelectorAll('.line')).map((line) => flat(line.textContent))).toEqual(APPROVED_WELCOME.lines);
	expect(Array.from(welcome!.querySelectorAll('.rows li')).map((row) => flat(row.textContent))).toEqual(APPROVED_WELCOME.rows);
	if (accountLine) expect(flat(welcome!.querySelector('.account')?.textContent)).toBe(APPROVED_WELCOME.account);
	else expect(welcome!.querySelector('.account')).toBeNull();
}
async function remountWide() {
	if (component) await unmount(component);
	vi.stubGlobal('matchMedia', (query: string) => ({ matches: query === '(min-width: 80em)', media: query, addEventListener() {}, removeEventListener() {} }));
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
	flushSync(); await settle();
}

describe('SetupScreen', () => {
	it('below 1280 wide, Welcome is its own screen with Continue and the account form is the next screen', async () => {
		await settle();
		expect(host.querySelector('h1')?.textContent).toBe(APPROVED_WELCOME.title);
		expectWelcomeCopy();
		expect(host.querySelector('#setup-name')).toBeNull();
		expect(host.textContent).toContain('Your account');
		expect(host.textContent).toContain('Work folder');
		expect(host.textContent).toContain('Providers & models');
		button('Continue')?.click(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Create your account');
		expect(host.querySelector('.welcome')).toBeNull();
		expect(host.querySelector('#setup-name')).toBeTruthy();
		expect(calls[0]).toEqual({ url: '/api/setup/status', method: 'GET' });
	});

	it('from 1280 wide, Welcome fills the right column beside the account form, which opens with the account sentence as its lede', async () => {
		await remountWide();
		expect(host.querySelector('h1')?.textContent).toBe(APPROVED_WELCOME.title);
		expectWelcomeCopy(false);
		expect(host.querySelector('.aside .welcome')).toBeTruthy();
		const lede = host.querySelector('.lede')!;
		expect(flat(lede.textContent)).toBe(APPROVED_WELCOME.account);
		expect(host.querySelector('.aside')!.contains(lede)).toBe(false);
		const form = host.querySelector('#setup-name')!;
		expect(form).toBeTruthy();
		// Reading order: title, lede, fields, then the Welcome column.
		const follows = (a: Element, b: Element) => Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
		expect(follows(host.querySelector('h1')!, lede)).toBe(true);
		expect(follows(lede, form)).toBe(true);
		expect(follows(form, host.querySelector('.aside')!)).toBe(true);
		fillAccount(); await settle();
		button('Continue')?.click(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Choose your work folder');
		button('Back')?.click(); await settle();
		expect(host.querySelector('.aside .welcome')).toBeTruthy();
	});

	it('keeps the account screen and its step names after Welcome', async () => {
		await settle(); button('Continue')?.click(); await settle();
		expect(host.textContent).toContain('Your account');
		expect(host.textContent).toContain('Work folder');
		expect(host.textContent).toContain('Providers & models');
		expect(calls[0]).toEqual({ url: '/api/setup/status', method: 'GET' });
	});

	it('keeps account fields when Back returns from the work-folder screen', async () => {
		await settle(); button('Continue')?.click(); await settle(); fillAccount(); await settle();
		button('Continue')?.click(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Choose your work folder');
		button('Back')?.click(); await settle();
		expect((host.querySelector('#setup-name') as HTMLInputElement).value).toBe('Ada Lovelace');
		expect((host.querySelector('#setup-username') as HTMLInputElement).value).toBe('ada');
	});

	it('creates the account only when Continue is pressed on Work folder, then opens provider rows', async () => {
		await settle(); button('Continue')?.click(); await settle(); fillAccount(); await settle();
		button('Continue')?.click(); await settle();
		expect(calls.some((call) => call.url === '/api/setup/first-account')).toBe(false);
		button('Continue')?.click(); await settle();
		expect(calls.filter((call) => call.url === '/api/setup/first-account')).toHaveLength(1);
		expect(connect).toHaveBeenCalledOnce();
		expect(host.querySelector('h1')?.textContent).toBe('Providers & models');
		expect(host.textContent).toContain('Connect at least one to start chatting.');
		expect(button('Start using CORSIA')?.disabled).toBe(true);
	});

	it('returns an existing-account response to the account form with Sign in', async () => {
		await remountWide(); fillAccount(); await settle();
		button('Continue')?.click(); await settle();
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => String(input) === '/api/setup/first-account' ? json({}, 409) : json({})));
		button('Continue')?.click(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Welcome to CORSIA');
		expect(host.textContent).toContain('This computer already has a CORSIA account. Sign in instead.');
		expect(button('Sign in')).toBeTruthy();
	});

	it.each([
		['a plain user-facing detail', { detail: 'That username isn’t available. Choose another.' }, 'That username isn’t available. Choose another.'],
		['the backend username validation detail', { detail: 'Use up to 32 lowercase letters, numbers, dashes or underscores for the username.' }, 'Use up to 32 lowercase letters, numbers, dashes or underscores for the username.'],
		['the backend password validation detail', { detail: 'Use a password of at least 8 characters.' }, 'Use a password of at least 8 characters.'],
		['structured detail', { detail: { message: 'username must be valid' } }, 'Couldn’t create your account. Try again.'],
		['no detail', {}, 'Couldn’t create your account. Try again.'],
	] as const)('returns a 422 with %s to an editable account form', async (_case, body, expected) => {
		await remountWide(); fillAccount(); await settle();
		button('Continue')?.click(); await settle();
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => String(input) === '/api/setup/first-account' ? json(body, 422) : json({})));
		button('Continue')?.click(); await settle();
		expect(host.textContent).toContain(expected);
		expect(getByRole('alert').textContent).toContain(expected);
		expect(button('Continue')?.disabled).toBe(false);
	});

	it('checks the signed-in local retry through setup progress and the authenticated start', async () => {
		if (component) await unmount(component);
		const requests: Array<{ url: string; init?: RequestInit }> = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input); requests.push({ url, init });
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, work_folder: '~/CORSIA' });
			if (url === '/api/setup/local-model') return json({}, 409);
			if (url === '/api/setup/local-download') return json(init?.method === 'POST' ? { ok: true } : { state: 'stopped' });
			if (url === '/api/setup/first-account') return json({ token: 'setup-token', user: { id: 'user', name: 'Ada Lovelace' } });
			if (url === '/api/setup/progress') return json({ setup_completed: false, status: { local_eligible: true, work_folder: '~/CORSIA' } });
			if (url === '/api/providers/local/setup') return json({ ok: true });
			if (url.includes('/sources')) return json({ sources: [] });
			if (url.includes('/corsia-agent')) return json({ target: { thread_id: 'corsia-thread' } });
			if (url.includes('/models')) return json({ models: [] });
			return json({});
		}));
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync(); await settle(); button('Continue')?.click(); await settle(); fillAccount(); await settle();
		button('Continue')?.click(); await settle(); button('Continue')?.click(); await settle();
		await vi.waitFor(() => expect(button('Retry')).toBeTruthy());
		button('Retry')?.click();
		await vi.waitFor(() => expect(requests.some((request) => request.url === '/api/providers/local/setup')).toBe(true));
		expect(requests.some((request) => request.url === '/api/setup/progress')).toBe(true);
		const authenticatedStart = requests.find((request) => request.url === '/api/providers/local/setup')!;
		expect(authenticatedStart.init?.headers).toMatchObject({ Authorization: 'Bearer setup-token' });
		expect(requests.filter((request) => request.url === '/api/setup/local-model')).toHaveLength(1);
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
		// No account exists yet, so the step rail must not mark it done under the error.
		const accountRow = [...host.querySelectorAll('.lbl')].find((label) => label.textContent === 'Your account')?.closest('*:has(> .lead)');
		expect(accountRow).toBeTruthy();
		expect(accountRow?.querySelector('.done-check')).toBeNull();
	});
});
