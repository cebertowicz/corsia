import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';
import { providers } from '$lib/stores/providers';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const picked = () => json({ state: 'picked', model: { name: 'Qwen 3.8', repository: 'example/model', download_size_bytes: 30_000_000_000, download_size_gb: 30, context_window: 65536 }, message: null });
const claude = (state: string, extra: Record<string, unknown> = {}) => ({ source: 'claude-code', name: 'Claude Code', state, ...extra });
const codex = (state: string) => ({ source: 'codex', name: 'Codex', state });
const localReady = { source: 'omlx', name: 'This computer', state: 'ready' };
const localDownloading = { source: 'omlx', name: 'This computer', state: 'needs_setup', setup_downloading: true, setup_stage: 'Downloading', download_percent: 38, download_eta_seconds: 840 };
const noLocal = { apple_silicon: true, macos_supported: true, local_eligible: false };
const note = () => host.querySelector('footer')?.textContent ?? '';

type Fake = { status: Record<string, unknown>; statusReads: number; sources: Record<string, unknown>[]; install: { state: string; reason: string | null; path_shadowed_by?: string | null }; cancelAnswer: { state: string; reason: string | null } | null; signin: string; posted: string[]; pick: unknown; providerList?: unknown[]; progressWrites?: Record<string, unknown>[] };
let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let fake: Fake;

async function settle() { await tick(); await new Promise((done) => setTimeout(done)); await tick(); }

function install(overrides: Partial<Fake> = {}) {
	fake = { status: { apple_silicon: true, macos_supported: true, local_eligible: true, work_folder: '~/CORSIA' }, statusReads: 0, sources: [], install: { state: 'running', reason: null }, cancelAnswer: null, signin: 'waiting', posted: [], pick: null, ...overrides };
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		const method = init?.method ?? 'GET';
		fake.posted.push(`${method} ${url}`);
		if (url === '/api/setup/progress' && method === 'PUT') { (fake.progressWrites ??= []).push(JSON.parse(String(init?.body))); return json({ ok: true }); }
		if (url === '/api/setup/status') { fake.statusReads += 1; return json(typeof fake.status === 'function' ? (fake.status as () => unknown)() : fake.status); }
		if (url.startsWith('/api/setup/local-model')) return fake.pick ? json(fake.pick) : picked();
		if (url === '/api/setup/local-download') return json({ in_progress: true });
		if (url === '/api/setup/first-account') return json({ token: 'setup-session', user: { id: 'setup-user', name: 'First Person' } });
		if (url === '/api/providers/sources/recheck' || url === '/api/providers/sources') return json({ sources: fake.sources });
		if (url === '/api/providers/cli/claude-code/install' && method === 'POST') return json({ ok: true });
		if (url === '/api/providers/cli/claude-code/install' && method === 'DELETE') {
			if (!fake.cancelAnswer) return json({ detail: 'down' }, 500);
			fake.install = fake.cancelAnswer;
			return json(fake.cancelAnswer);
		}
		if (url === '/api/providers/cli/claude-code/install') return json(fake.install);
		if (url.startsWith('/api/corsia-agent?')) return json({ target: { thread_id: 'conductor-1' } });
		if (url.startsWith('/api/vendor-accounts?provider=')) return json({ accounts: [] });
		if (url === '/api/vendor-accounts') return json({ id: 'acct-1' });
		if (url === '/api/vendor-accounts/acct-1/signin') return json({ account_id: 'acct-1', status: 'waiting' });
		if (url === '/api/vendor-accounts/acct-1/signin/check') return json({ account_id: 'acct-1', status: fake.signin });
		if (url.startsWith('/api/settings/providers') && method === 'GET') return json({ providers: fake.providerList ?? [{ name: 'anthropic', label: 'Anthropic', type: 'cloud' }, { name: 'openai', label: 'OpenAI', type: 'cloud' }] });
		if (url.startsWith('/api/providers') && method === 'PUT') return json({ ok: true });
		return json({ sources: [] });
	}));
}

async function open(overrides: Partial<Fake> = {}) {
	install(overrides);
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
	flushSync();
	await settle();
}

const checkbox = (name: string) => Array.from(host.querySelectorAll<HTMLButtonElement>('[role="checkbox"]')).find((item) => item.textContent?.includes(name))!;
const toggle = () => host.querySelector<HTMLButtonElement>('button[aria-label="Also run a local model on this computer"]');
const button = (text: string) => Array.from(host.querySelectorAll<HTMLButtonElement>('button')).find((item) => item.textContent?.trim() === text);
const row = (name: string) => host.querySelector<HTMLElement>(`section[aria-label="${name}"]`);
const rowButtons = (name: string) => Array.from(row(name)?.querySelectorAll('button') ?? []).map((item) => item.textContent?.trim());
const jobLabels = () => Array.from(host.querySelectorAll('.job-row .lbl')).map((item) => item.textContent?.trim());
const click = async (item: HTMLElement | null | undefined) => { item?.click(); await settle(); };

async function createAccount() {
	await click(button('Get started'));
	await fillAccount();
}
async function fillAccount() {
	for (const [input, value] of [
		[host.querySelector('input[autocomplete="name"]'), 'First Person'],
		[host.querySelector('input[autocomplete="username"]'), 'first'],
		...Array.from(host.querySelectorAll('input[autocomplete="new-password"]')).map((input) => [input, 'correct-password']),
	] as [HTMLInputElement, string][]) {
		input.value = value;
		input.dispatchEvent(new Event('input', { bubbles: true }));
	}
	await tick();
	await click(button('Continue'));
}
const refocus = async () => { window.dispatchEvent(new Event('focus')); await settle(); };

beforeEach(() => {
	token.set(null); user.set(null); providers.set([]);
	host = document.createElement('div'); document.body.appendChild(host);
});

afterEach(async () => {
	if (component) await unmount(component);
	component = null;
	host.remove(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe('Welcome asks which subscription the person pays for', () => {
	it('offers three unchecked picks, and keeps the local switch on and locked until one is checked', async () => {
		await open();

		expect(host.textContent).toContain('Which of these do you pay for?');
		expect(host.textContent).toContain('Choose any that apply. CORSIA sets them up after you create your account.');
		expect(host.textContent).toContain('CORSIA installs Claude Code, made by Anthropic, and you sign in.');
		expect(host.textContent).toContain('CORSIA installs Codex, made by OpenAI, and you sign in.');
		expect(host.textContent).toContain('From Anthropic, OpenAI or another provider. You paste it after you create your account.');
		expect(host.textContent).not.toContain('Neither');
		const picks = Array.from(host.querySelectorAll('[role="checkbox"]'));
		expect(picks.map((item) => item.getAttribute('aria-checked'))).toEqual(['false', 'false', 'false']);
		expect(toggle()?.getAttribute('aria-pressed')).toBe('true');
		expect(host.textContent).toContain('On until you pick one above.');
		// Locked, not disabled: it stays in the tab order at full colour, says why, and does nothing when pressed.
		expect(toggle()?.disabled).toBe(false);
		expect(toggle()?.getAttribute('aria-disabled')).toBe('true');
		const why = host.querySelector(`#${toggle()?.getAttribute('aria-describedby')}`);
		expect(why?.textContent).toBe('On until you pick one above.');
		await click(toggle());
		expect(toggle()?.getAttribute('aria-pressed')).toBe('true');
		expect(button('Get started')?.disabled).toBe(false);
	});

	it('keeps Welcome’s approved copy and names the model on this computer a local model', async () => {
		await open();

		expect(host.textContent).toContain('Your own AI team, on this computer. It remembers your projects, works with the tools and models you already use, and shows you results, not transcripts.');
		expect(host.textContent).toContain('Free, private, and works offline. It’s slower than Claude or ChatGPT, so it’s for work you’d rather keep on this computer, and you can move background jobs onto it.');
		expect(host.textContent).toContain('Qwen 3.8 · 30 GB · runs in oMLX');
		expect(host.textContent).toContain('The download starts in the background when you press Get started.');
		expect(host.textContent).not.toMatch(/about \d+ min|minutes/);
	});

	it('lets the three picks stack, and unlocks the switch once any is checked', async () => {
		await open();

		await click(checkbox('Claude'));
		await click(checkbox('ChatGPT'));
		await click(checkbox('A pay-as-you-go API key'));
		expect(Array.from(host.querySelectorAll('[role="checkbox"]')).map((item) => item.getAttribute('aria-checked'))).toEqual(['true', 'true', 'true']);
		expect(toggle()?.disabled).toBe(false);
		expect(host.textContent).not.toContain('On until you pick one above.');

		await click(checkbox('Claude'));
		expect(checkbox('Claude').getAttribute('aria-checked')).toBe('false');
		expect(checkbox('ChatGPT').getAttribute('aria-checked')).toBe('true');
	});

	it('turns the local switch back on when the last pick is unchecked after it was turned off', async () => {
		await open();
		await click(checkbox('Claude'));
		await click(toggle());
		expect(toggle()?.getAttribute('aria-pressed')).toBe('false');

		await click(checkbox('Claude'));

		expect(toggle()?.getAttribute('aria-pressed')).toBe('true');
		expect(toggle()?.getAttribute('aria-disabled')).toBe('true');
		// The record follows the switch, so a reload does not bring back the earlier off.
		expect(fake.progressWrites?.filter((write) => 'local_ai' in write)).toEqual([{ local_ai: false }, { local_ai: true }]);
	});

	it('says the local model is off, and hides the download line, once the switch is turned off', async () => {
		await open();
		await click(checkbox('Claude'));
		expect(host.textContent).toContain('The download starts in the background when you press Get started.');

		await click(toggle());

		expect(host.textContent).toContain('Off. You can turn it on later in Settings.');
		expect(host.textContent).not.toContain('The download starts in the background when you press Get started.');
	});

	it('shows one line instead of the question when a tool is already ready', async () => {
		await open({ status: { apple_silicon: true, macos_supported: true, local_eligible: true, ready_ai: 'Claude Code' } });

		expect(host.textContent).toContain('Claude Code is ready. CORSIA uses it for chat.');
		expect(host.textContent).not.toContain('Which of these do you pay for?');
		expect(host.querySelectorAll('[role="checkbox"]').length).toBe(0);
		expect(toggle()?.disabled).toBe(false);
	});

	it('gates Get started on a pick when this computer cannot run a local model, with the low-memory line as the reason', async () => {
		await open({ status: { apple_silicon: true, macos_supported: true, local_eligible: false } });

		expect(toggle()).toBeNull();
		expect(host.textContent).toContain('This computer doesn’t have enough memory to run a local model, so pick at least one above.');
		expect(button('Get started')?.disabled).toBe(true);
		await click(checkbox('A pay-as-you-go API key'));
		expect(button('Get started')?.disabled).toBe(false);
		expect(host.textContent).not.toContain('so pick at least one above');
	});

	it('does not gate Get started on a computer that cannot run a local model when a tool is ready', async () => {
		await open({ status: { apple_silicon: true, macos_supported: true, local_eligible: false, ready_ai: 'Codex' } });

		expect(button('Get started')?.disabled).toBe(false);
		expect(host.textContent).toContain('Codex is ready. CORSIA uses it for chat.');
	});

	it('holds the question behind a one-line spinner until the install check answers, then shows it once', async () => {
		let reads = 0;
		await open({ status: (() => { reads += 1; return { apple_silicon: true, macos_supported: true, local_eligible: true, ai_check_pending: reads < 2 }; }) as never });

		expect(host.textContent).toContain('Checking for Claude Code and Codex…');
		// The question is laid out but hidden, so the spinner holds its height; the locked line waits for the question.
		expect(host.querySelector('.question')?.classList.contains('waiting')).toBe(true);
		expect(host.querySelector('.stack .detecting')).not.toBeNull();
		expect(toggle()).not.toBeNull();
		expect(host.textContent).not.toContain('On until you pick one above.');
		expect(toggle()?.hasAttribute('aria-describedby')).toBe(false);
		expect(button('Get started')?.disabled).toBe(true);

		await new Promise((done) => setTimeout(done, 700));
		await settle();
		expect(host.textContent).not.toContain('Checking for Claude Code and Codex…');
		expect(host.textContent).toContain('Which of these do you pay for?');
		expect(host.querySelector('.question')?.classList.contains('waiting')).toBe(false);
		expect(host.textContent).toContain('On until you pick one above.');
		expect(button('Get started')?.disabled).toBe(false);
	});

	it('swaps the spinner for the ready line when the check finds a tool', async () => {
		let reads = 0;
		await open({ status: (() => { reads += 1; return { apple_silicon: true, macos_supported: true, local_eligible: true, ai_check_pending: reads < 2, ready_ai: reads < 2 ? null : 'Claude Code' }; }) as never });

		await new Promise((done) => setTimeout(done, 700));
		await settle();
		expect(host.textContent).toContain('Claude Code is ready. CORSIA uses it for chat.');
		expect(host.textContent).not.toContain('Which of these do you pay for?');
	});

	it('asks anyway after the wait is capped, and the question never collapses when a tool turns up later', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'Date'] });
		let ready: string | null = null;
		install({ status: (() => ({ apple_silicon: true, macos_supported: true, local_eligible: true, ai_check_pending: true, ready_ai: ready })) as never });
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync();
		await vi.advanceTimersByTimeAsync(100);
		expect(host.textContent).toContain('Checking for Claude Code and Codex…');

		await vi.advanceTimersByTimeAsync(2600);
		expect(host.textContent).toContain('Which of these do you pay for?');

		ready = 'Claude Code';
		await vi.advanceTimersByTimeAsync(2000);
		expect(host.textContent).toContain('Which of these do you pay for?');
		expect(host.textContent).not.toContain('is ready. CORSIA uses it for chat.');
	});

	const windowHeight = (short: boolean) => vi.stubGlobal('matchMedia', (query: string) => ({ matches: short && query.includes('max-height'), media: query, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() }));

	it.each([['the short window', true], ['the default window', false]])('holds oMLX’s terms behind one What CORSIA changes control at %s, and opens them in a popover', async (_name, short) => {
		windowHeight(short);
		await open({ status: { apple_silicon: true, macos_supported: true, local_eligible: true, existing_omlx: true } });

		expect(host.querySelector('.disclose')).toBeNull();
		expect(button('Details')).toBeUndefined();
		expect(Array.from(host.querySelectorAll('button')).filter((item) => item.textContent?.trim() === 'What CORSIA changes')).toHaveLength(1);
		expect(host.querySelectorAll('[role="radio"]').length).toBe(2);
		// The control is a collapsed link under the choices, after the last one in the page, not beside the found line.
		const lastChoice = Array.from(host.querySelectorAll('[role="radio"]')).pop()!;
		expect(lastChoice.compareDocumentPosition(button('What CORSIA changes')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
		expect(button('What CORSIA changes')!.closest('.spec')).toBeNull();
		await click(button('What CORSIA changes'));
		const terms = document.querySelector('[data-popover] .disclose');
		expect(terms?.textContent).toContain('Letting CORSIA manage it means CORSIA reads your oMLX’s admin key');
		expect(terms?.textContent).toContain('Max Concurrent Requests and Prefill Priority');
	});

	it('words the two oMLX choices as approved, with Let CORSIA manage it still the default', async () => {
		windowHeight(false);
		await open({ status: { apple_silicon: true, macos_supported: true, local_eligible: true, existing_omlx: true } });

		const [manage, mine] = Array.from(host.querySelectorAll('[role="radio"]'));
		expect(manage.getAttribute('aria-checked')).toBe('true');
		expect(manage.textContent).toContain('Let CORSIA manage it');
		expect(manage.textContent).toContain('Recommended. CORSIA sets up oMLX for the model it installs. You can put your own settings back in Settings any time.');
		expect(mine.textContent).toContain('CORSIA uses your models as they are and changes nothing in oMLX.');
	});
});

describe('the This computer row', () => {
	const own = { source: 'omlx', name: 'This computer', state: 'needs_setup' };
	const starterNote = "oMLX didn’t recommend a model for this computer, so CORSIA chose Ternary Bonsai 2.";
	const starter = (message: string | null) => ({ state: 'picked', model: { name: 'Ternary Bonsai 2', repository: 'prism-ml/Ternary-Bonsai-2-27B-mlx-2bit', download_size_bytes: 8_608_739_065, download_size_gb: 8.6, context_window: 32768 }, message });

	async function onTheStep(overrides: Partial<Fake>) {
		await open({ sources: [claude('not_installed'), codex('not_installed'), own], ...overrides });
		await click(checkbox('Claude'));
		await createAccount();
	}

	it('says what a local model is and its download size, with Download as the action', async () => {
		await onTheStep({});

		expect(row('This computer')?.textContent).toContain('Free and private, and works offline. 30 GB download.');
		expect(row('This computer')?.textContent).not.toContain('Local model');
		expect(rowButtons('This computer')).toEqual(['Download']);
	});

	it('leaves the size out when it is not known', async () => {
		await onTheStep({ pick: { state: 'failed', model: null, message: null } });

		const meta = row('This computer')?.textContent ?? '';
		expect(meta).toContain('Free and private, and works offline.');
		expect(meta).not.toMatch(/download\./);
		expect(meta).not.toContain('a local model');
	});

	it('says why it chose the starter model when oMLX recommended none, naming the model the server read from its profile', async () => {
		await onTheStep({ pick: starter(starterNote) });

		expect(row('This computer')?.textContent).toContain(starterNote);
		expect(row('This computer')?.textContent).toContain('Free and private, and works offline. 8.6 GB download.');
	});

	it('adds no starter note when oMLX recommended the model', async () => {
		await onTheStep({ pick: starter(null) });

		expect(host.textContent).not.toContain("didn't recommend");
	});

	describe('with the person’s own oMLX under management', () => {
		const managed = (starter_note: string | null) => ({ state: 'managed', cannot_check: null, required_version: '0.7.1rc1', restores: [], forgets_key: true, starter_note, starter_name: starter_note ? 'Ternary Bonsai 2' : null });
		// The server names the source "Local model"; the job label is that unless the management view names the starter.
		const downloading = (starter_note: string | null) => ({ ...localDownloading, name: 'Local model', omlx_management: managed(starter_note) });

		async function onTheStepAfterUseIt(starter_note: string | null) {
			await open({ status: { apple_silicon: true, macos_supported: true, local_eligible: true, existing_omlx: true, work_folder: '~/CORSIA' },
				pick: { state: 'own_omlx', model: null, message: null }, sources: [claude('not_installed'), codex('not_installed'), downloading(starter_note)] });
			await click(checkbox('Claude'));
			await click(button('Get started'));
			await click(button('Use it'));
			await fillAccount();
		}

		it('shows the same starter note on the This computer row when their oMLX recommended none', async () => {
			await onTheStepAfterUseIt(starterNote);

			expect(row('This computer')?.textContent).toContain(starterNote);
			expect(row('This computer')?.textContent).toContain('Downloading in the background');
			expect(row('This computer')?.textContent).not.toContain('Local model');
		});

		it('names the background download after the starter model the server reports', async () => {
			await onTheStepAfterUseIt(starterNote);

			expect(jobLabels()).toEqual(['Ternary Bonsai 2']);
		});

		it('keeps the Local model label on the download when their oMLX recommended the model', async () => {
			await onTheStepAfterUseIt(null);

			expect(jobLabels()).toEqual(['Local model']);
		});

		it('adds no note when their oMLX recommended the model', async () => {
			await onTheStepAfterUseIt(null);

			expect(row('This computer')?.textContent).toContain('Downloading in the background');
			expect(host.textContent).not.toContain('didn’t recommend');
		});
	});

	it('keeps Couldn’t check for the offline case and never offers the starter model', async () => {
		await open({ pick: { state: 'failed', model: null, message: 'oMLX could not be reached.' } });

		expect(host.textContent).toContain('CORSIA couldn’t check which local AI model fits this computer.');
		expect(host.textContent).not.toContain("didn't recommend");
		expect(host.textContent).not.toContain('Bonsai');
	});
});

describe('Cancel on a running install', () => {
	const cancelled = { state: 'cancelled', reason: null };
	const installs = () => fake.posted.filter((call) => call === 'POST /api/providers/cli/claude-code/install').length;

	async function installing() {
		await open({ sources: [claude('not_installed'), codex('not_installed')], cancelAnswer: cancelled });
		await click(checkbox('Claude'));
		await createAccount();
		expect(row('Claude Code')?.textContent).toContain('Installing Claude Code');
	}

	it('sits in the row\u2019s action slot next to the spinner, and the slot holds nothing else', async () => {
		await installing();

		const slot = row('Claude Code')!.querySelector('.row-actions')!;
		expect(rowButtons('Claude Code')).toEqual(['Cancel']);
		expect(slot.children.length).toBe(2);
		expect(slot.firstElementChild?.tagName).not.toBe('BUTTON');
		expect(slot.lastElementChild?.textContent?.trim()).toBe('Cancel');
	});

	it('stops the installer and returns the row to Set up, without starting another install', async () => {
		await installing();

		await click(row('Claude Code')!.querySelector<HTMLButtonElement>('button'));

		expect(fake.posted).toContain('DELETE /api/providers/cli/claude-code/install');
		expect(rowButtons('Claude Code')).toEqual(['Set up']);
		expect(row('Claude Code')?.textContent).toContain('CORSIA installs Claude Code for you.');
		expect(row('Claude Code')?.textContent).not.toContain('Installing');
		await refocus();
		expect(installs()).toBe(1);
		expect(rowButtons('Claude Code')).toEqual(['Set up']);
	});

	it('moves keyboard focus to Set up when Cancel unmounts, so a keyboard person is not dropped on the page', async () => {
		await installing();
		const cancel = row('Claude Code')!.querySelector<HTMLButtonElement>('button')!;
		cancel.focus();
		expect(document.activeElement).toBe(cancel);

		await click(cancel);

		const setUp = row('Claude Code')!.querySelector<HTMLButtonElement>('button')!;
		expect(setUp.textContent?.trim()).toBe('Set up');
		expect(document.activeElement).toBe(setUp);
	});

	it('returns the row to Set up when the install is found cancelled by the next look at it', async () => {
		await installing();

		fake.install = cancelled;
		await refocus();

		expect(rowButtons('Claude Code')).toEqual(['Set up']);
	});

	it('carries on to sign-in when the install had finished by the time Cancel landed', async () => {
		await installing();
		fake.cancelAnswer = { state: 'done', reason: null };
		fake.sources = [claude('needs_auth'), codex('not_installed')];

		await click(row('Claude Code')!.querySelector<HTMLButtonElement>('button'));

		expect(fake.posted).toContain('POST /api/vendor-accounts/acct-1/signin');
		expect(row('Claude Code')?.textContent).toContain('Finish signing in to your Claude account');
	});

	it('keeps the installing row when the cancel request fails', async () => {
		await installing();
		fake.cancelAnswer = null;

		await click(row('Claude Code')!.querySelector<HTMLButtonElement>('button'));

		expect(row('Claude Code')?.textContent).toContain('Installing Claude Code');
		expect(rowButtons('Claude Code')).toEqual(['Cancel']);
	});

	it('shows the failure when the installer failed before Cancel landed', async () => {
		await installing();
		fake.cancelAnswer = { state: 'failed', reason: 'no internet connection' };

		await click(row('Claude Code')!.querySelector<HTMLButtonElement>('button'));

		expect(rowButtons('Claude Code')).toContain('Try again');
		expect(row('Claude Code')?.textContent).toContain('no internet connection');
	});
});

describe('Connect an AI after a pick', () => {
	it('installs a checked tool by itself, then opens its sign-in, and lists nothing the person did not check', async () => {
		await open({ sources: [claude('not_installed'), codex('not_installed')] });
		await click(checkbox('Claude'));
		await createAccount();

		expect(fake.posted).toContain('POST /api/providers/cli/claude-code/install');
		expect(host.textContent).toContain('Setting up what you chose. You can add the others with CORSIA later.');
		expect(row('Claude Code')?.textContent).toContain('Installing Claude Code');
		expect(row('Claude Code')?.textContent).not.toMatch(/official installer|minutes|you chose/);
		expect(row('Codex')).toBeNull();
		expect(button('Set up')).toBeUndefined();

		fake.install = { state: 'done', reason: null };
		fake.sources = [claude('needs_auth'), codex('not_installed')];
		await refocus();
		expect(fake.posted).toContain('POST /api/vendor-accounts/acct-1/signin');
		expect(row('Claude Code')?.textContent).toContain('Finish signing in to your Claude account in the browser window that just opened.');
		expect(rowButtons('Claude Code')).toEqual(['Sign in']);
		expect(host.textContent).toContain('Ready when Claude Code is signed in.');
		expect(button('Start using CORSIA')?.disabled).toBe(true);

		fake.signin = 'completed';
		fake.sources = [claude('ready', { accounts: [{ state: 'ready' }] }), codex('not_installed')];
		await refocus();
		expect(row('Claude Code')?.textContent).toContain('Ready');
		expect(button('Start using CORSIA')?.disabled).toBe(false);
	});

	it('goes straight to sign-in for a checked tool that is installed but signed out', async () => {
		await open({ sources: [codex('needs_auth'), claude('not_installed')] });
		await click(checkbox('ChatGPT'));
		await createAccount();

		expect(fake.posted.some((call) => call.includes('/install'))).toBe(false);
		expect(fake.posted).toContain('POST /api/vendor-accounts/acct-1/signin');
		expect(row('Claude Code')).toBeNull();
		expect(rowButtons('Codex')).toEqual(['Sign in']);
	});

	it('sets up both checked tools', async () => {
		await open({ sources: [claude('not_installed'), codex('needs_auth')] });
		await click(checkbox('Claude'));
		await click(checkbox('ChatGPT'));
		await createAccount();

		expect(fake.posted).toContain('POST /api/providers/cli/claude-code/install');
		expect(fake.posted).toContain('POST /api/vendor-accounts/acct-1/signin');
		expect(row('Claude Code')).not.toBeNull();
		expect(row('Codex')).not.toBeNull();
	});

	it('opens an API key row ready to paste when the key was checked, lists no tool, and offers no other way to add one', async () => {
		await open({ sources: [claude('not_installed'), codex('not_installed')] });
		await click(checkbox('A pay-as-you-go API key'));
		await createAccount();

		expect(host.querySelector('input[aria-label="API key"]')).not.toBeNull();
		await settle();
		expect(document.activeElement?.getAttribute('aria-label')).toBe('API key');
		expect(row('Claude Code')).toBeNull();
		expect(row('Codex')).toBeNull();
		expect(host.textContent).not.toContain('Use an API key instead');
		expect(fake.posted.some((call) => call.includes('/install'))).toBe(false);
	});

	it('loads the providers when the key row opens, so its select shows Anthropic before anything is saved', async () => {
		await open({ sources: [claude('not_installed'), codex('not_installed')] });
		await click(checkbox('A pay-as-you-go API key'));
		await createAccount();
		await settle();

		expect(fake.posted.some((call) => call.startsWith('GET /api/settings/providers'))).toBe(true);
		const select = host.querySelector<HTMLSelectElement>('select[aria-label="Provider"]')!;
		expect(Array.from(select.options).map((option) => option.textContent)).toEqual(['Anthropic', 'OpenAI']);
		expect(select.value).toBe('anthropic');
		expect(select.selectedOptions[0]?.textContent).toBe('Anthropic');
	});

	it('records each pick as it is checked, so a reload can put them back', async () => {
		await open({ sources: [claude('not_installed'), codex('not_installed')] });
		await click(checkbox('Claude'));
		await click(checkbox('A pay-as-you-go API key'));
		await click(checkbox('Claude'));

		expect(fake.progressWrites?.filter((write) => write.picks)).toEqual([
			{ picks: { claude: true, chatgpt: false, key: false } },
			{ picks: { claude: true, chatgpt: false, key: true } },
			{ picks: { claude: false, chatgpt: false, key: true } },
		]);
	});

	it('says so when the provider list cannot be read, and keeps Save off', async () => {
		await open({ sources: [claude('not_installed'), codex('not_installed')], providerList: [] });
		await click(checkbox('A pay-as-you-go API key'));
		await createAccount();
		await settle();

		expect(host.textContent).toContain('CORSIA couldn’t load the list of providers. Try again.');
		const key = host.querySelector<HTMLInputElement>('input[aria-label="API key"]')!;
		key.value = 'pasted';
		key.dispatchEvent(new Event('input', { bubbles: true }));
		await settle();
		expect(button('Save')?.hasAttribute('disabled')).toBe(true);
	});

	it('shows no key row and no key button when nothing was checked, and keeps today’s Set up rows', async () => {
		await open({ sources: [claude('not_installed'), codex('not_installed')] });
		await createAccount();

		expect(host.querySelector('input[aria-label="API key"]')).toBeNull();
		expect(host.textContent).not.toContain('Use an API key instead');
		expect(rowButtons('Claude Code')).toEqual(['Set up']);
		expect(rowButtons('Codex')).toEqual(['Set up']);
		expect(fake.posted.some((call) => call.includes('/install'))).toBe(false);
	});

	it('says once, beside Start, that chat runs on the local model for now when a checked tool fails and the local model is ready', async () => {
		await open({ sources: [claude('not_installed'), localReady] });
		await click(checkbox('Claude'));
		fake.install = { state: 'failed', reason: 'no internet connection' };
		await createAccount();
		await refocus();

		expect(row('Claude Code')?.textContent).toContain('Couldn’t install Claude Code: no internet connection.');
		expect(host.textContent).not.toContain('Or start on the local model now');
		expect(host.textContent).not.toContain('until Claude Code is ready');
		expect(host.textContent?.split('Chat runs on the local model for now.').length).toBe(2);
		expect(button('Start using CORSIA')?.disabled).toBe(false);
	});

	it('does not say chat runs on the local model while the tool is still installing', async () => {
		await open({ sources: [claude('not_installed')] });
		await click(checkbox('Claude'));
		await createAccount();

		expect(host.textContent).not.toContain('Chat runs on the local model');
		expect(host.textContent).toContain('Ready when Claude Code is signed in.');
	});
});

describe('The wait line names only what the step shows', () => {
	const cancelClaude = () => click(row('Claude Code')!.querySelector<HTMLButtonElement>('button'));

	async function installingClaude(sources: Record<string, unknown>[], overrides: Partial<Fake> = {}) {
		await open({ sources, cancelAnswer: { state: 'cancelled', reason: null }, ...overrides });
		await click(checkbox('Claude'));
		await createAccount();
	}

	it('drops a cancelled install from the line, and tells the person to set it up when nothing else is moving', async () => {
		await installingClaude([claude('not_installed'), codex('not_installed')]);
		expect(note()).toContain('Ready when Claude Code is signed in.');

		await cancelClaude();

		expect(rowButtons('Claude Code')).toEqual(['Set up']);
		expect(note()).toContain('Set up Claude Code to continue.');
		expect(note()).not.toContain('signed in');
	});

	it('falls back to the download alone once the only install is cancelled', async () => {
		await installingClaude([claude('not_installed'), codex('not_installed'), localDownloading]);
		expect(note()).toContain('Ready when Claude Code is signed in or the download finishes in about 14 min.');

		await cancelClaude();

		expect(note()).toContain('Ready when the download finishes in about 14 min.');
		expect(note()).not.toContain('Claude Code');
	});

	it('counts an open key row as a route beside the download', async () => {
		await open({ sources: [claude('not_installed'), codex('not_installed'), localDownloading] });
		await click(checkbox('A pay-as-you-go API key'));
		await createAccount();

		expect(note()).toContain('Ready when you save your key or the download finishes in about 14 min.');
		expect(note()).not.toMatch(/Claude|Codex/);
	});

	it('says to save the key when the key is the only way on and there is no local model', async () => {
		await open({ status: noLocal, sources: [claude('not_installed'), codex('not_installed')] });
		await click(checkbox('A pay-as-you-go API key'));
		await createAccount();

		expect(note()).toContain('Save your API key to continue.');
		expect(note()).not.toMatch(/Claude|Codex|Connect/);
	});

	it('names the install and the key together when both are open', async () => {
		await open({ sources: [claude('not_installed'), codex('not_installed')], status: noLocal });
		await click(checkbox('A pay-as-you-go API key'));
		await click(checkbox('Claude'));
		await createAccount();

		expect(note()).toContain('Ready when Claude Code is signed in or you save your key.');
	});
});
