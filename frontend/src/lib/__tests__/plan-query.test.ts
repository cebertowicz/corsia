/** planQuery/planKeySet: observation bookkeeping in isolation (reset verdict §6 step 2). */
import { flushSync, mount, tick, unmount } from 'svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { token } from '$lib/stores/auth';
import { connectionState } from '$lib/stores/connection';
import { emit } from '$lib/ws/events';
import TestPlanQuery from './fixtures/TestPlanQuery.svelte';
import { reactiveProps } from './fixtures/reactiveProps.svelte';
import { resetPlanRuntimeForTests, entryFor } from '$lib/stores/plans.svelte';
import {
	ambientActiveForTests, observerCountForTests, resetObserversForTests,
} from '$lib/stores/planQuery.svelte';
import type { OperatorEnvelope } from '$lib/api/plans';

function envelope(id: string, tag = 'r1'): OperatorEnvelope {
	return {
		contract_version: 2, as_of: 'now', target: { kind: 'thread', id, label: 'Thread' },
		revision_token: tag, plans: [], attention: { awaiting_count: 0 },
	};
}

function ok(body: OperatorEnvelope, etag = '"e1"') {
	return { ok: true, status: 200, json: async () => body, headers: { get: (k: string) => (k === 'ETag' ? etag : null) } };
}

function routedFetch(routes: Record<string, () => unknown>) {
	const calls: string[] = [];
	const fn = vi.fn(async (url: string) => {
		calls.push(url);
		const key = Object.keys(routes).find((r) => url.includes(r));
		if (!key) return { ok: false, status: 404, json: async () => null, headers: { get: () => null } };
		const result = routes[key]();
		if (result instanceof Error) throw result;
		return result;
	});
	vi.stubGlobal('fetch', fn);
	return { calls, fn };
}

/** Past the old 45 s refresh loop: a timer-driven read would land inside it. */
const NO_POLL_WINDOW_MS = 60_000;

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

async function settle() {
	await tick();
	flushSync();
	await new Promise((r) => setTimeout(r, 0));
	flushSync();
}

beforeEach(() => {
	vi.unstubAllGlobals();
	resetObserversForTests();
	resetPlanRuntimeForTests();
	token.set('bearer-1');
	connectionState.set('connected');
	host = document.createElement('div');
	document.body.appendChild(host);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	resetObserversForTests();
	vi.restoreAllMocks();
	vi.useRealTimers();
});

const q = (id: string) => host.querySelector(`[data-testid="${id}"]`);

describe('observation refcounting', () => {
	it('0->1 observe issues exactly one load', async () => {
		const net = routedFetch({ '/threads/t-1': () => ok(envelope('t-1')) });
		component = mount(TestPlanQuery, { target: host, props: { getKey: () => ({ kind: 'thread', id: 't-1' }) } });
		await settle();
		expect(net.calls.length).toBe(1);
		expect(q('q-status')?.textContent).toBe('ready');
	});

	it('two declarations of the same key (1->2) issue exactly one load total', async () => {
		const net = routedFetch({ '/threads/t-1': () => ok(envelope('t-1')) });
		const getKey = () => ({ kind: 'thread' as const, id: 't-1' });
		const a = mount(TestPlanQuery, { target: host, props: { getKey } });
		const secondHost = document.createElement('div');
		document.body.appendChild(secondHost);
		const b = mount(TestPlanQuery, { target: secondHost, props: { getKey } });
		await settle();
		expect(net.calls.length).toBe(1);
		expect(observerCountForTests('thread:t-1')).toBe(2);
		unmount(a); unmount(b); secondHost.remove();
	});

	it('release to 0 retires a child key', async () => {
		routedFetch({ '/nodes/n1/child': () => ok(envelope('child-t')) });
		component = mount(TestPlanQuery, { target: host, props: { getKey: () => ({ kind: 'child', id: 'n1' }) } });
		await settle();
		expect(entryFor('child:n1').status).toBe('ready');
		unmount(component); component = null;
		await settle();
		expect(entryFor('child:n1').status).toBe('idle');
	});
});

describe('ambient trigger lifecycle', () => {
	it('issues no request on a timer or a visibility change while observed', async () => {
		vi.useFakeTimers();
		const net = routedFetch({ '/threads/t-1': () => ok(envelope('t-1')) });
		component = mount(TestPlanQuery, { target: host, props: { getKey: () => ({ kind: 'thread', id: 't-1' }) } });
		flushSync();
		await vi.advanceTimersByTimeAsync(1);
		const after = net.calls.length;
		await vi.advanceTimersByTimeAsync(NO_POLL_WINDOW_MS * 3);
		document.dispatchEvent(new Event('visibilitychange'));
		await vi.advanceTimersByTimeAsync(1);
		expect(net.calls.length).toBe(after);
	});

	it('re-reads once when the earliest wait becomes overdue, then stops', async () => {
		vi.useFakeTimers();
		const overdueAt = new Date(Date.now() + 90_000).toISOString();
		const waiting = envelope('t-1');
		waiting.plans = [{
			plan: { id: 'p1', state: 'active', revision: 1 }, owner: { kind: 'thread', id: 't-1' },
			progress: { done: 0, total: 1, open: 1, distribution: {} },
			display: { label_key: 'waiting', tone_key: 'waiting', not_moving: { next_overdue_at: overdueAt } },
			oversize: null, current_node_ids: [], next_node_ids: [], nodes: [],
		} as never];
		let reads = 0;
		const net = routedFetch({ '/threads/t-1': () => ok(reads++ === 0 ? waiting : envelope('t-1'), `"e${reads}"`) });
		component = mount(TestPlanQuery, { target: host, props: { getKey: () => ({ kind: 'thread', id: 't-1' }) } });
		flushSync();
		await vi.advanceTimersByTimeAsync(1);
		expect(net.calls.length).toBe(1);
		await vi.advanceTimersByTimeAsync(60_000);
		expect(net.calls.length).toBe(1);
		await vi.advanceTimersByTimeAsync(31_000);
		expect(net.calls.length).toBe(2);
		await vi.advanceTimersByTimeAsync(NO_POLL_WINDOW_MS * 3);
		expect(net.calls.length).toBe(2);
	});

	it('a nudge refreshes only the observed key it names', async () => {
		const net = routedFetch({
			'/threads/t-1': () => ok(envelope('t-1')),
			'/threads/t-2': () => ok(envelope('t-2')),
		});
		component = mount(TestPlanQuery, { target: host, props: { getKey: () => ({ kind: 'thread', id: 't-1' }) } });
		const otherHost = document.createElement('div');
		document.body.appendChild(otherHost);
		const other = mount(TestPlanQuery, { target: otherHost, props: { getKey: () => ({ kind: 'thread', id: 't-2' }) } });
		await settle();
		net.calls.length = 0;
		emit('plans_changed', {
			type: 'plans_changed', contract_version: 1,
			plans: [{ plan_id: 'p', revision: 2, owner: { kind: 'thread', id: 't-2' }, scope: { workspace_id: 'w', project_id: null } }],
		});
		await settle();
		expect(net.calls.map((url) => url.split('/threads/')[1])).toEqual(['t-2']);
		unmount(other); otherHost.remove();
	});

	it('starts with the first observer, stops with the last', async () => {
		routedFetch({ '/threads/t-1': () => ok(envelope('t-1')) });
		expect(ambientActiveForTests()).toBe(false);
		component = mount(TestPlanQuery, { target: host, props: { getKey: () => ({ kind: 'thread', id: 't-1' }) } });
		await settle();
		expect(ambientActiveForTests()).toBe(true);
		unmount(component); component = null;
		await settle();
		expect(ambientActiveForTests()).toBe(false);
	});

	it('post-unmount timer/visibility/reconnect/nudge issue nothing', async () => {
		vi.useFakeTimers();
		const net = routedFetch({ '/threads/t-1': () => ok(envelope('t-1')) });
		component = mount(TestPlanQuery, { target: host, props: { getKey: () => ({ kind: 'thread', id: 't-1' }) } });
		flushSync();
		await vi.advanceTimersByTimeAsync(1);
		flushSync();
		unmount(component); component = null;
		flushSync();
		const after = net.calls.length;
		await vi.advanceTimersByTimeAsync(NO_POLL_WINDOW_MS * 3);
		document.dispatchEvent(new Event('visibilitychange'));
		connectionState.set('disconnected');
		connectionState.set('connected');
		emit('plans_changed', {
			type: 'plans_changed', contract_version: 1,
			plans: [{ plan_id: 'p', revision: 2, owner: { kind: 'thread', id: 't-1' }, scope: { workspace_id: 'w', project_id: null } }],
		});
		await vi.advanceTimersByTimeAsync(1);
		expect(net.calls.length).toBe(after);
	});
});

describe('key-string identity stability', () => {
	it('does not churn observation when the caller rebuilds an equal key object', async () => {
		const net = routedFetch({ '/threads/t-1': () => ok(envelope('t-1')) });
		const props = reactiveProps<{ n: number }>({ n: 0 });
		component = mount(TestPlanQuery, {
			target: host,
			// A fresh object literal every recomputation — same string, different reference.
			props: { getKey: () => (props.n, { kind: 'thread', id: 't-1' }) },
		});
		await settle();
		expect(net.calls.length).toBe(1);
		props.n = 1;
		await settle();
		props.n = 2;
		await settle();
		expect(net.calls.length).toBe(1);
		expect(observerCountForTests('thread:t-1')).toBe(1);
	});

	it('declaring a key from the query effect does not self-retrigger (untracked declaration)', async () => {
		const net = routedFetch({ '/threads/t-1': () => ok(envelope('t-1')) });
		component = mount(TestPlanQuery, { target: host, props: { getKey: () => ({ kind: 'thread', id: 't-1' }) } });
		await settle();
		await settle();
		await settle();
		// Cold load only — the declaration effect must never depend on the payload it loads.
		expect(net.calls.length).toBe(1);
	});
});
