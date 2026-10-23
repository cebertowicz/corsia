import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';

// The mark's exit is held open by the test, so the order of exit and hand-over is observable.
const exitGate = vi.hoisted(() => ({ release: () => {}, started: false }));
vi.mock('$lib/livingLogo.js', () => ({
	createLivingLogo: () => ({
		exit: () => { exitGate.started = true; return new Promise<void>((done) => { exitGate.release = done; }); },
		destroy() {},
	}),
}));
vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;

async function settle() { await tick(); await new Promise((done) => setTimeout(done)); await tick(); }
const finishButton = () => Array.from(host.querySelectorAll('button')).find((candidate) => candidate.textContent?.trim() === 'Start using CORSIA');

afterEach(async () => {
	if (component) await unmount(component);
	component = null; host?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); token.set(null); user.set(null);
});

async function openModels() {
	token.set('setup-token'); user.set({ id: 'user', name: 'Ada' });
	exitGate.started = false;
	vi.stubGlobal('matchMedia', (query: string) => ({ matches: query === '(min-width: 80em)', media: query, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		if (url === '/api/setup/progress') return init?.method === 'PUT' ? json({}) : json({ setup_completed: false, status: { apple_silicon: true, macos_supported: true, local_eligible: false, work_folder: '~/CORSIA' } });
		if (url.includes('/sources')) return json({ sources: [] });
		if (url.includes('/corsia-agent')) return json({ target: { thread_id: 'corsia-thread' } });
		if (url.includes('/models')) return json({ models: [{ id: 'claude', name: 'Claude', backend: 'harness', litellm_model: 'claude-code/sonnet', status: 'ready', connection: 'claude-code', runs_on: [{ selected: true, status: 'ready' }] }] });
		return json({});
	}));
	host = document.createElement('div'); document.body.appendChild(host);
	const onFinished = vi.fn();
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished, resume: true } });
	flushSync();
	await vi.waitFor(() => expect(finishButton()?.disabled).toBe(false));
	return onFinished;
}

describe('SetupScreen hand-over to the first chat', () => {
	it('plays the mark’s exit first and calls onFinished only once it has finished', async () => {
		const onFinished = await openModels();
		finishButton()?.click();
		await vi.waitFor(() => expect(exitGate.started).toBe(true));
		await settle();
		expect(onFinished).not.toHaveBeenCalled();
		exitGate.release();
		await vi.waitFor(() => expect(onFinished).toHaveBeenCalledOnce());
	});

	it('does not play the exit when setup progress could not be saved', async () => {
		const onFinished = await openModels();
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/setup/progress' && init?.method === 'PUT' ? json({}, 500) : json({})));
		finishButton()?.click(); await settle();
		expect(exitGate.started).toBe(false);
		expect(onFinished).not.toHaveBeenCalled();
	});
});
