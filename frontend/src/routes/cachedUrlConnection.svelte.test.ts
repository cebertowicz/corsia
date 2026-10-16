import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { setImmediate } from 'node:timers';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import Page from './+page.svelte';
import { token, user } from '$lib/stores/auth';
import { activeThreadId, messages, threads } from '$lib/stores/threads';
import { uiScope } from '$lib/stores/uiScope';
import { disconnect, on } from '$lib/ws';
import * as conductor from '$lib/utils/conductor';

// Production route/HomeLifecycle/LoginScreen/connection/auth handlers/ChatPanel.
// No helper change, browser, provider, network, chat intent, or count reset.
const fixtureSession = 'fixture-session';
const conductorId = 'fixture-conductor';
class FakeSocket {
	static CONNECTING = 0;
	static OPEN = 1;
	static CLOSING = 2;
	static CLOSED = 3;
	static instances: FakeSocket[] = [];
	readyState = FakeSocket.CONNECTING;
	onopen: (() => void) | null = null;
	onmessage: ((event: { data: string }) => void) | null = null;
	onclose: (() => void) | null = null;
	onerror: (() => void) | null = null;
	sentTypes: string[] = [];
	constructor(_url: string) { FakeSocket.instances.push(this); }
	send(raw: string) { this.sentTypes.push(JSON.parse(raw).type); }
	close() { this.readyState = FakeSocket.CLOSED; }
	authenticate() {
		this.readyState = FakeSocket.OPEN;
		this.onopen?.();
		this.onmessage?.({ data: JSON.stringify({
			type: 'auth_ok', user: { id: 'fixture-user', name: 'Fixture' }, active_turns: [],
		}) });
	}
}
let host: HTMLDivElement;
let page: ReturnType<typeof mount> | null;
let connected: number;
let unsubscribe: () => void;
let historyOwners: string[];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
	status, headers: { 'Content-Type': 'application/json' },
});
async function settle() {
	// Drain Svelte, response promises and actual dynamic module imports while
	// the fake clock stays fixed at the selected boundary.
	for (let turn = 0; turn < 20; turn++) {
		await tick();
		await new Promise<void>(done => setImmediate(done));
		flushSync();
	}
}
beforeEach(() => {
	vi.useFakeTimers();
	conductor._resetFirstRunLandingForTests();
	conductor.corsiaAgentId.set(null);
	user.set(null);
	token.set(null);
	activeThreadId.set(null);
	messages.set([]);
	threads.set([]);
	uiScope.set(null);
	window.history.replaceState({}, '', `/?${new URLSearchParams({ token: fixtureSession })}`);
	FakeSocket.instances = [];
	connected = 0;
	historyOwners = [];
	vi.stubGlobal('WebSocket', FakeSocket);
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = new URL(String(input), 'http://fixture.invalid');
		if (init?.method && init.method !== 'GET') throw new Error(`Unexpected write: ${init.method} ${url.pathname}`);
		if (url.pathname === '/api/auth/status') return json({ has_users: true });
		if (url.pathname === '/api/corsia-agent') return json({ target: { thread_id: conductorId, model: 'fixture-model' }, first_run_pending: true });
		if (url.pathname === '/api/threads') return json({ threads: [] });
		if (url.pathname === '/api/models') return json({ models: [] });
		if (url.pathname === '/api/settings') return json({ features: { focus_mode: true } });
		if (url.pathname.startsWith('/api/delegation/threads/')) return json({ thread_kind: 'conductor' });
		if (url.pathname.endsWith('/messages')) {
			const stack = new Error().stack ?? '';
			historyOwners.push(stack.includes('fetchThreadMessages') ? 'refresh'
				: stack.includes('selectThread') ? 'selection' : 'unclassified');
			return json({ messages: [], has_more: false });
		}
		// Unsupported ancillary reads cannot fabricate successful payloads.
		return json({}, 404);
	}));
	unsubscribe = on('connected', () => { connected++; });
	host = document.createElement('div');
	document.body.appendChild(host);
	page = null;
});
afterEach(async () => {
	if (page) await unmount(page);
	unsubscribe();
	disconnect();
	for (const socket of FakeSocket.instances) socket.close();
	vi.clearAllTimers();
	vi.useRealTimers();
	host.remove();
	user.set(null);
	token.set(null);
	activeThreadId.set(null);
	messages.set([]);
	threads.set([]);
	uiScope.set(null);
	conductor._resetFirstRunLandingForTests();
	window.history.replaceState({}, '', '/');
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});
function render() {
	page = mount(Page, { target: host });
	flushSync();
}
function witness(label: string) {
	console.info('connection-red witness', JSON.stringify({
		label, sockets: FakeSocket.instances.length,
		liveSockets: FakeSocket.instances.filter(socket => socket.readyState !== FakeSocket.CLOSED).length,
		authSends: FakeSocket.instances.flatMap(socket => socket.sentTypes).filter(type => type === 'auth').length,
		connected, activeThread: get(activeThreadId), historyOwners,
	}));
}
describe('cached session with URL sign-in connection ownership', () => {
	it('keeps one live socket and one first page when URL sign-in repeats the retained session', async () => {
		token.set(fixtureSession); // same persisted session retained by the canceled visit
		render();
		expect(FakeSocket.instances).toHaveLength(1);
		FakeSocket.instances[0].authenticate();
		await settle();
		expect(get(activeThreadId)).toBe(conductorId);
		expect(historyOwners).toEqual(['selection']);
		await vi.advanceTimersByTimeAsync(100); // the former URL sign-in delay
		const second = FakeSocket.instances[1];
		if (second) second.authenticate();
		await settle();
		witness('retained-session plus URL-session');
		expect(FakeSocket.instances.filter(socket => socket.readyState !== FakeSocket.CLOSED)).toHaveLength(1);
		expect(connected).toBe(1);
		expect(historyOwners).toEqual(['selection']);
	});
	it('uses one socket and one first page without a retained session', async () => {
		render();
		// URL sign-in is consumed on mount, with no delayed second attempt.
		expect(FakeSocket.instances).toHaveLength(1);
		await vi.advanceTimersByTimeAsync(100);
		expect(FakeSocket.instances).toHaveLength(1);
		FakeSocket.instances[0].authenticate();
		await settle();
		witness('URL-session only');
		expect(connected).toBe(1);
		expect(historyOwners).toEqual(['selection']);
		expect(get(activeThreadId)).toBe(conductorId);
	});
});
