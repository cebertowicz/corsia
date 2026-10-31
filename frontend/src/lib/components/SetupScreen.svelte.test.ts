import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
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
let progressBodies: string[];
let localEligible = false;
let macosSupported: boolean | null = true;
let workFolder: string | undefined;
let sources: Array<{ source: string; name: string; state: string }>;

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
	token.set(null); user.set(null); localEligible = false; macosSupported = true; workFolder = '~/CORSIA'; calls = []; progressBodies = []; sources = [];
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input); const method = init?.method ?? 'GET'; calls.push({ url, method });
		if (url === '/api/setup/progress' && method === 'PUT') progressBodies.push(String(init?.body));
		if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: macosSupported, local_eligible: localEligible, ...(workFolder ? { work_folder: workFolder } : {}) });
		if (url === '/api/setup/first-account') return json({ token: 'setup-token', user: { id: 'user', name: 'Ada Lovelace' } });
		if (url.includes('/sources')) return json({ sources });
		if (url.includes('/corsia-agent')) return json({ target: { thread_id: 'corsia-thread' } });
		if (url.includes('/models')) return json({ models: [] });
		if (url.includes('/install')) return json({ state: 'running', reason: null, ref: null, path_shadowed_by: null });
		return json({});
	}));
	host = document.createElement('div'); document.body.appendChild(host);
	finished = vi.fn();
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: finished } });
	flushSync();
});

/** Welcome, then the account form with its work folder, ending wherever Continue leads once the account exists. */
async function passFolder() {
	await settle(); button('Continue')?.click(); await settle(); fillAccount(); await settle();
	button('Continue')?.click(); await settle();
}
const rowNames = () => Array.from(host.querySelectorAll('section.item')).map((row) => row.getAttribute('aria-label'));

afterEach(async () => {
	if (component) await unmount(component);
	host.remove(); delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__; vi.restoreAllMocks(); vi.unstubAllGlobals();
});

// The approved Welcome copy (onboarding story section 2), spelled out here so a change to the shipped strings fails this test.
const APPROVED_WELCOME = {
	title: 'Welcome to CORSIA',
	lines: [
		'Your agents learn how you like things done and keep their own instructions and skills up to date, so you don’t have to.',
		'You can check that yourself: CORSIA measures the time you spend on work here, including keeping an eye on your agents.',
	],
	rows: [
		'CORSIA handles: the setup steps for connecting your tools, and checking work against what you asked before it reaches you.',
		'You decide: what gets done. When a choice is yours to make, agents bring it to you with a recommendation.',
		'Asks you first: sending or posting as you, and spending money.',
	],
};
const flat = (text: string | null | undefined) => (text ?? '').replace(/\s+/g, ' ').trim();
function expectWelcomeCopy() {
	const welcome = host.querySelector('.welcome');
	expect(welcome).toBeTruthy();
	expect(Array.from(welcome!.querySelectorAll('.line')).map((line) => flat(line.textContent))).toEqual(APPROVED_WELCOME.lines);
	expect(Array.from(welcome!.querySelectorAll('.rows li')).map((row) => flat(row.textContent))).toEqual(APPROVED_WELCOME.rows);
	expect(welcome!.querySelector('.account')).toBeNull();
}
/** The Getting started screen's Continue, then the account form. */
async function openAccount() { await settle(); button('Continue')?.click(); await settle(); }
async function remountWithWorkFolder(next: string | undefined) {
	if (component) await unmount(component);
	host.replaceChildren(); workFolder = next;
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: finished } });
	flushSync();
}
/** Back on the account screen after the account was made and the person moved on to subscriptions. */
let finished = vi.fn();
async function backToAccount() {
	await passFolder();
	expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions');
	button('Back')?.click(); await settle();
	expect(host.querySelector('h1')?.textContent).toBe('Your account');
}
const putCalls = () => vi.mocked(fetch).mock.calls.filter(([url, init]) => String(url) === '/api/setup/account' && init?.method === 'PUT');
const putBodies = () => putCalls().map(([, init]) => JSON.parse(String(init?.body)));
async function remountWide() {
	if (component) await unmount(component);
	vi.stubGlobal('matchMedia', (query: string) => ({ matches: query === '(min-width: 80em)', media: query, addEventListener() {}, removeEventListener() {} }));
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
	flushSync(); await settle();
}

describe('SetupScreen', () => {
	it.each([
		{ supported: false, title: 'Update macOS to open CORSIA' },
		{ supported: null, title: APPROVED_WELCOME.title },
	])('shows $title when the macOS floor is $supported', async ({ supported, title }) => {
		macosSupported = supported;
		await remountWithWorkFolder('~/CORSIA'); await settle();
		expect(host.querySelector('h1')?.textContent).toBe(title);
	});

	it('opens on Getting started, the first of four rail steps, with Continue to the account form', async () => {
		await settle();
		expect(host.querySelector('h1')?.textContent).toBe(APPROVED_WELCOME.title);
		expectWelcomeCopy();
		expect(host.querySelector('#setup-name')).toBeNull();
		expect([...host.querySelectorAll('.lbl')].map((label) => label.textContent?.trim())).toEqual(['Getting started', 'Account & workspace', 'Your subscriptions', 'Providers & models']);
		expect(button('Back')).toBeUndefined();
		button('Continue')?.click(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Create your account');
		// The work folder row rides with the form, so Welcome's Continue reaches both.
		expect(host.textContent).toContain('Work folder');
		expect(host.textContent).toContain('~/CORSIA');
		expect(host.querySelector('.welcome')).toBeNull();
		expect(host.querySelector('#setup-name')).toBeTruthy();
		expect(calls[0]).toEqual({ url: '/api/setup/status', method: 'GET' });
		button('Back')?.click(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe(APPROVED_WELCOME.title);
	});

	it('shows the same screens from 1280 wide, with the living mark beside them that stays put from one screen to the next', async () => {
		await remountWide();
		expect(host.querySelector('h1')?.textContent).toBe(APPROVED_WELCOME.title);
		expectWelcomeCopy();
		const mark = host.querySelector('.brand .living-mark');
		expect(mark).toBeTruthy();
		button('Continue')?.click(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Create your account');
		expect(host.querySelector('#setup-name')).toBeTruthy();
		// A screen change swaps the scene, never the mark: it is the same element, so its animations carry on.
		expect(host.querySelector('.brand .living-mark')).toBe(mark);
		expect(host.querySelectorAll('.living-mark')).toHaveLength(1);
	});

	it('keeps the account screen and its step names after Welcome', async () => {
		await settle(); button('Continue')?.click(); await settle();
		expect(host.textContent).toContain('Account & workspace');
		expect(host.textContent).toContain('Providers & models');
		expect(calls[0]).toEqual({ url: '/api/setup/status', method: 'GET' });
	});

	it('gives the account screen its lede and the email field its reason', async () => {
		await settle(); button('Continue')?.click(); await settle();
		expect(host.querySelector('.lede')?.textContent).toBe('Your account and password stay on this computer.');
		expect(host.querySelector('#setup-email')?.closest('.field')?.querySelector('.hint')?.textContent).toBe('Used only to sign you up for services you choose, such as web search.');
	});

	it('labels the name field Full name and prefills the username from the first name', async () => {
		await settle(); button('Continue')?.click(); await settle();
		expect(host.querySelector('label[for="setup-name"]')?.textContent?.trim()).toBe('Full name');
		setInput('setup-name', 'Ada Lovelace'); await settle();
		expect((host.querySelector('#setup-username') as HTMLInputElement).value).toBe('ada');
	});

	it('leaves a cleared or edited username alone when the name changes', async () => {
		await settle(); button('Continue')?.click(); await settle();
		const username = () => (host.querySelector('#setup-username') as HTMLInputElement).value;
		setInput('setup-name', 'Ada Lovelace'); await settle();
		setInput('setup-username', ''); await settle();
		expect(username()).toBe('');
		setInput('setup-name', 'Ada King'); await settle();
		expect(username()).toBe('');
		// Clearing the name too makes the suggestion empty; the cleared username still stays cleared.
		setInput('setup-name', ''); await settle();
		setInput('setup-name', 'Grace Hopper'); await settle();
		expect(username()).toBe('');
		setInput('setup-username', 'countess'); await settle();
		setInput('setup-name', 'Augusta King'); await settle();
		expect(username()).toBe('countess');
	});

	it('creates the account when Continue is pressed on the form with its work folder, then opens provider rows', async () => {
		await settle(); button('Continue')?.click(); await settle(); fillAccount(); await settle();
		expect(calls.some((call) => call.url === '/api/setup/first-account')).toBe(false);
		button('Continue')?.click(); await settle();
		expect(calls.filter((call) => call.url === '/api/setup/first-account')).toHaveLength(1);
		expect(connect).toHaveBeenCalledOnce();
		expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions');
		button('Neither')?.click(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Providers & models');
		expect(host.textContent).toContain('CORSIA needs one working model to continue setup.');
		expect(button('Start using CORSIA')?.disabled).toBe(true);
	});

	it('keeps the shell-owned default when a warned folder is replaced', async () => {
		const invoke = vi.fn(async (command: string) => command === 'choose_work_folder'
			? { state: 'ready', path: '/fixture/Documents', folder_warning: { kind: 'personal_folder', folder: 'Documents' } }
			: undefined);
		Object.defineProperty(window, '__CORSIA_SHELL__', { configurable: true, value: { mode: 'local', default_workspace: { path: '/fixture/default-workspace', display: '~/CORSIA' } } });
		Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } });
		await remountWithWorkFolder('~/CORSIA');
		await openAccount(); button('Change')?.click(); await settle();
		button('Use ~/CORSIA')?.click(); fillAccount(); await settle();
		button('Continue')?.click(); await settle();
		expect(invoke.mock.calls.filter(([command]) => command === 'configure_work_folder')).toHaveLength(0);
		expect(calls.filter((call) => call.url === '/api/setup/first-account')).toHaveLength(1);
	});

	it('offers the canonical default after returning to setup and re-picking a saved personal folder', async () => {
		const invoke = vi.fn(async (command: string) => command === 'choose_work_folder'
			? { state: 'ready', path: '/fixture/personal-workspace', folder_warning: { kind: 'personal_folder', folder: 'Personal' } }
			: undefined);
		Object.defineProperty(window, '__CORSIA_SHELL__', { configurable: true, value: { mode: 'local', default_workspace: { path: '/fixture/default-workspace', display: '~/CORSIA' } } });
		Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } });
		await remountWithWorkFolder('~/Personal');
		await openAccount(); button('Back')?.click(); await settle(); button('Continue')?.click(); await settle();
		button('Change')?.click(); await settle();
		expect(button('Use ~/CORSIA')).toBeTruthy();
	});

	it('applies the real default path when replacing a saved personal folder before account submit', async () => {
		const invoke = vi.fn(async (command: string) => command === 'choose_work_folder'
			? { state: 'ready', path: '/fixture/personal-workspace', folder_warning: { kind: 'personal_folder', folder: 'Personal' } }
			: undefined);
		Object.defineProperty(window, '__CORSIA_SHELL__', { configurable: true, value: { mode: 'local', default_workspace: { path: '/fixture/default-workspace', display: '~/CORSIA' } } });
		Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } });
		await remountWithWorkFolder('~/Personal');
		await openAccount(); button('Back')?.click(); await settle(); button('Continue')?.click(); await settle();
		button('Change')?.click(); await settle(); button('Use ~/CORSIA')?.click(); fillAccount(); await settle();
		button('Continue')?.click(); await settle();
		expect(invoke).toHaveBeenCalledWith('configure_work_folder', { path: '/fixture/default-workspace' });
		expect(invoke).not.toHaveBeenCalledWith('configure_work_folder', { path: '~/CORSIA' });
	});

	for (const [state, reason] of [['unreadable', 'permission denied'], ['unwritable', 'the disk is read-only']] as const) {
		it(`does not apply a ${state} folder or create an account`, async () => {
			const invoke = vi.fn(async (command: string) => command === 'choose_work_folder'
				? { state, path: '/fixture/unavailable', reason }
				: undefined);
			Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke } });
			await openAccount(); button('Change')?.click(); await settle(); fillAccount(); await settle();

			expect(button('Continue')).toHaveProperty('disabled', true);
			button('Continue')?.click(); await settle();
			expect(invoke.mock.calls.filter(([command]) => command === 'configure_work_folder')).toHaveLength(0);
			expect(calls.filter((call) => call.url === '/api/setup/first-account')).toHaveLength(0);
		});
	}

	it('asks which subscriptions they pay for, installs the picked tool at once and shows a row only for it', async () => {
		sources = [{ source: 'claude-code', name: 'Claude Code', state: 'not_installed' }, { source: 'codex', name: 'Codex', state: 'not_installed' }];
		await passFolder();
		expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions');
		expect(host.textContent).toContain('Which do you pay for?');
		for (const label of ['Claude', 'ChatGPT', 'Both', 'Neither']) expect(button(label)).toBeTruthy();
		button('Claude')?.click(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Providers & models');
		expect(calls).toContainEqual({ url: '/api/providers/cli/claude-code/install', method: 'POST' });
		expect(calls.some((call) => call.url === '/api/providers/cli/codex/install')).toBe(false);
		// The answer is kept in setup progress, where the first chat reads which subscriptions they pay for.
		expect(progressBodies.map((body) => JSON.parse(body))).toContainEqual({ picks: { claude: true, chatgpt: false } });
		expect(rowNames()).toEqual(['Claude Code', 'Local model', 'API provider']);
		expect(button('Sign in')).toBeUndefined();
	});

	it('installs only the chosen CLI after its delayed detection', async () => {
		await passFolder();
		button('Claude')?.click(); await settle();
		expect(calls.filter((call) => call.method === 'POST' && call.url.includes('/install'))).toEqual([]);
		sources = [{ source: 'claude-code', name: 'Claude Code', state: 'unverified' }, { source: 'codex', name: 'Codex', state: 'not_installed' }];
		window.dispatchEvent(new Event('focus')); await settle();
		expect(calls.some((call) => call.url.includes('/install') && call.method === 'POST')).toBe(false);
		sources = [{ source: 'claude-code', name: 'Claude Code', state: 'not_installed' }, { source: 'codex', name: 'Codex', state: 'not_installed' }];
		window.dispatchEvent(new Event('focus')); await settle();
		expect(calls.filter((call) => call.method === 'POST' && call.url.includes('/install'))).toEqual([{ url: '/api/providers/cli/claude-code/install', method: 'POST' }]);
	});

	it('polls for the chosen CLI without a source event and starts its install', async () => {
		await passFolder();
		vi.useFakeTimers();
		try {
			button('Claude')?.click(); flushSync();
			sources = [{ source: 'claude-code', name: 'Claude Code', state: 'not_installed' }];
			await vi.advanceTimersByTimeAsync(3000); await tick();
			expect(calls.filter((call) => call.method === 'POST' && call.url === '/api/providers/cli/claude-code/install')).toHaveLength(1);
		} finally { vi.useRealTimers(); }
	});

	it('starts the chosen install when an unrelated source check fails', async () => {
		sources = [{ source: 'claude-code', name: 'Claude Code', state: 'unverified' }];
		const originalFetch = vi.mocked(fetch).getMockImplementation()!;
		// The recheck writes Claude Code's row, then fails on another source's check.
		vi.mocked(fetch).mockImplementation(async (input, init) => {
			if (!String(input).includes('/sources/recheck')) return originalFetch(input, init);
			sources = [{ source: 'claude-code', name: 'Claude Code', state: 'not_installed' }];
			return new Response('{}', { status: 503 });
		});
		await passFolder();
		vi.useFakeTimers();
		try {
			button('Claude')?.click(); flushSync();
			await vi.advanceTimersByTimeAsync(3000); await tick();
			expect(calls.filter((call) => call.method === 'POST' && call.url === '/api/providers/cli/claude-code/install')).toHaveLength(1);
		} finally { vi.useRealTimers(); }
	});

	it('shows Retry if detection fails while an unchecked source is present', async () => {
		sources = [{ source: 'claude-code', name: 'Claude Code', state: 'unverified' }];
		await passFolder();
		const originalFetch = vi.mocked(fetch).getMockImplementation()!;
		vi.mocked(fetch).mockImplementation(async (input, init) => String(input).includes('/sources/recheck') ? new Response('{}', { status: 500 }) : originalFetch(input, init));
		vi.useFakeTimers();
		try {
			button('Claude')?.click(); flushSync();
			await vi.advanceTimersByTimeAsync(3000); await tick();
			const row = host.querySelector('section[aria-label="Claude Code"]');
			expect(row?.textContent).toContain('Couldn’t check Claude Code.');
			expect(row?.textContent).not.toContain('Retry');
			expect(row?.querySelector('.spinner')).toBeNull();
			expect([...host.querySelectorAll('button')].filter((item) => item.textContent?.trim() === 'Retry')).toHaveLength(1);
		} finally { vi.useRealTimers(); }
	});

	it.each([
		{ vendor: 'claude-code', name: 'Claude Code', choice: 'Claude', succeeds: true },
		{ vendor: 'claude-code', name: 'Claude Code', choice: 'Claude', succeeds: false },
		{ vendor: 'codex', name: 'Codex', choice: 'ChatGPT', succeeds: true },
		{ vendor: 'codex', name: 'Codex', choice: 'ChatGPT', succeeds: false },
	])('shows $name source retry pending and returns to success=$succeeds', async ({ vendor, name, choice, succeeds }) => {
		sources = [{ source: vendor, name, state: 'unverified' }];
		await passFolder();
		const originalFetch = vi.mocked(fetch).getMockImplementation()!;
		let resolveRetry!: (response: Response) => void;
		let rechecks = 0;
		vi.mocked(fetch).mockImplementation(async (input, init) => {
			const url = String(input);
			if (url.includes('/sources/recheck')) {
				rechecks++;
				if (rechecks === 1) return new Response('{}', { status: 500 });
				if (rechecks > 2) return json({ sources });
				return new Promise<Response>((resolve) => { resolveRetry = resolve; });
			}
			if (url.includes('/models') && sources[0]?.state === 'ready') return json({ models: [{ connection: vendor, status: 'ready', runs_on: [{ selected: true, status: 'ready' }] }] });
			return originalFetch(input, init);
		});
		vi.useFakeTimers();
		try {
			button(choice)?.click(); flushSync();
			await vi.advanceTimersByTimeAsync(3000); await tick();
			const row = () => host.querySelector(`section[aria-label="${name}"]`);
			const error = 'CORSIA couldn’t check your model sources. Try again.';
			const checking = 'Checking which models are ready…';
			expect(row()?.textContent).toContain(`Couldn’t check ${name}.`);
			expect(host.textContent).toContain(error);
			expect([...host.querySelectorAll('button')].filter((item) => item.textContent?.trim() === 'Retry')).toHaveLength(1);
			button('Retry')!.click(); await tick();
			expect(row()?.querySelector('.spinner')).toBeTruthy();
			expect(host.querySelector('section[aria-label="' + checking + '"] .spinner')).toBeTruthy();
			expect(host.textContent).not.toContain(error);
			expect(button('Retry')?.disabled).toBe(true);
			button('Retry')?.click(); expect(rechecks).toBe(2);
			window.dispatchEvent(new Event('focus')); await vi.advanceTimersByTimeAsync(0); await tick();
			expect(row()?.querySelector('.spinner')).toBeTruthy();
			expect(host.querySelector('section[aria-label="' + checking + '"] .spinner')).toBeTruthy();
			expect(host.textContent).not.toContain(error);
			expect(button('Retry')?.disabled).toBe(true);
			if (succeeds) sources = [{ source: vendor, name, state: 'ready' }];
			resolveRetry(succeeds ? json({ sources }) : new Response('{}', { status: 500 }));
			await vi.advanceTimersByTimeAsync(0); await tick();
			if (succeeds) {
				expect(host.textContent).not.toContain(error);
				expect(row()?.textContent).toContain('Ready');
			} else {
				expect(row()?.textContent).toContain(`Couldn’t check ${name}.`);
				expect(host.textContent).toContain(error);
				expect(button('Retry')?.disabled).toBe(false);
			}
			expect(row()?.querySelector('.spinner')).toBeNull();
		} finally { vi.useRealTimers(); }
	});

	it('keeps an unrelated provider error visible during a source retry', async () => {
		sources = [
			{ source: 'claude-code', name: 'Claude Code', state: 'unverified' },
			{ source: 'codex', name: 'Codex', state: 'ready', limited: true } as never,
		];
		await passFolder();
		const originalFetch = vi.mocked(fetch).getMockImplementation()!;
		let resolveRetry!: (response: Response) => void;
		let rechecks = 0;
		vi.mocked(fetch).mockImplementation(async (input, init) => {
			if (!String(input).includes('/sources/recheck')) return originalFetch(input, init);
			if (++rechecks === 1) return new Response('{}', { status: 500 });
			return new Promise<Response>((resolve) => { resolveRetry = resolve; });
		});
		vi.useFakeTimers();
		try {
			(button('Yes') ?? button('Both'))!.click(); flushSync();
			await vi.advanceTimersByTimeAsync(3000); await tick();
			const codex = () => host.querySelector('section[aria-label="Codex"]');
			expect(codex()?.textContent).toContain('usage limit');
			button('Retry')!.click(); await tick();
			expect(codex()?.textContent).toContain('usage limit');
			expect(codex()?.querySelector('.spinner')).toBeNull();
			resolveRetry(new Response('{}', { status: 500 }));
			await vi.advanceTimersByTimeAsync(0); await tick();
		} finally { vi.useRealTimers(); }
	});

	describe('the Local model row by host', () => {
		const providersStep = async (extra: Array<{ source: string; name: string; state: string }> = []) => {
			sources = [{ source: 'claude-code', name: 'Claude Code', state: 'not_installed' }, { source: 'codex', name: 'Codex', state: 'not_installed' }, ...extra];
			// The status is read when the screen mounts, so mount it again with this host's facts.
			await unmount(component!); component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } }); flushSync();
			await passFolder();
			button('Claude')?.click(); await settle();
			expect(host.querySelector('h1')?.textContent).toBe('Providers & models');
		};

		it('keeps the Local model row, and its memory reason, on a Mac with too little memory', async () => {
			localEligible = false; macosSupported = true;
			await providersStep();
			expect(rowNames()).toEqual(['Claude Code', 'Local model', 'API provider']);
			expect(host.textContent).toContain('Adding a local model through CORSIA needs at least 32 GB of memory');
		});

		it('lists no Local model row, and no memory reason, on a server that is not macOS', async () => {
			localEligible = false; macosSupported = null;
			await providersStep();
			expect(rowNames()).toEqual(['Claude Code', 'API provider']);
			expect(host.textContent).not.toContain('24 GB');
			expect(host.textContent).not.toContain('Local model');
		});

		it('lists no Local model row when the local source row says the host never runs one', async () => {
			localEligible = false; macosSupported = true;
			await providersStep([{ source: 'omlx', name: 'Local model', state: 'needs_setup', never_local: true } as never]);
			expect(rowNames()).not.toContain('Local model');
		});
	});

	it('explains the subscriptions screen: a lede, and under neither-signed-in the subscription and the free-model line this computer can honor', async () => {
		const plans = 'Claude Code and Codex are the apps you’ll sign in to next.';
		const lede = 'CORSIA’s agents can work through a Claude or ChatGPT subscription you already pay for.';
		sources = [{ source: 'claude-code', name: 'Claude Code', state: 'not_installed' }, { source: 'codex', name: 'Codex', state: 'not_installed' }];
		await passFolder();
		expect(host.querySelector('.lede')?.textContent).toBe(lede);
		expect(host.textContent).toContain(plans);
		expect(host.textContent).toContain('No subscription? Add an API key on the next screen.');
		expect(host.textContent).not.toContain('The free model downloading to this computer');
		expect(host.querySelector('.explain')?.compareDocumentPosition(host.querySelector('.choices')!)).toBe(Node.DOCUMENT_POSITION_PRECEDING);
	});

	it('offers the free model as the no-subscription option when this computer can run it', async () => {
		localEligible = true;
		sources = [{ source: 'claude-code', name: 'Claude Code', state: 'not_installed' }, { source: 'codex', name: 'Codex', state: 'not_installed' }];
		// The status is read when the screen mounts, so mount it again now that this computer is eligible.
		await unmount(component!); component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } }); flushSync();
		await passFolder();
		expect(host.textContent).toContain('Claude Code and Codex are the apps you’ll sign in to next.');
		expect(host.textContent).toContain('No subscription? The free model downloading to this computer works too, or add an API key.');
		expect(host.textContent).not.toContain('Add an API key on the next screen.');
	});

	it('explains the ChatGPT question when Claude Code is signed in, and the Claude question when Codex is', async () => {
		sources = [{ source: 'claude-code', name: 'Claude Code', state: 'ready' }, { source: 'codex', name: 'Codex', state: 'not_installed' }];
		await passFolder();
		expect(host.querySelector('.lede')?.textContent).toBe('CORSIA’s agents can work through a Claude or ChatGPT subscription you already pay for.');
		expect(host.textContent).toContain('A ChatGPT subscription uses Codex (a Claude subscription uses Claude Code). If you pay, CORSIA installs it now and you sign in on the next screen. If not, Claude Code is enough to continue.');
		expect(host.textContent).not.toContain('No subscription?');
	});

	it('explains the Claude question when Codex is signed in', async () => {
		sources = [{ source: 'claude-code', name: 'Claude Code', state: 'not_installed' }, { source: 'codex', name: 'Codex', state: 'ready' }];
		await passFolder();
		expect(host.textContent).toContain('A Claude subscription uses Claude Code (a ChatGPT subscription uses Codex). If you pay, CORSIA installs it now and you sign in on the next screen. If not, Codex is enough to continue.');
		expect(host.textContent).not.toContain('No subscription?');
	});

	it('says what the work folder is for beside its row on the account form', async () => {
		await settle(); button('Continue')?.click(); await settle();
		expect(host.textContent).toContain('Your projects and files live here.');
	});

	it('asks only about the other subscription when one tool is signed in', async () => {
		sources = [{ source: 'claude-code', name: 'Claude Code', state: 'ready' }, { source: 'codex', name: 'Codex', state: 'not_installed' }];
		await passFolder();
		expect(host.textContent).toContain('Claude Code is signed in.');
		expect(host.textContent).toContain('Do you also pay for ChatGPT?');
		button('No')?.click(); await settle();
		expect(rowNames()).toEqual(['Claude Code', 'Local model', 'API provider']);
	});

	it('says a signed-in account without the tool does not include it, and its switch unpicks the tool', async () => {
		const account = { account_id: 'a1', label: 'me@example.com', label_verified: true, identified: true, state: 'no_usable_model', resets_at: null, corsia_agent_binding: false };
		sources = [{ source: 'claude-code', name: 'Claude Code', state: 'ready', accounts: [account] } as never, { source: 'codex', name: 'Codex', state: 'not_installed' }];
		await passFolder();
		button('Claude')?.click(); await settle();
		expect(host.textContent).toContain('This Claude account has no models for Claude Code. Sign in with another account, or turn Claude Code off.');
		progressBodies.length = 0;
		host.querySelector<HTMLButtonElement>('button[aria-label="Claude Code"]')!.click(); await settle();
		expect(progressBodies.map((body) => JSON.parse(body).picks)).toContainEqual({ claude: false, chatgpt: false });
		expect(rowNames()).not.toContain('Claude Code');
	});

	it('records nothing about the other tool when a row is turned off in a resumed setup, where the subscriptions question was not answered', async () => {
		const account = { account_id: 'a1', label: 'me@example.com', label_verified: true, identified: true, state: 'no_usable_model', resets_at: null, corsia_agent_binding: false };
		sources = [{ source: 'claude-code', name: 'Claude Code', state: 'ready', accounts: [account] } as never, { source: 'codex', name: 'Codex', state: 'ready' }];
		const base = vi.mocked(fetch).getMockImplementation()!;
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			if (String(input) === '/api/setup/progress' && (init?.method ?? 'GET') === 'GET') return json({ setup_completed: false, status: { local_eligible: false, work_folder: '~/CORSIA' } });
			return base(input, init);
		}));
		await unmount(component);
		token.set('setup-token');
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn(), resume: true } });
		flushSync(); await settle(); await settle();
		expect(rowNames()).toContain('Claude Code');
		progressBodies.length = 0;
		host.querySelector<HTMLButtonElement>('button[aria-label="Claude Code"]')!.click(); await settle();
		expect(progressBodies.map((body) => JSON.parse(body)).filter((body) => 'picks' in body)).toEqual([]);
		expect(rowNames()).not.toContain('Claude Code');
		expect(rowNames()).toContain('Codex');
	});

	it('skips the subscriptions screen when both tools are signed in, and Back returns to the account', async () => {
		sources = [{ source: 'claude-code', name: 'Claude Code', state: 'ready' }, { source: 'codex', name: 'Codex', state: 'ready' }];
		await passFolder();
		expect(host.querySelector('h1')?.textContent).toBe('Providers & models');
		button('Back')?.click(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Your account');
		expect(host.querySelector('[data-testid="setup-folder-made"]')?.textContent).toContain('CORSIA keeps your work in');
		expect(host.querySelector('#setup-name')).toBeTruthy();
	});

	it('goes Back from models to subscriptions to the account, and it never creates a second account', async () => {
		await passFolder();
		button('Neither')?.click(); await settle();
		button('Back')?.click(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions');
		button('Back')?.click(); await settle();
		// The account exists, so Back shows the form again, filled, and Continue with nothing changed writes nothing.
		expect(host.querySelector('h1')?.textContent).toBe('Your account');
		expect((host.querySelector('#setup-name') as HTMLInputElement).value).toBe('Ada Lovelace');
		expect((host.querySelector('#setup-username') as HTMLInputElement).value).toBe('ada');
		expect(button('Continue')?.disabled).toBe(false);
		button('Continue')?.click(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions');
		expect(calls.filter((call) => call.url === '/api/setup/first-account')).toHaveLength(1);
	});

	it('returns an existing-account response to the account form with Sign in', async () => {
		await openAccount(); fillAccount(); await settle();
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => String(input) === '/api/setup/first-account' ? json({}, 409) : json({})));
		button('Continue')?.click(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Create your account');
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
		await openAccount(); fillAccount(); await settle();
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => String(input) === '/api/setup/first-account' ? json(body, 422) : json({})));
		button('Continue')?.click(); await settle();
		expect(host.textContent).toContain(expected);
		expect(getByRole('alert').textContent).toContain(expected);
		expect(button('Continue')?.disabled).toBe(false);
	});

	describe('Back to the account after it was made (editable account)', () => {
		beforeEach(() => {
			vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
				const url = String(input); const method = init?.method ?? 'GET'; calls.push({ url, method });
				if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: false, work_folder: '~/CORSIA' });
				if (url === '/api/setup/first-account') return json({ token: 'first-token', user: { id: 'ada', name: 'Ada Lovelace' } });
				if (url === '/api/setup/account' && method === 'PUT') {
					const changes = JSON.parse(String(init?.body));
					return json(changes.password ? { ok: true, token: 'renewed-token', user: { id: 'ada', name: 'Ada Lovelace' } } : { ok: true });
				}
				if (url.includes('/sources')) return json({ sources });
				if (url.includes('/models')) return json({ models: [] });
				return json({});
			});
		});

		it('fills the form with what was entered, keeps the username read-only and leaves the passwords blank', async () => {
			await backToAccount();
			expect((host.querySelector('#setup-name') as HTMLInputElement).value).toBe('Ada Lovelace');
			const username = host.querySelector('#setup-username') as HTMLInputElement;
			expect(username.value).toBe('ada');
			expect(username.readOnly).toBe(true);
			expect((host.querySelector('#setup-password') as HTMLInputElement).value).toBe('');
			expect((host.querySelector('#setup-confirm-password') as HTMLInputElement).value).toBe('');
			// A blank pair reads as optional, not as something still to fill in.
			expect(host.querySelector('label[for="setup-password"]')?.textContent).toContain('New password (optional)');
			expect(host.querySelector('label[for="setup-confirm-password"]')?.textContent).toContain('Confirm new password');
			// The folder was made with the account, so it reads back and cannot be changed here.
			expect(host.querySelector('[data-testid="setup-folder-made"]')?.textContent).toContain('CORSIA keeps your work in ~/CORSIA');
			expect(button('Change')).toBeUndefined();
		});

		it('makes no call at all when nothing changed, and never creates a second account', async () => {
			await backToAccount();
			button('Continue')?.click(); await settle();
			expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions');
			expect(putCalls()).toHaveLength(0);
			expect(calls.filter((call) => call.url === '/api/setup/first-account')).toHaveLength(1);
		});

		it('treats a name re-entered unchanged, with only spaces added, as no change', async () => {
			await backToAccount();
			setInput('setup-name', '  Ada Lovelace '); await settle();
			button('Continue')?.click(); await settle();
			expect(putCalls()).toHaveLength(0);
		});

		it('saves only the name when only the name changed', async () => {
			await backToAccount();
			setInput('setup-name', 'Ada King'); await settle();
			expect((host.querySelector('#setup-username') as HTMLInputElement).value).toBe('ada');
			button('Continue')?.click(); await settle();
			expect(putBodies()).toEqual([{ name: 'Ada King' }]);
			expect(putCalls()[0][1]?.headers).toMatchObject({ Authorization: 'Bearer first-token' });
			expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions');
		});

		it('saves only the email when only the email changed, and an emptied email removes it', async () => {
			await backToAccount();
			setInput('setup-email', 'ada@example.test'); await settle();
			button('Continue')?.click(); await settle();
			expect(putBodies()).toEqual([{ email: 'ada@example.test' }]);
			button('Back')?.click(); await settle();
			setInput('setup-email', ''); await settle();
			button('Continue')?.click(); await settle();
			expect(putBodies()).toEqual([{ email: 'ada@example.test' }, { email: '' }]);
		});

		it('saves a new password with its confirmation and carries on with the session the server returns', async () => {
			await backToAccount();
			setInput('setup-password', 'newpassword1'); setInput('setup-confirm-password', 'newpassword1'); await settle();
			const connected = vi.mocked(connect).mock.calls.length;
			button('Continue')?.click(); await settle();
			expect(putBodies()).toEqual([{ password: 'newpassword1' }]);
			expect(get(token)).toBe('renewed-token');
			// The old session ended with the old password, so the socket reconnects on the new one.
			expect(connect).toHaveBeenCalledTimes(connected + 1);
			expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions');
		});

		it('holds Continue while a changed password is too short or does not match its confirmation', async () => {
			await backToAccount();
			setInput('setup-password', 'short'); setInput('setup-confirm-password', 'short'); await settle();
			expect(button('Continue')?.disabled).toBe(true);
			setInput('setup-password', 'newpassword1'); setInput('setup-confirm-password', 'different1'); await settle();
			expect(button('Continue')?.disabled).toBe(true);
			expect(putCalls()).toHaveLength(0);
			setInput('setup-confirm-password', 'newpassword1'); await settle();
			expect(button('Continue')?.disabled).toBe(false);
		});

		it('holds Continue when the name is emptied', async () => {
			await backToAccount();
			setInput('setup-name', ''); await settle();
			expect(button('Continue')?.disabled).toBe(true);
		});

		it('sends the changed fields together in one save', async () => {
			await backToAccount();
			setInput('setup-name', 'Ada King'); setInput('setup-email', 'ada@example.test'); await settle();
			button('Continue')?.click(); await settle();
			expect(putBodies()).toEqual([{ name: 'Ada King', email: 'ada@example.test' }]);
		});

		it('sends the name along once when the profile an email belongs in is missing, instead of looping', async () => {
			await backToAccount();
			const answer = vi.mocked(fetch).getMockImplementation()!;
			let refused = false;
			vi.mocked(fetch).mockImplementation(async (input, init) => {
				if (String(input) === '/api/setup/account' && !JSON.parse(String(init?.body)).name && !refused) { refused = true; return json({ code: 'profile_missing' }, 422); }
				return answer(input, init);
			});
			setInput('setup-email', 'ada@example.test'); await settle();
			button('Continue')?.click(); await settle();
			expect(putBodies()).toEqual([{ email: 'ada@example.test' }, { name: 'Ada Lovelace', email: 'ada@example.test' }]);
			expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions');
		});

		it('stays on the screen with the reason when the save is refused, and with a plain line when it fails', async () => {
			await backToAccount();
			const answer = vi.mocked(fetch).getMockImplementation()!;
			vi.mocked(fetch).mockImplementation(async (input, init) => String(input) === '/api/setup/account' ? json({ detail: 'Use a password of at least 8 characters.' }, 422) : answer(input, init));
			setInput('setup-name', 'Ada King'); await settle();
			button('Continue')?.click(); await settle();
			expect(host.querySelector('h1')?.textContent).toBe('Your account');
			expect(getByRole('alert').textContent).toContain('Use a password of at least 8 characters.');
			vi.mocked(fetch).mockImplementation(async (input, init) => String(input) === '/api/setup/account' ? json({ code: 'setup_failed', ref: 'K7QM2X' }, 500) : answer(input, init));
			button('Continue')?.click(); await settle();
			expect(host.querySelector('h1')?.textContent).toBe('Your account');
			expect(getByRole('alert').textContent).toContain('Couldn’t save your account. Try again.');
			// A system failure carries the reference that finds its log entry.
			expect(getByRole('alert').textContent).toContain('Reference: K7QM2X');
			// What was typed stays, and the next press tries the same change again.
			expect((host.querySelector('#setup-name') as HTMLInputElement).value).toBe('Ada King');
		});

		it('ends the account step instead of offering a retry when setup finished somewhere else', async () => {
			await backToAccount();
			const answer = vi.mocked(fetch).getMockImplementation()!;
			vi.mocked(fetch).mockImplementation(async (input, init) => String(input) === '/api/setup/account' ? json({ code: 'setup_complete' }, 409) : answer(input, init));
			setInput('setup-name', 'Ada King'); await settle();
			button('Continue')?.click(); await settle();
			expect(finished).toHaveBeenCalledTimes(1);
			expect(host.querySelector('[role="alert"]')).toBeNull();
		});

		it('goes Back to Getting started from the account screen', async () => {
			await backToAccount();
			button('Back')?.click(); await settle();
			expect(host.querySelector('h1')?.textContent).toBe(APPROVED_WELCOME.title);
			expect(putCalls()).toHaveLength(0);
		});
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
		button('Continue')?.click(); await settle();
		await vi.waitFor(() => expect(button('Neither')).toBeTruthy()); button('Neither')?.click(); await settle();
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
		const accountRow = [...host.querySelectorAll('.lbl')].find((label) => label.textContent === 'Account & workspace')?.closest('*:has(> .lead)');
		expect(accountRow).toBeTruthy();
		expect(accountRow?.querySelector('.done-check')).toBeNull();
	});
});
