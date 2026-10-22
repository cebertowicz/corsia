import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let onFinished = vi.fn();

async function settle() { await tick(); await Promise.resolve(); await tick(); }
function click(label: string) {
	const button = Array.from(host.querySelectorAll('button')).find((candidate) => candidate.textContent?.trim() === label);
	expect(button, host.textContent ?? '').toBeTruthy(); button?.click();
}
function fill(id: string, value: string) {
	const input = host.querySelector<HTMLInputElement>(id)!;
	input.value = value; input.dispatchEvent(new Event('input', { bubbles: true }));
}

afterEach(async () => {
	if (component) await unmount(component);
	component = null; host?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); token.set(null); user.set(null);
});

async function openModels({ localEligible = false, localReplies = [], progressFails = false, sources = () => [], classifier }: { localEligible?: boolean; localReplies?: Array<Response | Error>; progressFails?: boolean; sources?: () => unknown[]; classifier?: unknown } = {}) {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: localEligible, work_folder: '~/CORSIA' });
		if ((url === '/api/setup/local-download' || url === '/api/providers/local/setup') && init?.method === 'POST') {
			const reply = localReplies.shift() ?? json({ ok: true });
			if (reply instanceof Error) throw reply;
			return reply;
		}
		if (url === '/api/setup/first-account' && init?.method === 'POST') return json({ token: 'test-token', user: { id: 'user', name: 'Ada' } });
		if (url === '/api/setup/progress' && init?.method === 'PUT') return json({}, progressFails ? 500 : 200);
		if (url.startsWith('/api/setup/classifier')) return classifier ? json(classifier) : json({ state: 'ready' });
		if (url.includes('/sources')) return json({ sources: sources() });
		if (url.includes('/corsia-agent')) return json({ target: { thread_id: 'corsia-thread' } });
		if (url.includes('/models')) return json({ models: [{ id: 'claude', name: 'Claude', backend: 'harness', litellm_model: 'claude-code/sonnet', status: 'ready', connection: 'claude-code', runs_on: [{ selected: true, status: 'ready' }] }] });
		return json({});
	}));
	host = document.createElement('div'); document.body.appendChild(host);
	onFinished = vi.fn();
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished } });
	flushSync(); await settle(); click('Continue'); await settle(); // past the Welcome screen of a narrow window
	fill('#setup-name', 'Ada'); fill('#setup-username', 'ada'); fill('#setup-password', 'password1'); fill('#setup-confirm-password', 'password1');
	await settle(); click('Continue'); await settle();
	await vi.waitFor(() => expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions'));
	click('Neither'); await settle();
	expect(host.querySelector('h1')?.textContent).toBe('Providers & models');
	await vi.waitFor(() => expect(Array.from(host.querySelectorAll('button')).find((candidate) => candidate.textContent?.trim() === 'Start using CORSIA')?.disabled).toBe(false));
}

describe('SetupScreen account recovery', () => {
	it('offers a working Save a report action when account creation fails', async () => {
		const invoke = vi.fn(async () => undefined);
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke });
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: false, work_folder: '~/CORSIA' });
			if (url === '/api/setup/first-account' && init?.method === 'POST') return json({}, 500);
			return json({});
		}));
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync(); await settle(); click('Continue'); await settle();
		fill('#setup-name', 'Ada'); fill('#setup-username', 'ada'); fill('#setup-password', 'password1'); fill('#setup-confirm-password', 'password1');
		await settle(); click('Continue'); await settle();
		expect(host.textContent).toContain('Couldn’t create your account. Try again.');
		click('Save a report'); await settle();
		// No provider row failed, so the report adds no row details that would claim nothing went wrong.
		expect(invoke).toHaveBeenCalledWith('save_setup_report', { details: '' });
	});
});

describe('SetupScreen local-start recovery', () => {
	it.each([
		['an unrecognized refusal without exposing its message', [json({ message: 'private route detail' }, 500)], 'CORSIA couldn’t start local setup.'],
		['an unrecognized refusal with a reference', [json({ message: 'private route detail', ref: 'local-start-123' }, 500)], 'CORSIA couldn’t start local setup.'],
		['an unreachable local backend', [new Error('network down')], 'CORSIA couldn’t start local setup.'],
	] as const)('shows the local row recovery for %s', async (_name, localReplies, expected) => {
		await openModels({ localEligible: true, localReplies: [...localReplies] });
		await vi.waitFor(() => expect(host.textContent).toContain(expected));
		expect(host.textContent).not.toContain('private route detail');
	});
});

const omlx = (fields: Record<string, unknown>) => ({ source: 'omlx', name: 'Local model', state: 'needs_setup', ...fields });
const localPosts = () => vi.mocked(fetch).mock.calls.filter(([url, init]) => ['/api/providers/local/setup', '/api/setup/local-download'].includes(String(url)) && init?.method === 'POST').length;

describe('SetupScreen Download press is never silent', () => {
	it('shows why and a Retry when the service accepts the press but starts nothing', async () => {
		await openModels({ localEligible: true, localReplies: [json({ ok: true })] });
		await vi.waitFor(() => expect(host.textContent).toContain('CORSIA couldn’t start the download. Press Retry.'));
		expect(Array.from(host.querySelectorAll('button')).some((candidate) => candidate.textContent?.trim() === 'Download')).toBe(false);
		expect(Array.from(host.querySelectorAll('button')).some((candidate) => candidate.textContent?.trim() === 'Retry')).toBe(true);
	});

	it('uses the service\'s own reason when it holds the press for another process', async () => {
		const fix = 'Another CORSIA window is still setting up the local model. Quit CORSIA completely, reopen it, then press Retry.';
		await openModels({ localEligible: true, localReplies: [json({ ok: false, reason: 'operation_held', fix, ref: 'local-0123456789abcdef' })] });
		await vi.waitFor(() => expect(host.textContent).toContain(fix));
		expect(host.textContent).toContain('local-0123456789abcdef');
	});

	it('moves the row to downloading when Retry is pressed and the service starts the flight', async () => {
		let flying = false;
		await openModels({ localEligible: true, sources: () => [omlx(flying ? { local_state: 'downloading', download_percent: 3 } : {})],
			localReplies: [json({ ok: true }), json({ ok: true, in_progress: true, stage: 'Installing' })] });
		await vi.waitFor(() => expect(host.textContent).toContain('CORSIA couldn’t start the download.'));
		flying = true;
		click('Retry'); await settle();
		await vi.waitFor(() => expect(host.textContent).toContain('Downloading 3%'));
		expect(host.textContent).not.toContain('CORSIA couldn’t start the download.');
		expect(localPosts()).toBe(2);
	});

	it('treats an already-connected local model as started, not as a failure', async () => {
		await openModels({ localEligible: true, sources: () => [omlx({ state: 'ready', local_state: 'ready' })], localReplies: [json({ ok: true, existing: true })] });
		await settle();
		expect(host.textContent).not.toContain('CORSIA couldn’t start');
	});
});

describe('SetupScreen classifier download stays unseen', () => {
	it.each([
		['downloading', { state: 'downloading', bytes_done: 60, bytes_total: 100 }],
		['failed', { state: 'failed', reason: 'Couldn’t download', retryable: true }],
	] as const)('shows no line for it and Start using CORSIA stays enabled while it is %s', async (_name, classifier) => {
		await openModels({ classifier });
		expect(host.textContent).not.toContain('Finishing setup');
		expect(host.textContent).not.toContain('Downloading');
		const start = Array.from(host.querySelectorAll('button')).find((candidate) => candidate.textContent?.trim() === 'Start using CORSIA');
		expect(start?.disabled).toBe(false);
	});

	it('retries a failed classifier download quietly when the window comes back to the front', async () => {
		await openModels({ classifier: { state: 'failed', reason: 'Couldn’t download', retryable: true } });
		const posts = () => vi.mocked(fetch).mock.calls.filter(([url, init]) => String(url) === '/api/setup/classifier' && init?.method === 'POST').length;
		const before = posts();
		window.dispatchEvent(new Event('focus')); await settle();
		await vi.waitFor(() => expect(posts()).toBe(before + 1));
	});
});

describe('SetupScreen completion', () => {
	it('shows the progress-save recovery when the completion write fails', async () => {
		await openModels({ progressFails: true });
		click('Start using CORSIA');
		await vi.waitFor(() => expect(host.textContent).toContain('CORSIA couldn’t save setup progress. Try again.'));
		expect(onFinished).not.toHaveBeenCalled();
	});

	it('finishes by recording setup complete and never chooses a model for the CORSIA agent', async () => {
		await openModels();
		click('Start using CORSIA');
		await vi.waitFor(() => expect(onFinished).toHaveBeenCalledOnce());

		const calls = vi.mocked(fetch).mock.calls.map(([url, init]) => ({ url: String(url), method: init?.method ?? 'GET', body: init?.body }));
		// The agent's model stays unset: the person picks it on the first chat.
		expect(calls.filter((call) => call.url.startsWith('/api/threads') && call.method !== 'GET')).toEqual([]);
		const progress = calls.filter((call) => call.url === '/api/setup/progress' && call.method === 'PUT').map((call) => JSON.parse(String(call.body)));
		expect(progress.at(-1)).toMatchObject({ completed: true });
	});
});
