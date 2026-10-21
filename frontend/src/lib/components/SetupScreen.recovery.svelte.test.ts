import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

const bindSetupFirstModel = vi.hoisted(() => vi.fn());
vi.mock('$lib/setupFirstModel', async (importOriginal) => ({
	...await importOriginal<typeof import('$lib/setupFirstModel')>(),
	bindSetupFirstModel,
}));

import SetupScreen from './SetupScreen.svelte';
import { SetupFirstModelError } from '$lib/setupFirstModel';
import { token, user } from '$lib/stores/auth';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;

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

async function openModels({ localEligible = false, otherOmlx = 'none', localReplies = [], progressFails = false }: { localEligible?: boolean; otherOmlx?: 'none' | 'stopped_theirs' | 'kept_both'; localReplies?: Array<Response | Error>; progressFails?: boolean } = {}) {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: localEligible, other_omlx: otherOmlx, work_folder: '~/CORSIA' });
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
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
	flushSync(); await settle(); click('Continue'); await settle(); // past the Welcome screen of a narrow window
	fill('#setup-name', 'Ada'); fill('#setup-username', 'ada'); fill('#setup-password', 'password1'); fill('#setup-confirm-password', 'password1');
	await settle(); click('Continue'); await settle(); click('Continue'); await settle();
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
		click('Continue'); await settle();
		expect(host.textContent).toContain('Couldn’t create your account. Try again.');
		click('Save a report'); await settle();
		expect(invoke).toHaveBeenCalledWith('save_setup_report');
	});
});

describe('SetupScreen local-start recovery', () => {
	it.each([
		['a changed foreign oMLX', [json({ code: 'foreign_omlx_changed' }, 409)], 'CORSIA couldn’t start local setup. Press Retry, or save a report.'],
		['an unrecognized refusal without exposing its message', [json({ message: 'private route detail' }, 500)], 'CORSIA couldn’t start local setup. Press Retry, or save a report.'],
		['an unrecognized refusal with a reference', [json({ message: 'private route detail', ref: 'local-start-123' }, 500)], 'CORSIA couldn’t start local setup. Press Retry, or save a report.'],
		['an unreachable local backend', [new Error('network down')], 'CORSIA couldn’t start local setup. Press Retry, or save a report.'],
	] as const)('shows the local row recovery for %s', async (_name, localReplies, expected) => {
		await openModels({ localEligible: true, localReplies: [...localReplies] });
		await vi.waitFor(() => expect(host.textContent).toContain(expected));
		expect(host.textContent).not.toContain('private route detail');
	});

	it('always shows the other-oMLX question when the server requires a fresh choice', async () => {
		await openModels({ localEligible: true, otherOmlx: 'stopped_theirs', localReplies: [json({ code: 'consent_required' }, 409)] });
		await vi.waitFor(() => expect(host.textContent).toContain('Another copy of oMLX is already running'));
	});

	it('shows generic recovery when the person’s other-oMLX choice cannot start setup', async () => {
		await openModels({ localEligible: true, localReplies: [json({ code: 'consent_required' }, 409), json({ message: 'private route detail' }, 500)] });
		click('Keep it running');
		await vi.waitFor(() => expect(host.textContent).toContain('CORSIA couldn’t start local setup. Press Retry, or save a report.'));
		expect(host.textContent).not.toContain('private route detail');
	});
});

describe('SetupScreen completion recovery', () => {
	it('uses the progress-save code when the actual completion write fails', async () => {
		bindSetupFirstModel.mockReset();
		bindSetupFirstModel.mockResolvedValueOnce({ model: 'claude-code/sonnet', source: 'claude-code' });
		await openModels({ progressFails: true });
		click('Start using CORSIA');
		await vi.waitFor(() => expect(host.textContent).toContain('CORSIA couldn’t save setup progress. Try again.'));
	});

	it.each([
		['no model', 'no_model', 'CORSIA couldn’t find a working model. Connect a provider or choose one that is ready, then try again.'],
		['incomplete model binding', 'binding_incomplete', 'CORSIA couldn’t save the selected model. Try again.'],
		['catalog refusal', 'catalog_refused', 'CORSIA couldn’t save the selected model. Check the provider setup, then try again.'],
		['catalog unavailable', 'catalog_unavailable', 'CORSIA couldn’t check your available models. Try again.'],
		['agent unavailable', 'agent_unavailable', 'CORSIA couldn’t reach its agent. Try again.'],
		['progress save', 'progress_save', 'CORSIA couldn’t save setup progress. Try again.'],
	] as const)('shows named recovery copy for %s', async (_name, code, expected) => {
		bindSetupFirstModel.mockReset();
		bindSetupFirstModel.mockRejectedValueOnce(new SetupFirstModelError(code));
		await openModels();
		click('Start using CORSIA');
		await vi.waitFor(() => expect(bindSetupFirstModel).toHaveBeenCalledOnce());
		await vi.waitFor(() => expect(host.textContent).toContain(expected));
		expect(host.textContent).not.toContain(code);
	});
});
