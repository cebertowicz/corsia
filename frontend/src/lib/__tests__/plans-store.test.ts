/**
 * Canonical plan-key owner: transition/failure matrix (reset verdict §4/§5).
 * Adapted from the reset's proven prototype probes.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { token } from '$lib/stores/auth';
import {
	entryFor, refresh, retire, cancelAll, resetPlanRuntimeForTests, requestSeqForTests,
} from '$lib/stores/plans.svelte';
import type { OperatorEnvelope } from '$lib/api/plans';

const BASE: OperatorEnvelope = {
	contract_version: 2, as_of: 'now',
	target: { kind: 'thread', id: 't1', label: 'Thread' },
	revision_token: 'base', plans: [],
	attention: { awaiting_count: 0 },
};

function envelope(tag: string): OperatorEnvelope {
	return { ...BASE, revision_token: tag };
}

interface Pending {
	url: string;
	etag: string | null;
	ok(tag: string, etagValue?: string): void;
	notModified(): void;
	fail(status?: number): void;
	boom(message?: string): void;
	/** 200 headers received, then the body stream dies — `res.json()` rejects. */
	truncated(): void;
}

/** A fetch mock whose responses are resolved explicitly, in any order. */
function controllableFetch() {
	const pending: Pending[] = [];
	const fn = vi.fn((url: string, init?: RequestInit) => {
		const headers = (init?.headers ?? {}) as Record<string, string>;
		let settle: (value: unknown) => void = () => {};
		let bomb: (reason: unknown) => void = () => {};
		const promise = new Promise((resolve, reject) => { settle = resolve; bomb = reject; });
		pending.push({
			url,
			etag: headers['If-None-Match'] ?? null,
			ok: (tag, etagValue = `"${tag}"`) => settle({
				ok: true, status: 200,
				json: () => Promise.resolve(envelope(tag)),
				headers: { get: (k: string) => (k === 'ETag' ? etagValue : null) },
			}),
			notModified: () => settle({
				ok: false, status: 304, json: () => Promise.resolve(null),
				headers: { get: () => null },
			}),
			fail: (status = 403) => settle({
				ok: false, status, json: () => Promise.resolve(null),
				text: () => Promise.resolve('nope'),
				headers: { get: () => null },
			}),
			boom: (message = 'network down') => bomb(new Error(message)),
			truncated: () => settle({
				ok: true, status: 200,
				json: () => Promise.reject(new TypeError('network error: body stream terminated')),
				headers: { get: (k: string) => (k === 'ETag' ? '"partial"' : null) },
			}),
		});
		return promise;
	});
	vi.stubGlobal('fetch', fn);
	return { pending, fn };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
	vi.unstubAllGlobals();
	resetPlanRuntimeForTests();
	token.set('bearer-1');
});
afterEach(() => vi.restoreAllMocks());

const T1 = { kind: 'thread', id: 't1' } as const;

describe('cold load', () => {
	it('idle -> loading -> ready', async () => {
		const net = controllableFetch();
		const p = refresh(T1);
		expect(entryFor('thread:t1').status).toBe('loading');
		net.pending[0].ok('A');
		await p;
		expect(entryFor('thread:t1').status).toBe('ready');
		expect(entryFor('thread:t1').payload?.revision_token).toBe('A');
		expect(entryFor('thread:t1').etag).toBe('"A"');
	});

	it('routes full workspace and project reads through distinct canonical keys', async () => {
		const net = controllableFetch();
		const workspace = refresh({ kind: 'workspace' });
		const project = refresh({ kind: 'project', id: 'project/weird id' });
		expect(net.pending.map((request) => request.url)).toEqual([
			'/api/plans/operator/scopes/workspace',
			'/api/plans/operator/scopes/projects/project%2Fweird%20id',
		]);
		net.pending[0].ok('WORKSPACE');
		net.pending[1].ok('PROJECT');
		await Promise.all([workspace, project]);
		expect(entryFor('workspace').payload?.revision_token).toBe('WORKSPACE');
		expect(entryFor('project:project/weird id').payload?.revision_token).toBe('PROJECT');
	});

	it('keeps compact Activity representations on independent keys and routes', async () => {
		const activity = (kind: 'workspace_scope' | 'project_scope', id: string) => ({
			contract_version: 1,
			representation: 'activity_scope_index',
			as_of: 'now',
			target: { kind, id, label: id },
			revision_token: `activity-${id}`,
			plans: [],
			attention: { awaiting_count: 0 },
		});
		const fetchMock = vi.fn(async (url: string) => ({
			ok: true,
			status: 200,
			json: async () => String(url).endsWith('/workspace')
				? activity('workspace_scope', 'workspace')
				: activity('project_scope', 'project/weird id'),
			headers: { get: () => '"activity"' },
		}));
		vi.stubGlobal('fetch', fetchMock);
		await Promise.all([
			refresh({ kind: 'activity-workspace' }),
			refresh({ kind: 'activity-project', id: 'project/weird id' }),
		]);
		expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
			'/api/plans/activity/scopes/workspace',
			'/api/plans/activity/scopes/projects/project%2Fweird%20id',
		]);
		expect(entryFor('activity:workspace').payload?.revision_token).toBe('activity-workspace');
		expect(entryFor('activity-project:project/weird id').payload?.revision_token).toBe('activity-project/weird id');
		expect(entryFor('workspace').payload).toBeNull();
	});
});

describe('concurrency', () => {
	it('two concurrent refreshes issue one request and queue exactly one follow-up', async () => {
		const net = controllableFetch();
		const a = refresh(T1);
		const b = refresh(T1);
		expect(net.pending.length).toBe(1);
		net.pending[0].ok('A');
		await a; await b;
		expect(net.pending.length).toBe(2);
		net.pending[1].ok('B');
		await flush(); await flush();
		expect(net.pending.length).toBe(2);
		expect(entryFor('thread:t1').payload?.revision_token).toBe('B');
	});

	it('an older in-flight response can never overwrite newer truth (out-of-order resolve)', async () => {
		const net = controllableFetch();
		const first = refresh(T1);
		net.pending[0].ok('OLD');
		await first;
		const second = refresh(T1);
		expect(net.pending.length).toBe(2);
		net.pending[1].ok('NEW');
		await second;
		expect(entryFor('thread:t1').payload?.revision_token).toBe('NEW');
		expect(entryFor('thread:t1').etag).toBe('"NEW"');
	});

	it('a late failure never erases a payload that already arrived', async () => {
		const net = controllableFetch();
		const p = refresh(T1);
		net.pending[0].ok('GOOD');
		await p;
		const q = refresh(T1);
		net.pending[1].boom();
		await q;
		expect(entryFor('thread:t1').payload?.revision_token).toBe('GOOD');
		expect(entryFor('thread:t1').status).toBe('stale');
	});
});

describe('request identity across retirement and recreation', () => {
	it('COUNTEREXAMPLE: key retired and recreated while the original request is still in flight', async () => {
		const net = controllableFetch();
		const orphan = refresh(T1);
		retire(T1);
		const revived = refresh(T1);
		expect(net.pending.length).toBe(2);
		net.pending[1].ok('NEW');
		await revived;
		net.pending[0].ok('OLD');
		await orphan;
		await flush();
		expect(entryFor('thread:t1').payload?.revision_token).toBe('NEW');
		expect(entryFor('thread:t1').etag).toBe('"NEW"');
	});

	it('a response arriving after retirement with no refetch does not resurrect the entry', async () => {
		const net = controllableFetch();
		const p = refresh(T1);
		retire(T1);
		net.pending[0].ok('GHOST');
		await p;
		expect(entryFor('thread:t1').status).toBe('idle');
		expect(entryFor('thread:t1').payload).toBe(null);
	});

	it('request ids are globally monotonic and never reset per key', async () => {
		const net = controllableFetch();
		const p = refresh(T1);
		net.pending[0].ok('A');
		await p;
		const before = requestSeqForTests();
		retire(T1);
		const q = refresh(T1);
		net.pending[1].ok('B');
		await q;
		expect(requestSeqForTests()).toBeGreaterThan(before);
	});
});

describe('auth', () => {
	it('COUNTEREXAMPLE: sign-out then an old 200 response — the 200 is discarded', async () => {
		const net = controllableFetch();
		const p = refresh(T1);
		net.pending[0].ok('A');
		await p;
		const q = refresh(T1);
		token.set(null);
		cancelAll();
		await refresh(T1);
		expect(entryFor('thread:t1').status).toBe('unauthorized');
		net.pending[1].ok('AFTER-SIGNOUT');
		await q; await flush();
		expect(entryFor('thread:t1').payload).toBeNull();
		expect(entryFor('thread:t1').status).toBe('unauthorized');
	});

	it('missing token with no payload is unauthorized, never silent idle', async () => {
		controllableFetch();
		token.set(null);
		await refresh(T1);
		expect(entryFor('thread:t1').status).toBe('unauthorized');
		expect(entryFor('thread:t1').errorMessage).toBe('Sign-in required.');
	});
});

describe('invalidation while loading', () => {
	it('a nudge during flight forces exactly one follow-up read, so a pre-commit 304 cannot stick', async () => {
		const net = controllableFetch();
		const first = refresh(T1);
		net.pending[0].ok('V1');
		await first;
		const inFlight = refresh(T1);
		refresh(T1);
		net.pending[1].notModified();
		await inFlight;
		await flush();
		expect(net.pending.length).toBe(3);
		net.pending[2].ok('V2');
		await flush(); await flush();
		expect(entryFor('thread:t1').payload?.revision_token).toBe('V2');
		expect(entryFor('thread:t1').status).toBe('ready');
	});

	it('304 marks fresh without discarding the payload', async () => {
		const net = controllableFetch();
		const p = refresh(T1);
		net.pending[0].ok('V1');
		await p;
		const q = refresh(T1);
		net.pending[1].notModified();
		await q;
		expect(entryFor('thread:t1').status).toBe('ready');
		expect(entryFor('thread:t1').payload?.revision_token).toBe('V1');
	});

	it('sends the stored ETag on revalidation', async () => {
		const net = controllableFetch();
		const p = refresh(T1);
		net.pending[0].ok('V1');
		await p;
		const q = refresh(T1);
		expect(net.pending[1].etag).toBe('"V1"');
		net.pending[1].notModified();
		await q;
	});
});

describe('errors', () => {
	it('preserves 401, 403, and 404 as distinct scope conditions', async () => {
		const net = controllableFetch();
		for (const [status, expected, message] of [
			[401, 'unauthorized', 'Sign-in required.'],
			[403, 'forbidden', 'You do not have access to this scope.'],
			[404, 'not-found', 'This selected scope is no longer available.'],
		] as const) {
			const p = refresh(T1);
			net.pending.at(-1)!.fail(status);
			await p;
			expect(entryFor('thread:t1').status).toBe(expected);
			expect(entryFor('thread:t1').errorMessage).toBe(message);
		}
	});

	it('a transport failure with no payload is error, with a safe message', async () => {
		const net = controllableFetch();
		const p = refresh(T1);
		net.pending[0].boom('ECONNREFUSED /Users/secret/x');
		await p;
		expect(entryFor('thread:t1').status).toBe('error');
	});

	it('a 500 body is never echoed to the user', async () => {
		const net = controllableFetch();
		const p = refresh(T1);
		net.pending[0].fail(500);
		await p;
		expect(entryFor('thread:t1').errorMessage).toBe('The server had a problem handling this request.');
	});
});

describe('cross-key isolation', () => {
	it('keeps thread and child reads isolated when they resolve in reverse order', async () => {
		const net = controllableFetch();
		const first = refresh({ kind: 'thread', id: 't1' });
		const child = refresh({ kind: 'child', id: 'n1' });
		const second = refresh({ kind: 'thread', id: 't2' });
		expect(net.pending.length).toBe(3);
		net.pending[2].ok('THREAD-2');
		net.pending[1].ok('CHILD');
		net.pending[0].ok('THREAD-1');
		await Promise.all([first, child, second]);
		expect(entryFor('thread:t1').payload?.revision_token).toBe('THREAD-1');
		expect(entryFor('child:n1').payload?.revision_token).toBe('CHILD');
		expect(entryFor('thread:t2').payload?.revision_token).toBe('THREAD-2');
	});
});

describe('cache bound', () => {
	it('enforceCacheCap retires unobserved keys beyond the cap, keeping observed ones', async () => {
		const { enforceCacheCap } = await import('$lib/stores/plans.svelte');
		const net = controllableFetch();
		const a = refresh({ kind: 'thread', id: 't1' });
		net.pending[0].ok('t1');
		await a;
		const b = refresh({ kind: 'thread', id: 't2' });
		net.pending[1].ok('t2');
		await b;
		enforceCacheCap(new Set(['thread:t1']), 0);
		expect(entryFor('thread:t2').payload).toBeNull();
		expect(entryFor('thread:t1').payload?.revision_token).toBe('t1');
	});
});

// ---------------------------------------------------------------------------
// F1/F2 coalescer remedy (wide-lens PASS verdict, Design A). Cases A1-A5 and
// B1-B4 are ported 1:1 from the verdict's own probes
// (zwl-lifecycle.test.ts, describes A and B) into this file's existing
// helpers, per the implementation fence.
// ---------------------------------------------------------------------------

describe('F1: coalescer slot ownership across supersession', () => {
	it('A1 retire -> recreate: the orphan must not disarm the recreated key', async () => {
		const net = controllableFetch();
		void refresh(T1); // req 1, becomes an orphan
		retire(T1);
		void refresh(T1); // req 2, the live request
		net.pending[0].ok('ORPHAN');
		await flush();
		void refresh(T1); // an ambient trigger: must coalesce into req 2
		await flush();
		expect(net.pending.length).toBe(2);
	});

	it('A2 cancelAll (auth rotation): the orphan must not disarm the reissued request', async () => {
		const net = controllableFetch();
		void refresh(T1); // req 1
		cancelAll();
		void refresh(T1); // req 2 on the same runtime object
		net.pending[0].ok('ORPHAN');
		await flush();
		void refresh(T1); // ambient trigger: must coalesce
		await flush();
		expect(net.pending.length).toBe(2);
	});

	it('A3 three repeated orphan cycles then one trigger stays coalesced', async () => {
		const net = controllableFetch();
		void refresh(T1);
		retire(T1); void refresh(T1);
		retire(T1); void refresh(T1);
		retire(T1); void refresh(T1); // req 4 is live
		net.pending[0].ok('o1'); net.pending[1].ok('o2'); net.pending[2].ok('o3');
		await flush();
		void refresh(T1);
		await flush();
		expect(net.pending.length).toBe(4);
	});

	it('A4 an orphan must not consume the live runtime’s queued follow-up', async () => {
		const net = controllableFetch();
		void refresh(T1); // req 1 -> orphan
		retire(T1);
		void refresh(T1); // req 2, live
		void refresh(T1); // queues a follow-up on req 2
		net.pending[0].ok('ORPHAN');
		await flush();
		// The orphan must neither issue nor steal the queued follow-up.
		expect(net.pending.length).toBe(2);
		net.pending[1].ok('LIVE');
		await flush();
		// Exactly one follow-up, issued after req 2 settled.
		expect(net.pending.length).toBe(3);
	});

	it('A5 (control) the orphan itself is still inadmissible', async () => {
		const net = controllableFetch();
		void refresh(T1);
		retire(T1);
		void refresh(T1);
		net.pending[1].ok('NEW');
		await flush();
		net.pending[0].ok('ORPHAN');
		await flush();
		expect(entryFor('thread:t1').payload?.revision_token).toBe('NEW');
	});
});

describe('F2: liveness of the in-flight slot', () => {
	it('B1 a body-stream failure must not permanently wedge the key', async () => {
		const net = controllableFetch();
		void refresh(T1).catch(() => {});
		net.pending[0].truncated();
		await flush();
		void refresh(T1).catch(() => {}); // a later ambient trigger
		await flush();
		expect(net.pending.length).toBe(2);
	});

	it('B2 a body-stream failure must leave a truthful, non-refreshing entry', async () => {
		const net = controllableFetch();
		void refresh(T1).catch(() => {});
		net.pending[0].truncated();
		await flush();
		expect(entryFor('thread:t1').refreshing).toBe(false);
		expect(entryFor('thread:t1').status).toBe('error');
	});

	it('B3 refresh() must not reject its callers (every caller uses `void refresh(...)`)', async () => {
		const net = controllableFetch();
		const p = refresh(T1);
		net.pending[0].truncated();
		await expect(p).resolves.toBeUndefined();
	});

	it('B4 (control) an ordinary fetch rejection already recovers', async () => {
		const net = controllableFetch();
		void refresh(T1);
		net.pending[0].boom();
		await flush();
		expect(entryFor('thread:t1').status).toBe('error');
		void refresh(T1);
		await flush();
		expect(net.pending.length).toBe(2);
	});
});
