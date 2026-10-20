import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';
import type { SetupProgress } from '$lib/setupProgress';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
	status, headers: { 'Content-Type': 'application/json' },
});

const LOCAL = { source: 'omlx', name: 'Local model', state: 'needs_setup', setup_stage: 'Installing', download_percent: null };
const CODEX_SIGNED_OUT = { source: 'codex', name: 'Codex', state: 'needs_auth', accounts: [{ state: 'needs_auth' }] };
const CLAUDE_MISSING = { source: 'claude-code', name: 'Claude Code', state: 'not_installed', accounts: [] };
const CODEX_READY = { source: 'codex', name: 'Codex', state: 'ready', accounts: [{ state: 'ready' }] };

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let progress: SetupProgress | null;
let sources: Record<string, unknown>[];
let requests: string[];
let writes: Record<string, unknown>[];
let completionSaves: boolean;
let onFinished: ReturnType<typeof vi.fn<() => void>>;

function backend() {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		const method = init?.method ?? 'GET';
		requests.push(`${method} ${url}`);
		if (url === '/api/setup/progress' && method === 'PUT') {
			const change = JSON.parse(String(init?.body));
			writes.push(change);
			return change.completed && !completionSaves ? json({}, 500) : json({ ok: true });
		}
		if (url === '/api/setup/progress') return progress ? json(progress) : json({}, 500);
		if (url === '/api/providers/sources/recheck' || url === '/api/providers/sources') return json({ sources });
		return json({});
	}));
}

async function settle() { await tick(); await new Promise((done) => setTimeout(done)); await tick(); }

/** Open setup as a reload does: a session exists, the account exists, and the page starts over. */
async function reload() {
	if (component) await unmount(component);
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished, resume: true } });
	flushSync();
	await settle();
}

const rowNames = () => Array.from(host.querySelectorAll('section[aria-label]')).map((row) => row.getAttribute('aria-label'));
const button = (label: string) => Array.from(host.querySelectorAll('button')).find((candidate) => candidate.textContent?.trim() === label);

beforeEach(() => {
	token.set('session'); user.set(null);
	progress = { setup_completed: false, step: 'models', local_ai: null, status: { local_eligible: true, free_disk_gb: 200 } };
	sources = [LOCAL, CODEX_SIGNED_OUT];
	requests = []; writes = []; completionSaves = true;
	onFinished = vi.fn<() => void>();
	backend();
	host = document.createElement('div'); document.body.appendChild(host);
});

afterEach(async () => {
	if (component) await unmount(component);
	component = null;
	host.remove(); token.set(null); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe('SetupScreen resume', () => {
	it('lands on Connect an AI with the recorded choice, without asking the pre-account status gate', async () => {
		progress = { ...progress!, local_ai: true };
		await reload();

		expect(host.textContent).toContain('Connect an AI');
		expect(rowNames()).toContain('This computer');
		expect(requests).not.toContain('GET /api/setup/status');
	});

	it('shows the same sources after a reload as the first pass, whatever local-AI choice was recorded', async () => {
		for (const local_ai of [true, false, null]) {
			progress = { ...progress!, local_ai };
			await reload();
			expect(host.textContent).toContain('Connect an AI');
			expect(rowNames()).toContain('This computer');
			expect(rowNames()).toContain('Codex');
		}
	});

	it('puts back what was checked on Welcome, so a reload shows only those tools', async () => {
		sources = [LOCAL, CLAUDE_MISSING, CODEX_SIGNED_OUT];
		progress = { ...progress!, picks: { claude: false, chatgpt: true, key: false } };
		await reload();

		expect(rowNames()).toContain('Codex');
		expect(rowNames()).not.toContain('Claude Code');
		expect(host.querySelector('input[aria-label="API key"]')).toBeNull();
	});

	it('puts back a checked API key as an open key row after a reload', async () => {
		sources = [LOCAL, CLAUDE_MISSING, CODEX_SIGNED_OUT];
		progress = { ...progress!, picks: { claude: false, chatgpt: false, key: true } };
		await reload();

		expect(host.querySelector('input[aria-label="API key"]')).not.toBeNull();
		expect(rowNames()).not.toContain('Claude Code');
		expect(rowNames()).not.toContain('Codex');
	});

	it('reopens the oMLX question where it was left', async () => {
		progress = { ...progress!, step: 'local-consent', local_ai: true, status: { local_eligible: true, existing_omlx: true } };
		await reload();

		expect(host.textContent).toContain('Use your oMLX?');
	});

	it('does not reopen a setup the backend says is finished', async () => {
		progress = { setup_completed: true, step: null, local_ai: null };
		await reload();

		expect(onFinished).toHaveBeenCalledOnce();
		expect(host.textContent).not.toContain('Connect an AI');
	});

	it('offers a way forward when the record cannot be read', async () => {
		const saved = progress;
		progress = null;
		await reload();
		expect(host.textContent).toContain('CORSIA couldn’t start');

		progress = saved;
		button('Try again')?.click();
		await settle();
		expect(host.textContent).toContain('Connect an AI');
	});

	it('records the step it reopens at', async () => {
		await reload();

		expect(writes).toContainEqual({ step: 'models' });
	});

	it('records completion before the app opens, even with no model chosen', async () => {
		sources = [CODEX_READY];
		await reload();

		button('Start using CORSIA')?.click();
		await settle();

		expect(writes).toContainEqual({ completed: true });
		expect(onFinished).toHaveBeenCalledOnce();
	});

	it('stays on Connect an AI and says so when completion cannot be saved', async () => {
		sources = [CODEX_READY];
		completionSaves = false;
		await reload();

		button('Start using CORSIA')?.click();
		await settle();

		expect(onFinished).not.toHaveBeenCalled();
		expect(host.textContent).toContain('couldn’t save that setup is finished');

		completionSaves = true;
		button('Start using CORSIA')?.click();
		await settle();
		expect(onFinished).toHaveBeenCalledOnce();
	});

	it('keeps a signed-in person on Connect an AI when they decline the existing oMLX', async () => {
		progress = { ...progress!, step: 'local-consent', local_ai: true, status: { local_eligible: true, existing_omlx: true } };
		await reload();

		button('Not now')?.click();
		await settle();

		expect(host.textContent).toContain('Connect an AI');
		expect(host.textContent).not.toContain('Create your account');
		expect(writes).toContainEqual({ local_ai: false });
	});
});
