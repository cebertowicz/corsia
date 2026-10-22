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

async function openModels({ localEligible = false, localReplies = [], progressFails = false }: { localEligible?: boolean; localReplies?: Array<Response | Error>; progressFails?: boolean } = {}) {
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
		if (url.includes('/sources')) return json({ sources: [] });
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
