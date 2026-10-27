import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const calls = vi.hoisted(() => ({ restore: 0, recover: 0 }));
vi.mock('./turnState', async (importOriginal) => {
	const actual = await importOriginal<typeof import('./turnState')>();
	return {
		...actual,
		restoreAuthTurns: (...args: Parameters<typeof actual.restoreAuthTurns>) => {
			calls.restore += 1;
			return actual.restoreAuthTurns(...args);
		},
	};
});
vi.mock('./pendingRecovery', async (importOriginal) => {
	const actual = await importOriginal<typeof import('./pendingRecovery')>();
	return {
		...actual,
		recoverPendingInputs: (...args: Parameters<typeof actual.recoverPendingInputs>) => {
			calls.recover += 1;
			return actual.recoverPendingInputs(...args);
		},
	};
});

import { connect, disconnect, isConnected, reconnectNow } from './connection';
import { on } from './events';
import { sendMessage, stopRequestedTurns, stopTurn } from './send';
import { subscribedThreads } from './state';
import { _resetTurnStateForTests, getTurnState } from './turnState';
import { logout, token, user } from '../stores/auth';
import { connectionError, connectionState, drainStatus } from '../stores/connection';
import { activeThreadId, messages } from '../stores/threads';

type Frame = Record<string, unknown>;

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
	sent: Frame[] = [];
	closeCalls = 0;
	constructor(_url: string) { FakeSocket.instances.push(this); }
	send(raw: string) { this.sent.push(JSON.parse(raw)); }
	close() { this.closeCalls += 1; this.readyState = FakeSocket.CLOSED; }
	open() { this.readyState = FakeSocket.OPEN; this.onopen?.(); }
	receive(frame: Frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
	authOk(userId: string, activeTurns: Frame[] = []) {
		this.receive({ type: 'auth_ok', user: { id: userId, name: userId }, active_turns: activeTurns });
	}
	nativeClose() { this.readyState = FakeSocket.CLOSED; this.onclose?.(); }
	nativeError() { this.onerror?.(); }
	types() { return this.sent.map(frame => frame.type); }
}

const runningTurn = { thread_id: 'main-1', turn_id: 'turn-1', turn_order: 1 };
const sockets = () => FakeSocket.instances;
const live = () => sockets().filter(socket => socket.readyState !== FakeSocket.CLOSED);
let connected = 0;
let stopListening: () => void;
let historyPage: (threadId: string) => Promise<unknown>;
let drainBody: () => Promise<unknown>;

async function flush() {
	for (let turn = 0; turn < 20; turn++) await Promise.resolve();
}

function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>(done => { resolve = done; });
	return { promise, resolve };
}

function signIn(session: string) {
	// LoginScreen's sign-in: set the explicit session, then connect.
	token.set(session);
	connect();
}

function authenticated(session: string, userId: string, activeTurns: Frame[] = []) {
	signIn(session);
	const socket = sockets().at(-1)!;
	socket.open();
	socket.authOk(userId, activeTurns);
	return socket;
}

beforeEach(() => {
	vi.useFakeTimers();
	vi.stubGlobal('WebSocket', FakeSocket);
	historyPage = async () => ({ messages: [], has_more: false });
	drainBody = async () => ({ active: false });
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = new URL(String(input), 'http://fixture.invalid');
		if (url.pathname === '/api/system/drain') {
			const body = await drainBody();
			return { ok: true, status: 200, json: async () => body };
		}
		if (url.pathname === '/api/auth/logout') return { ok: true, status: 200, json: async () => ({}) };
		const history = url.pathname.match(/^\/api\/threads\/([^/]+)\/messages$/);
		if (history) {
			const body = await historyPage(history[1]);
			return { ok: true, status: 200, json: async () => body };
		}
		return { ok: false, status: 404, json: async () => ({}) };
	}));
	// Start every case from a reset backoff with no owner.
	token.set('reset-session');
	reconnectNow();
	disconnect();
	token.set(null);
	user.set(null);
	FakeSocket.instances = [];
	calls.restore = 0;
	calls.recover = 0;
	connected = 0;
	stopListening = on('connected', () => { connected += 1; });
	_resetTurnStateForTests();
	subscribedThreads.clear();
	stopRequestedTurns.set(new Map());
	activeThreadId.set('main-1');
	messages.set([]);
	drainStatus.set(null);
	connectionState.set('disconnected');
	connectionError.set(null);
});

afterEach(() => {
	stopListening();
	disconnect();
	token.set(null);
	user.set(null);
	activeThreadId.set(null);
	messages.set([]);
	vi.clearAllTimers();
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe('connection ownership lifecycle', () => {
	it('reuses the same-session owner while connecting and open', () => {
		signIn('session-a');
		connect();
		expect(sockets()).toHaveLength(1);
		sockets()[0].open();
		connect();
		signIn('session-a');
		expect(sockets()).toHaveLength(1);
		expect(sockets()[0].types()).toEqual(['auth']);
	});

	it('explicit reconnect replaces an active owner once and cancels a pending retry', () => {
		const a = authenticated('session-a', 'user-a');
		reconnectNow();
		expect(sockets()).toHaveLength(2);
		expect(a.closeCalls).toBe(1);
		a.nativeClose();
		vi.advanceTimersByTime(60_000);
		expect(sockets()).toHaveLength(2);

		const b = sockets()[1];
		b.open();
		b.authOk('user-a');
		b.nativeClose();
		reconnectNow();
		expect(sockets()).toHaveLength(3);
		vi.advanceTimersByTime(60_000);
		expect(sockets()).toHaveLength(3);
	});

	it('schedules exactly one retry for an unexpected error and close under the existing backoff', () => {
		const a = authenticated('session-a', 'user-a');
		a.nativeError();
		expect(a.closeCalls).toBe(1);
		expect(get(connectionState)).toBe('error');
		a.nativeClose();
		expect(get(connectionError)).toBe('Reconnecting in 2s...');
		vi.advanceTimersByTime(1_499);
		expect(sockets()).toHaveLength(1);
		vi.advanceTimersByTime(1);
		expect(sockets()).toHaveLength(2);
		vi.advanceTimersByTime(60_000);
		expect(sockets()).toHaveLength(2);
	});

	it('starts the backoff over once a reconnect authenticates', () => {
		let socket = authenticated('session-a', 'user-a');
		// Ten drops, each recovered, in one long-lived window.
		for (let drop = 0; drop < 10; drop++) {
			socket.nativeClose();
			vi.advanceTimersByTime(30_000);
			socket = sockets().at(-1)!;
			socket.open();
			socket.authOk('user-a');
		}
		const last = socket;
		last.nativeClose();
		expect(get(connectionError)).toBe('Reconnecting in 2s...');
		vi.advanceTimersByTime(1_500);
		expect(sockets().at(-1)).not.toBe(last);
	});

	it('keeps growing the backoff while the server never authenticates a reconnect', () => {
		const first = authenticated('session-a', 'user-a');
		first.nativeClose();
		const delays: string[] = [get(connectionError)!];
		for (let attempt = 0; attempt < 3; attempt++) {
			vi.advanceTimersByTime(30_000);
			sockets().at(-1)!.nativeClose();
			delays.push(get(connectionError)!);
		}
		expect(delays).toEqual([
			'Reconnecting in 2s...', 'Reconnecting in 2s...', 'Reconnecting in 3s...', 'Reconnecting in 5s...',
		]);
	});

	it.each(['explicit connect', 'new session', 'disconnect'] as const)(
		'leaves a pending retry inert after %s',
		(intervention) => {
			const a = authenticated('session-a', 'user-a');
			a.nativeClose();
			if (intervention === 'explicit connect') connect();
			if (intervention === 'new session') token.set('session-b');
			if (intervention === 'disconnect') disconnect();
			const afterIntervention = sockets().length;
			vi.advanceTimersByTime(60_000);
			expect(sockets()).toHaveLength(afterIntervention);
			expect(afterIntervention).toBe(intervention === 'explicit connect' ? 2 : 1);
		},
	);

	it('retired sockets cannot clear the current owner or move its heartbeat', () => {
		const a = authenticated('session-a', 'user-a');
		const b = authenticated('session-b', 'user-b');
		expect(a.closeCalls).toBe(1);
		vi.advanceTimersByTime(30_000);
		a.receive({ type: 'ping' });
		a.nativeError();
		a.nativeClose();
		expect(isConnected()).toBe(true);
		expect(get(connectionState)).toBe('connected');
		vi.advanceTimersByTime(14_999);
		expect(b.closeCalls).toBe(0);
		vi.advanceTimersByTime(1);
		expect(b.closeCalls).toBe(1);
		expect(a.closeCalls).toBe(1);
	});

	it('deliberate disconnect leaves a later native close nothing to reconnect', () => {
		const a = authenticated('session-a', 'user-a');
		disconnect();
		a.nativeClose();
		vi.advanceTimersByTime(60_000);
		expect(sockets()).toHaveLength(1);
		expect(get(user)?.id).toBe('user-a');
	});

	it('logout revokes first, then retires the owner so stale auth cannot restore the user', async () => {
		const a = authenticated('session-a', 'user-a');
		await logout();
		expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input) === '/api/auth/logout')).toBe(true);
		expect(a.closeCalls).toBe(1);
		a.authOk('user-a');
		a.nativeClose();
		vi.advanceTimersByTime(60_000);
		expect(get(user)).toBeNull();
		expect(sockets()).toHaveLength(1);
	});

	it('an invalid-session error retires the owner and schedules no retry', () => {
		const a = authenticated('session-a', 'user-a');
		a.receive({ type: 'error', message: 'Invalid or expired session' });
		expect(get(token)).toBeNull();
		expect(get(user)).toBeNull();
		expect(get(connectionError)).toBe('Invalid or expired session');
		a.nativeClose();
		vi.advanceTimersByTime(60_000);
		expect(sockets()).toHaveLength(1);
	});
});

describe('URL identity precedence (Governance controls)', () => {
	it.each(['connecting', 'open', 'authenticated'] as const)(
		'G-a: a different URL session wins over a cached owner that is %s',
		(stage) => {
			signIn('session-cached');
			const a = sockets()[0];
			if (stage !== 'connecting') a.open();
			if (stage === 'authenticated') a.authOk('user-cached');
			const sentBeforeRetirement = a.sent.length;

			signIn('session-url');
			expect(a.closeCalls).toBe(1);
			const b = sockets()[1];
			b.open();
			expect(b.sent).toEqual([{ type: 'auth', token: 'session-url' }]);

			a.open();
			a.authOk('user-cached');
			a.nativeError();
			a.nativeClose();
			b.authOk('user-url');
			a.authOk('user-cached');
			// Past any retry, short of B's own 45 s heartbeat.
			vi.advanceTimersByTime(40_000);

			expect(a.sent).toHaveLength(sentBeforeRetirement);
			expect(get(user)?.id).toBe('user-url');
			expect(live()).toEqual([b]);
			expect(sockets()).toHaveLength(2);
		},
	);

	it('G-b: logout after synchronous URL sign-in leaves no socket or user, even on stale auth', async () => {
		signIn('session-url');
		const a = sockets()[0];
		await logout();
		a.open();
		a.authOk('user-url');
		a.nativeClose();
		vi.advanceTimersByTime(60_000);
		expect(a.sent).toEqual([]);
		expect(get(user)).toBeNull();
		expect(live()).toEqual([]);
		expect(sockets()).toHaveLength(1);
	});

	it('G-c: an invalid URL session never falls back to the valid cached one', () => {
		const a = authenticated('session-cached', 'user-cached');
		signIn('session-url');
		const b = sockets()[1];
		b.open();
		b.receive({ type: 'error', message: 'Invalid or expired session' });
		a.authOk('user-cached');
		b.nativeClose();
		vi.advanceTimersByTime(60_000);
		expect(get(token)).toBeNull();
		expect(get(user)).toBeNull();
		expect(get(connectionError)).toBe('Invalid or expired session');
		expect(sockets()).toHaveLength(2);
		expect(live()).toEqual([]);
	});

	it('G-d: a same-session URL sign-in keeps one socket, auth and connected', () => {
		const a = authenticated('session-a', 'user-a');
		signIn('session-a');
		expect(sockets()).toEqual([a]);
		expect(a.types().filter(type => type === 'auth')).toHaveLength(1);
		expect(connected).toBe(1);
	});
});

describe('stale frames and recovery (Turn & Delivery controls)', () => {
	it('TD-1: a stale auth_ok with an older snapshot changes nothing after B is current', () => {
		authenticated('session-a', 'user-a');
		const a = sockets()[0];
		const b = authenticated('session-b', 'user-b', [runningTurn]);
		const before = {
			restore: calls.restore, recover: calls.recover, connected,
			sent: b.sent.length, turn: getTurnState('main-1'),
		};
		expect(subscribedThreads.has('main-1')).toBe(true);

		a.authOk('user-a', []);

		expect(calls.restore).toBe(before.restore);
		expect(calls.recover).toBe(before.recover);
		expect(connected).toBe(before.connected);
		expect(b.sent).toHaveLength(before.sent);
		expect(getTurnState('main-1')).toEqual(before.turn);
		expect(subscribedThreads.has('main-1')).toBe(true);
		expect(get(user)?.id).toBe('user-b');
	});

	it('TD-2: an input sent on A survives a dropped receipt and resolves from B without resend', async () => {
		const a = authenticated('session-a', 'user-a');
		const inputId = sendMessage('main-1', 'hello')!;
		const inputFrame = a.sent.find(frame => frame.input_id === inputId)!;
		reconnectNow();
		a.receive({ type: 'input_accepted', thread_id: 'main-1', input_id: inputId });
		expect(getTurnState('main-1').pendingInputIds).toEqual([inputId]);

		historyPage = async () => ({
			messages: [{
				id: 'canonical-1', role: 'user', content: 'hello',
				created_at: '2026-10-03T15:00:00Z', metadata: { input_id: inputId },
			}],
			has_more: false,
		});
		const b = sockets()[1];
		b.open();
		b.authOk('user-a');
		await flush();

		expect(getTurnState('main-1').pendingInputIds).toEqual([]);
		expect(b.types()).not.toContain(inputFrame.type);
		expect(calls.recover).toBe(2);
	});

	it.each([
		['absent', [], false],
		['present', [runningTurn], true],
	] as const)('TD-3: Stop on A settles from B when the turn is %s, with no automatic resend', (_label, snapshot, stillStopping) => {
		authenticated('session-a', 'user-a', [runningTurn]);
		stopTurn('main-1');
		expect(sockets()[0].types()).toContain('stop');
		reconnectNow();
		const b = sockets()[1];
		b.open();
		b.authOk('user-a', [...snapshot]);

		// InputBarActionRow's stopping predicate.
		const state = getTurnState('main-1');
		const stopping = state.phase === 'active'
			&& get(stopRequestedTurns).get('main-1') === state.turnId;
		expect(stopping).toBe(stillStopping);
		expect(b.types()).not.toContain('stop');
		if (stillStopping) {
			stopTurn('main-1');
			expect(b.types()).toContain('stop');
		}
	});

	it('TD-4: a mid-turn delta on retired A is dropped; B continues the same turn', () => {
		const deltas: unknown[] = [];
		const stopDeltas = on('text_delta', (frame) => { deltas.push(frame); });
		const a = authenticated('session-a', 'user-a', [runningTurn]);
		reconnectNow();
		a.receive({ type: 'text_delta', ...runningTurn, content: 'stale', seq: 1 });
		expect(deltas).toHaveLength(0);

		const b = sockets()[1];
		b.open();
		b.authOk('user-a', [runningTurn]);
		expect(connected).toBe(2);
		expect(getTurnState('main-1')).toMatchObject({ turnId: 'turn-1', turnOrder: 1 });
		b.receive({ type: 'text_delta', ...runningTurn, content: 'live', seq: 1 });
		expect(deltas).toHaveLength(1);
		stopDeltas();
	});

	it('TD-5: subscriptions go only to the socket whose auth_ok is handled', () => {
		const a = authenticated('session-a', 'user-a');
		const aSubscribes = a.types().filter(type => type === 'subscribe_session').length;
		const b = authenticated('session-b', 'user-b');
		const bSubscribes = b.types().filter(type => type === 'subscribe_session').length;
		expect(bSubscribes).toBeGreaterThan(0);
		a.authOk('user-a');
		expect(a.types().filter(type => type === 'subscribe_session')).toHaveLength(aSubscribes);
		expect(b.types().filter(type => type === 'subscribe_session')).toHaveLength(bSubscribes);
	});

	it('TD-6: recovery started under A writes nothing and reads no further page after B replaces it', async () => {
		signIn('session-a');
		const a = sockets()[0];
		a.open();
		const inputId = sendMessage('main-1', 'hello')!;
		const page = deferred<unknown>();
		const historyCalls: string[] = [];
		historyPage = (threadId) => { historyCalls.push(threadId); return page.promise; };
		const drain = deferred<unknown>();
		drainBody = () => drain.promise;
		a.authOk('user-a');
		await flush();
		expect(historyCalls).toHaveLength(1);
		const rowsBefore = get(messages);

		signIn('session-b');
		page.resolve({
			messages: [{
				id: 'canonical-a', role: 'user', content: 'hello',
				created_at: '2026-10-03T15:00:00Z', metadata: { input_id: inputId },
			}],
			has_more: true,
			oldest_id: 'canonical-a',
		});
		drain.resolve({ active: true, reason: 'restart', blockingTurns: [] });
		await flush();

		expect(historyCalls).toHaveLength(1);
		expect(getTurnState('main-1').pendingInputIds).toEqual([inputId]);
		expect(get(messages)).toEqual(rowsBefore);
		expect(get(drainStatus)).toBeNull();
	});

	it('TD-6: a drain read started under A is not applied after logout', async () => {
		const drain = deferred<unknown>();
		drainBody = () => drain.promise;
		authenticated('session-a', 'user-a');
		await logout();
		drain.resolve({ active: true, reason: 'restart', blockingTurns: [] });
		await flush();
		expect(get(drainStatus)).toBeNull();
	});

	it('TD-7: same-session reuse runs recovery and turn restore once per owner auth_ok', async () => {
		signIn('session-a');
		connect();
		const a = sockets()[0];
		a.open();
		connect();
		a.authOk('user-a');
		await flush();
		expect(sockets()).toHaveLength(1);
		expect(calls.recover).toBe(1);
		expect(calls.restore).toBe(1);
	});
});
