import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import Page from './+page.svelte';
import { token, user, isAuthenticated, logout } from '$lib/stores/auth';
import { activeThreadId, messages, threads, type Thread } from '$lib/stores/threads';
import { uiScope } from '$lib/stores/uiScope';
import { _resetProjectsForTests } from '$lib/stores/projects';
import * as conductor from '$lib/utils/conductor';
import { pendingCorsiaMessage, requestCorsiaMessage } from '$lib/corsiaRequest';
import { activeModelId, models, type Model } from '$lib/stores/models';
import { disconnect } from '$lib/ws';
import { resetTurnState } from '$lib/ws/turnState';

// Only transport is replaced. The route, auth handlers, auth gate, ChatPanel,
// first-run helper, and both selection functions are the production modules.
class FakeSocket {
	static OPEN = 1;
	static instances: FakeSocket[] = [];
	readyState = 1;
	onopen: (() => void) | null = null;
	onmessage: ((event: { data: string }) => void) | null = null;
	onclose: (() => void) | null = null;
	onerror: (() => void) | null = null;
	sent: Record<string, unknown>[] = [];
	constructor(_url: string) { FakeSocket.instances.push(this); }
	send(value: string) { this.sent.push(JSON.parse(value)); }
	close() { this.readyState = 3; }
	authenticate() {
		this.onopen?.();
		this.onmessage?.({ data: JSON.stringify({
			type: 'auth_ok', user: { id: 'fixture-user', name: 'Fixture' }, active_turns: [],
		}) });
	}
}

function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>(done => { resolve = done; });
	return { promise, resolve };
}
const row = (id: string, projectId?: string): Thread => ({
	id, user_id: 'fixture-user', profile: 'personal', title: id, model: null,
	...(projectId ? { project_id: projectId } : {}),
	created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
});
let host: HTMLDivElement;
let page: ReturnType<typeof mount> | null;
let lookup: ReturnType<typeof deferred<Response>>;
let paths: string[];
let scopeLookups: Map<string, Promise<Response>>;
let firstRunPending: boolean;
let listedThreads: Thread[];
let catalogModels: Model[];
let deferredThreadList: ReturnType<typeof deferred<Response>> | null;
let deferredProjectList: ReturnType<typeof deferred<Response>> | null;
let landSpy: MockInstance<typeof conductor.landOnFirstRun>;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
	status, headers: { 'Content-Type': 'application/json' },
});

beforeEach(() => {
	conductor._resetFirstRunLandingForTests();
	conductor.corsiaAgentId.set(null);
	user.set(null);
	token.set('fixture-session');
	activeThreadId.set(null);
	messages.set([]);
	threads.set([]);
	uiScope.set(null);
	_resetProjectsForTests();
	activeModelId.set(null);
	models.set([]);
	firstRunPending = true;
	listedThreads = [];
	catalogModels = [];
	deferredThreadList = null;
	deferredProjectList = null;
	lookup = deferred<Response>();
	paths = [];
	scopeLookups = new Map();
	const currentPaths = paths;
	FakeSocket.instances = [];
	vi.stubGlobal('WebSocket', FakeSocket);
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = new URL(String(input), 'http://fixture.invalid');
		currentPaths.push(url.pathname);
		if (url.pathname === '/api/auth/logout' && init?.method === 'POST') return json({});
		if (init?.method && init.method !== 'GET') throw new Error(`Unexpected write: ${init.method} ${url.pathname}`);
		if (url.pathname === '/api/corsia-agent') return scopeLookups.get('workspace') ?? lookup.promise;
		if (url.pathname === '/api/conductor') return scopeLookups.get(url.searchParams.get('scope') ?? '') ?? lookup.promise;
		if (url.pathname === '/api/threads') return deferredThreadList?.promise ?? json({ threads: listedThreads });
		if (url.pathname === '/api/projects') return deferredProjectList?.promise ?? json({ projects: [] });
		if (url.pathname === '/api/models') return json({ models: catalogModels });
		if (url.pathname === '/api/settings' || url.pathname === '/api/system/drain') return json({});
		if (url.pathname.startsWith('/api/delegation/threads/')) return json({ thread_kind: 'conductor' });
		if (url.pathname.endsWith('/messages')) return json({ messages: [], has_more: false, next_cursor: null });
		// Ancillary reads cannot supply a selection or fabricate a conductor.
		return json({}, 404);
	}));
	landSpy = vi.spyOn(conductor, 'landOnFirstRun'); // call-through observation
	host = document.createElement('div');
	document.body.appendChild(host);
	page = null;
});

afterEach(async () => {
	if (page) await unmount(page);
	disconnect();
	host.remove();
	user.set(null);
	token.set(null);
	activeThreadId.set(null);
	threads.set([]);
	uiScope.set(null);
	_resetProjectsForTests();
	activeModelId.set(null);
	models.set([]);
	resetTurnState('fixture-conductor');
	pendingCorsiaMessage.set(null);
	conductor._resetFirstRunLandingForTests();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

function render() {
	page = mount(Page, { target: host });
	flushSync();
	expect(FakeSocket.instances).toHaveLength(1);
}
function resolveLookup(target: Record<string, unknown> = { thread_id: 'fixture-conductor' }) {
	lookup.resolve(json({ target, first_run_pending: firstRunPending }));
}
async function settle() {
	// Drain route tick/response promises and dynamic imports, without fake timers.
	for (let turn = 0; turn < 8; turn++) {
		await tick();
		await new Promise(done => setTimeout(done, 0));
		flushSync();
	}
}
function authenticate() { FakeSocket.instances[0].authenticate(); }
function historyReads(id: string) { return paths.filter(path => path === `/api/threads/${id}/messages`); }

describe('first-run entry across auth readiness', () => {
	it('waits for delayed auth even when conductor transport can resolve immediately', async () => {
		render();
		expect(get(isAuthenticated)).toBe(false);
		expect(host.querySelector('.auth-gate')).not.toBeNull();
		resolveLookup();
		await settle();
		expect(get(conductor.corsiaAgentId)).toBeNull();
		expect(paths.filter(path => path === '/api/corsia-agent')).toHaveLength(0);
		expect(landSpy).not.toHaveBeenCalled();
		const preAuthLandingAttempts = landSpy.mock.calls.length;
		const preAuthThreadListReads = paths.filter(path => path === '/api/threads').length;
		expect(get(activeThreadId)).toBeNull();
		expect(historyReads('fixture-conductor')).toHaveLength(0);
		authenticate();
		await settle();
		expect(get(isAuthenticated)).toBe(true);
		expect(host.querySelector('.chat-shell')).not.toBeNull();
		console.info('delayed-auth witness', JSON.stringify({
			authReady: get(isAuthenticated), chatMounted: !!host.querySelector('.chat-shell'),
			activeThread: get(activeThreadId), conductorHistoryReads: historyReads('fixture-conductor').length,
			landingAttempts: landSpy.mock.calls.length, preAuthLandingAttempts, preAuthThreadListReads,
		}));
		expect(get(activeThreadId)).toBe('fixture-conductor');
		expect(get(conductor.corsiaAgentId)).toBe('fixture-conductor');
		expect(historyReads('fixture-conductor')).toHaveLength(1);
	});

	it('selects through the real ChatPanel when auth precedes conductor lookup', async () => {
		render();
		authenticate();
		await settle();
		resolveLookup();
		await vi.waitFor(() => expect(get(activeThreadId)).toBe('fixture-conductor'));
		expect(historyReads('fixture-conductor')).toHaveLength(1);
	});

	it('opens CORSIA and sends one pending request through the composer', async () => {
		firstRunPending = false;
		render();
		authenticate();
		await settle();
		resolveLookup();
		await settle();
		scopeLookups.set('workspace', Promise.resolve(json({ target: { thread_id: 'fixture-conductor' }, first_run_pending: false })));
		requestCorsiaMessage('Connect a Google account for all my projects');
		await vi.waitFor(() => expect(get(pendingCorsiaMessage)).toBeNull());
		await vi.waitFor(() => expect(get(activeThreadId)).toBe('fixture-conductor'));
		expect(get(activeModelId)).toBeNull();
		expect(get(models)).toEqual([]);
		await vi.waitFor(() => expect(FakeSocket.instances[0].sent.filter((frame) => frame.type === 'message')).toHaveLength(1));
		const messageFrame = FakeSocket.instances[0].sent.find((frame) => frame.type === 'message');
		expect(messageFrame).toMatchObject({
			type: 'message', thread_id: 'fixture-conductor', content: 'Connect a Google account for all my projects',
		});
		FakeSocket.instances[0].onmessage?.({ data: JSON.stringify({
			type: 'input_accepted', thread_id: 'fixture-conductor', input_id: messageFrame?.input_id,
			message_id: 'fixture-message', created_at: '2026-10-01T00:00:01Z', turn_id: 'fixture-turn', turn_order: 1,
		}) });
		await vi.waitFor(() => expect(historyReads('fixture-conductor')).toHaveLength(1));
		await settle();
		expect(FakeSocket.instances[0].sent.filter((frame) => frame.type === 'message')).toHaveLength(1);
		expect(get(pendingCorsiaMessage)).toBeNull();
	});

	it('preserves a previously opened thread', async () => {
		listedThreads = [row('fixture-open')];
		threads.set(listedThreads);
		activeThreadId.set('fixture-open');
		render();
		authenticate();
		resolveLookup();
		await settle();
		expect(get(activeThreadId)).toBe('fixture-open');
		expect(historyReads('fixture-conductor')).toHaveLength(0);
	});

	it('does not auto-enter from project scope', async () => {
		uiScope.set({ id: 'fixture-project', name: 'Fixture project', color: '#456789' });
		render();
		authenticate();
		resolveLookup();
		await settle();
		expect(get(activeThreadId)).toBeNull();
		expect(historyReads('fixture-conductor')).toHaveLength(0);
	});

	it('does not enter when first-run pending is false', async () => {
		firstRunPending = false;
		render();
		authenticate();
		resolveLookup();
		await settle();
		expect(get(activeThreadId)).toBeNull();
		expect(historyReads('fixture-conductor')).toHaveLength(0);
	});

	it('does not steal a user selection made while lookup is in flight', async () => {
		listedThreads = [row('fixture-choice')];
		render();
		authenticate();
		await settle();
		const choice = Array.from(host.querySelectorAll<HTMLElement>('.thread-item')).find(item => item.textContent?.includes('fixture-choice'));
		expect(choice, 'real sidebar conversation row').toBeDefined();
		choice!.click();
		await vi.waitFor(() => expect(get(activeThreadId)).toBe('fixture-choice'));
		resolveLookup();
		await settle();
		expect(get(activeThreadId)).toBe('fixture-choice');
		expect(historyReads('fixture-choice')).toHaveLength(1);
		expect(historyReads('fixture-conductor')).toHaveLength(0);
	});

	it('keeps the phone drawer open when the landing arrives after the user opened it', async () => {
		const width = window.innerWidth;
		window.innerWidth = 393;
		try {
			render();
			authenticate();
			await settle();
			const sidebar = host.querySelector('aside.sidebar');
			expect(sidebar, 'real phone sidebar').not.toBeNull();
			host.querySelector<HTMLButtonElement>('button[aria-label="Toggle sidebar"]')!.click();
			await settle();
			expect(sidebar!.classList.contains('open'), 'the user opened the drawer').toBe(true);
			resolveLookup();
			await vi.waitFor(() => expect(get(activeThreadId)).toBe('fixture-conductor'));
			await settle();
			expect(sidebar!.classList.contains('open'), 'the landing left the drawer open').toBe(true);
		} finally {
			window.innerWidth = width;
		}
	});

	it('does not steal a user selection after landing waits for a hidden conductor list read', async () => {
		listedThreads = [row('fixture-choice')];
		render();
		authenticate();
		await settle();
		const threadListReadsBeforeLanding = paths.filter(path => path === '/api/threads').length;
		deferredThreadList = deferred<Response>();
		resolveLookup();
		await vi.waitFor(() => expect(paths.filter(path => path === '/api/threads').length)
			.toBeGreaterThan(threadListReadsBeforeLanding));
		threads.set(listedThreads);
		flushSync();
		await tick();
		const choice = Array.from(host.querySelectorAll<HTMLElement>('.thread-item')).find(item => item.textContent?.includes('fixture-choice'));
		expect(choice, 'real sidebar conversation row').toBeDefined();
		choice!.click();
		await vi.waitFor(() => expect(get(activeThreadId)).toBe('fixture-choice'));
		deferredThreadList.resolve(json({ threads: listedThreads }));
		await settle();
		expect(get(activeThreadId)).toBe('fixture-choice');
		expect(historyReads('fixture-choice')).toHaveLength(1);
		expect(historyReads('fixture-conductor')).toHaveLength(0);
	});

	it('keeps New Chat after a pending landing starts its hidden thread-list read', async () => {
		render();
		authenticate();
		await settle();
		const threadListReadsBeforeLanding = paths.filter(path => path === '/api/threads').length;
		deferredThreadList = deferred<Response>();
		resolveLookup();
		await vi.waitFor(() => expect(paths.filter(path => path === '/api/threads').length)
			.toBeGreaterThan(threadListReadsBeforeLanding));
		const newChat = host.querySelector<HTMLButtonElement>('button[aria-label="New chat"]');
		expect(newChat, 'real sidebar New Chat control').not.toBeNull();
		newChat!.click();
		expect(get(activeThreadId)).toBeNull();
		deferredThreadList.resolve(json({ threads: [] }));
		await settle();
		expect(get(activeThreadId)).toBeNull();
		expect(historyReads('fixture-conductor')).toHaveLength(0);
	});

	it('keeps New Chat after a conversation choice before the conductor lookup resolves', async () => {
		listedThreads = [row('fixture-choice')];
		render();
		authenticate();
		await settle();
		const choice = Array.from(host.querySelectorAll<HTMLElement>('.thread-item')).find(item => item.textContent?.includes('fixture-choice'));
		expect(choice, 'real sidebar conversation row').toBeDefined();
		choice!.click();
		await vi.waitFor(() => expect(get(activeThreadId)).toBe('fixture-choice'));
		const newChat = host.querySelector<HTMLButtonElement>('button[aria-label="New chat"]');
		expect(newChat, 'real sidebar New Chat control').not.toBeNull();
		newChat!.click();
		expect(get(activeThreadId)).toBeNull();
		resolveLookup();
		await settle();
		expect(get(activeThreadId)).toBeNull();
		expect(historyReads('fixture-conductor')).toHaveLength(0);
	});

	it('does not let a delayed project catalog move scope after a workspace choice', async () => {
		listedThreads = [row('fixture-choice'), row('fixture-conductor', 'fixture-project')];
		deferredProjectList = deferred<Response>();
		render();
		authenticate();
		await settle();
		resolveLookup({ thread_id: 'fixture-conductor', project_id: 'fixture-project' });
		await vi.waitFor(() => expect(landSpy).toHaveBeenCalledTimes(1));
		await tick();
		const choice = Array.from(host.querySelectorAll<HTMLElement>('.thread-item')).find(item => item.textContent?.includes('fixture-choice'));
		expect(choice, 'real workspace sidebar conversation row').toBeDefined();
		choice!.click();
		await vi.waitFor(() => expect(get(activeThreadId)).toBe('fixture-choice'));
		deferredProjectList.resolve(json({ projects: [{
			id: 'fixture-project', name: 'Fixture project', status: 'active', accent_color: '#456789',
		}] }));
		await settle();
		expect(get(uiScope)).toBeNull();
		expect(get(activeThreadId)).toBe('fixture-choice');
		expect(historyReads('fixture-choice')).toHaveLength(1);
		expect(historyReads('fixture-conductor')).toHaveLength(0);
	});

	it('ignores a canceled lookup after the route unmounts', async () => {
		render();
		authenticate();
		await settle();
		expect(paths).toContain('/api/corsia-agent');
		await unmount(page!);
		page = null;
		resolveLookup();
		await settle();
		expect(landSpy).not.toHaveBeenCalled();
		expect(get(activeThreadId)).toBeNull();
		expect(historyReads('fixture-conductor')).toHaveLength(0);
	});

	it('ignores the stale scope response after switching to a project', async () => {
		const projectLookup = deferred<Response>();
		scopeLookups.set('fixture-project', projectLookup.promise);
		render();
		authenticate();
		await settle();
		uiScope.set({ id: 'fixture-project', name: 'Fixture project', color: '#456789' });
		await settle();
		resolveLookup();
		await settle();
		expect(landSpy).not.toHaveBeenCalled();
		expect(get(activeThreadId)).toBeNull();
		projectLookup.resolve(json({ target: { thread_id: 'fixture-project-conductor' }, first_run_pending: false }));
		await settle();
		expect(landSpy).toHaveBeenCalledTimes(1);
		expect(landSpy.mock.calls[0][1]?.id).toBe('fixture-project');
		expect(get(uiScope)?.id).toBe('fixture-project');
		expect(get(activeThreadId)).toBeNull();
		expect(historyReads('fixture-conductor')).toHaveLength(0);
		expect(historyReads('fixture-project-conductor')).toHaveLength(0);
	});

	it('does not land from a late response after real logout clears auth', async () => {
		render();
		authenticate();
		await settle();
		await logout();
		await settle();
		const conductorReadsAtLogout = paths.filter(path => path === '/api/corsia-agent').length;
		resolveLookup();
		await settle();
		expect(get(isAuthenticated)).toBe(false);
		expect(host.querySelector('.auth-gate')).not.toBeNull();
		expect(landSpy).not.toHaveBeenCalled();
		expect(get(activeThreadId)).toBeNull();
		expect(paths.filter(path => path === '/api/corsia-agent')).toHaveLength(conductorReadsAtLogout);
		expect(historyReads('fixture-conductor')).toHaveLength(0);
	});

	it('selects on an already-authenticated page without waiting for another auth frame', async () => {
		user.set({ id: 'fixture-user', name: 'Fixture' });
		render();
		resolveLookup();
		await vi.waitFor(() => expect(get(activeThreadId)).toBe('fixture-conductor'));
		expect(get(isAuthenticated)).toBe(true);
		expect(historyReads('fixture-conductor')).toHaveLength(1);
	});
});
