import { beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

import {
	activeThreadId,
	pendingDeltasByThread,
} from '../stores/threads';
import { token, user } from '../stores/auth';
import { connectionError, connectionState, drainStatus, refreshDrainStatus } from '../stores/connection';
import { setWs, subscribedThreads } from './state';
import { handleMessage } from './handlers';
import { handleAuthOk, handleSystemDraining, handleWsError } from './handlersConnection';
import { on } from './events';
import { resetUpdateStoreForTests, updateOperation } from '../update/store';
import { stopRequestedTurns } from './send';
import {
	_resetTurnStateForTests,
	applyLifecycle,
	getTurnState,
} from './turnState';
import { clearAllContextUsage, setContextUsage, getContextUsage } from '../stores/contextUsageByThread';

beforeEach(() => {
	activeThreadId.set(null);
	clearAllContextUsage();
	token.set(null);
	user.set(null);
	subscribedThreads.clear();
	setWs(null);
	drainStatus.set(null);
	stopRequestedTurns.set(new Map());
	_resetTurnStateForTests();
});

describe('drain status', () => {
	it('reads the update operation when a drain frame arrives, so a second device sees an update started elsewhere', async () => {
		token.set('t');
		const fetchMock = vi.fn(async () => new Response(JSON.stringify({
			available: true, operation: { operation_id: 'u1', kind: 'update', status: 'in_flight', phase: 'draining' },
		})));
		vi.stubGlobal('fetch', fetchMock);
		handleSystemDraining({ draining: true, reason: 'update' });
		await vi.waitFor(() => expect(get(updateOperation)?.phase).toBe('draining'));
		expect(String((fetchMock.mock.calls as unknown[][])[0][0])).toContain('/api/system/update-status');
		vi.unstubAllGlobals();
		resetUpdateStoreForTests();
	});

	it('retains the exact active turn blocker for restart UX', () => {
		handleSystemDraining({
			draining: true,
			reason: 'update',
			activeProcesses: 2,
			activeTurns: 1,
			hostAccessSupervisors: 1,
			ownedProcessGroups: 0,
			browserCommands: 0,
			turnAdmissions: 0,
			blockingTurns: [{
				threadId: 'thread-1',
				turnId: 'turn-1',
				turnOrder: 7,
				executionClass: 'native',
				title: 'Inspect Batch 2 history/tool storage',
				startedAt: '2026-08-03T21:47:32Z',
			}],
		});

		expect(get(drainStatus)).toMatchObject({
			activeProcesses: 2,
			activeTurns: 1,
			blockingTurns: [{
				threadId: 'thread-1',
				title: 'Inspect Batch 2 history/tool storage',
			}],
		});
	});

	it('seeds authoritative drain state after authentication', async () => {
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
			ok: true,
			json: async () => ({
				active: true,
				reason: 'update',
				activeProcesses: 1,
				activeTurns: 1,
				turnAdmissions: 0,
				hostAccessSupervisors: 0,
				ownedProcessGroups: 0,
				browserCommands: 0,
				countdown: null,
				blockingTurns: [{
					threadId: 'thread-live',
					turnId: 'turn-live',
					turnOrder: 8,
					executionClass: 'native',
					title: 'Live blocker',
					startedAt: '2026-08-03T23:00:00Z',
				}],
			}),
		}));

		handleAuthOk({ user: { id: 'steve' }, active_turns: [] }, vi.fn());
		await vi.waitFor(() => {
			expect(get(drainStatus)?.blockingTurns[0]?.threadId).toBe('thread-live');
		});
		expect(fetch).toHaveBeenCalledWith(
			'/api/system/drain?token=test-token',
			{ cache: 'no-store' },
		);
		vi.unstubAllGlobals();
	});

	it('does not let an older auth seed erase a newer drain frame', async () => {
		token.set('test-token');
		let resolveResponse!: (response: object) => void;
		vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise((resolve) => {
			resolveResponse = resolve;
		})));

		handleAuthOk({ user: { id: 'steve' }, active_turns: [] }, vi.fn());
		handleSystemDraining({
			draining: true,
			reason: 'update',
			activeProcesses: 1,
			activeTurns: 1,
			turnAdmissions: 0,
			hostAccessSupervisors: 0,
			ownedProcessGroups: 0,
			browserCommands: 0,
			countdown: null,
			blockingTurns: [{
				threadId: 'thread-newer',
				turnId: 'turn-newer',
				turnOrder: 9,
				executionClass: 'native',
				title: 'Newer blocker',
				startedAt: '2026-08-03T23:01:00Z',
			}],
		});
		resolveResponse({
			ok: true,
			json: async () => ({ active: false }),
		});
		const drainReads = () => vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('/api/system/drain'));
		await vi.waitFor(() => expect(drainReads()).toHaveLength(1));
		await Promise.resolve();

		expect(get(drainStatus)?.blockingTurns[0]?.threadId).toBe('thread-newer');
		vi.unstubAllGlobals();
	});

	it('only applies the newest overlapping drain refresh request', async () => {
		const responses: Array<(response: object) => void> = [];
		vi.stubGlobal('fetch', vi.fn().mockImplementation(() => (
			new Promise((resolve) => responses.push(resolve))
		)));

		const older = refreshDrainStatus('test-token');
		const newer = refreshDrainStatus('test-token');
		responses[0]({
			ok: true,
			json: async () => ({
				active: true,
				activeProcesses: 1,
				activeTurns: 1,
				blockingTurns: [{
					threadId: 'thread-older',
					turnId: 'turn-older',
					turnOrder: 1,
					executionClass: 'native',
					title: 'Older request',
					startedAt: '2026-08-03T23:00:00Z',
				}],
			}),
		});
		await older;
		expect(get(drainStatus)).toBeNull();

		responses[1]({
			ok: true,
			json: async () => ({
				active: true,
				activeProcesses: 1,
				activeTurns: 1,
				blockingTurns: [{
					threadId: 'thread-newest',
					turnId: 'turn-newest',
					turnOrder: 2,
					executionClass: 'native',
					title: 'Newest request',
					startedAt: '2026-08-03T23:01:00Z',
				}],
			}),
		});
		await newer;

		expect(get(drainStatus)?.blockingTurns[0]?.threadId).toBe('thread-newest');
		vi.unstubAllGlobals();
	});
});

describe('auth turn restoration', () => {
	it('retires a receipt retained across a provider reconnect', () => {
		setContextUsage('thread-1', { total_used: 90 } as never);

		handleAuthOk({ user: { id: 'owner-1' }, active_turns: [] }, vi.fn());

		expect(getContextUsage('thread-1')).toBeNull();
	});


	it('restores descendant motion truth from the snapshot roots', () => {
		handleAuthOk({
			user: { id: 'owner-1' },
			active_turns: [
				{ thread_id: 'sub-cc', turn_id: 'turn-cc', turn_order: 7, visible_root_thread_id: 'parent-1' },
				{ thread_id: 'parent-1', turn_id: 'turn-own', turn_order: 8, visible_root_thread_id: 'parent-1' },
			],
		}, vi.fn());

		// A reconnect learns the subagent tree from the snapshot alone: the
		// hidden thread's Turn carries its visible root, and the visible
		// thread's own Turn names itself.
		expect(getTurnState('sub-cc')).toMatchObject({
			phase: 'active', visibleRootThreadId: 'parent-1',
		});
		expect(getTurnState('parent-1')).toMatchObject({
			phase: 'active', visibleRootThreadId: 'parent-1',
		});
	});

	it('treats a snapshot entry without a root as unknown lineage, not an error', () => {
		handleAuthOk({
			user: { id: 'owner-1' },
			active_turns: [
				{ thread_id: 'sub-cc', turn_id: 'turn-cc', turn_order: 7 },
			],
		}, vi.fn());

		expect(getTurnState('sub-cc')).toMatchObject({
			phase: 'active', visibleRootThreadId: null,
		});
	});

	it('replaces reconnect truth from active_turns', () => {
		applyLifecycle('omitted', { turnId: 'old-omitted', turnOrder: 1 });
		applyLifecycle('successor', { turnId: 'old-turn', turnOrder: 2 });
		const resetHeartbeat = vi.fn();

		handleAuthOk({
			user: { id: 'owner-1' },
			active_turns: [
				{ thread_id: 'successor', turn_id: 'new-turn', turn_order: 3 },
				{ thread_id: 'restored', turn_id: 'restored-turn', turn_order: 4 },
			],
		}, resetHeartbeat);

		expect(resetHeartbeat).toHaveBeenCalledOnce();
		expect(getTurnState('omitted')).toMatchObject({
			turnId: 'old-omitted', turnOrder: 1, phase: 'terminal',
		});
		expect(getTurnState('successor')).toMatchObject({
			turnId: 'new-turn', turnOrder: 3, phase: 'active',
		});
		expect(getTurnState('restored')).toMatchObject({
			turnId: 'restored-turn', turnOrder: 4, phase: 'active',
		});
	});

	it('inactive snapshot releases the reconnect barrier without erasing terminal identity', () => {
		activeThreadId.set('omitted');
		applyLifecycle('omitted', { turnId: 'known-turn', turnOrder: 9 });
		setWs({
			readyState: WebSocket.OPEN,
			send: vi.fn(),
		} as unknown as WebSocket);

		handleAuthOk({
			user: { id: 'owner-1' },
			active_turns: [],
		}, vi.fn());

		expect(getTurnState('omitted')).toMatchObject({
			turnId: 'known-turn', turnOrder: 9, phase: 'terminal',
		});
		expect(get(pendingDeltasByThread).has('omitted')).toBe(true);

		handleMessage({
			type: 'session_snapshot',
			thread_id: 'omitted',
			turn_id: null,
			turn_order: null,
			state: null,
		}, () => {});

		expect(get(pendingDeltasByThread).has('omitted')).toBe(false);
		expect(getTurnState('omitted')).toMatchObject({
			turnId: 'known-turn', turnOrder: 9, phase: 'terminal',
		});
	});
});

describe('Stop receipt frames', () => {
	it('emits the exact receipt for the requester UI', () => {
		const received: unknown[] = [];
		const unsubscribe = on('stop_receipt', frame => received.push(frame));
		const receipt = {
			type: 'stop_receipt', thread_id: 'thread-1', outcome: 'stale_active', turn_id: 'turn-old',
		};

		handleMessage(receipt, () => {});

		expect(received).toEqual([receipt]);
		unsubscribe();
	});

	it('emits user-scoped warm progress through the websocket dispatcher', () => {
		const received: unknown[] = [];
		const unsubscribe = on('thread_warm_progress', frame => received.push(frame));
		const progress = {
			type: 'thread_warm_progress', thread_id: 'agent-1', model: 'omlx@srv/model',
			progress: { total_blocks: 10, ready_blocks: 4 },
		};

		handleMessage(progress, () => {});

		expect(received).toEqual([progress]);
		unsubscribe();
	});

	it('re-enables Stop after its visible failure without ending the exact turn', () => {
		applyLifecycle('thread-1', { turnId: 'turn-1', turnOrder: 1 });
		stopRequestedTurns.set(new Map([['thread-1', 'turn-1']]));

		handleWsError({
			type: 'error', thread_id: 'thread-1',
			error_type: 'stop_failed',
			turn_id: 'turn-1',
			message: 'Stop failed: exact turn turn-1 remains active after Stop',
		});

		expect(get(stopRequestedTurns).has('thread-1')).toBe(false);
		expect(getTurnState('thread-1')).toMatchObject({
			phase: 'active', turnId: 'turn-1', turnOrder: 1,
		});
	});

	it('does not let an older Stop failure clear a successor Stop marker', () => {
		stopRequestedTurns.set(new Map([['thread-1', 'turn-B']]));

		handleWsError({
			type: 'error', thread_id: 'thread-1',
			error_type: 'stop_failed', turn_id: 'turn-A',
			message: 'Stop failed: cleanup for turn-A failed',
		});

		expect(get(stopRequestedTurns).get('thread-1')).toBe('turn-B');
	});

	it('clears auth and reports the invalid-or-expired-session string', () => {
		token.set('a-session-token');
		user.set({ id: 'demo', name: 'demo' });

		handleWsError({ type: 'error', message: 'Invalid or expired session' });

		expect(get(token)).toBeNull();
		expect(get(user)).toBeNull();
		expect(get(connectionState)).toBe('error');
		expect(get(connectionError)).toBe('Invalid or expired session');
	});
});
