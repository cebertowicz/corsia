import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { token } from './auth';
import { endVisit, loadUpdateCards, openAtDivider, threadUpdateCards, visitDividers, watchUpdateCardNudges } from './threadUpdateCards';

import { emit } from '$lib/ws/events';

const divider = { since: '2026-09-29T09:00:00.000000', first_seq: 4 };

function card(id: number, afterSeq = 1) {
	return {
		id, thread_id: 't1', kind: 'done', node_id: `n${id}`, title: `Item ${id}`,
		after_seq: afterSeq, created_at: '2026-09-29T10:00:00.000000', node_exists: true, asks_user: true,
	};
}

function respond(...pages: object[]) {
	const fetchMock = vi.fn();
	for (const page of pages) {
		fetchMock.mockResolvedValueOnce({ ok: true, json: async () => page });
	}
	vi.stubGlobal('fetch', fetchMock);
	return fetchMock;
}

beforeEach(() => {
	token.set('bearer');
	threadUpdateCards.set({});
	visitDividers.set({});
	openAtDivider.set(null);
});

afterEach(() => {
	vi.unstubAllGlobals();
	token.set(null);
});

it('a replacement nudge refreshes even the first pending read and rejects its stale cards', async () => {
	const resolves: Array<(response: object) => void> = [];
	vi.stubGlobal('fetch', vi.fn(() => new Promise(resolve => { resolves.push(resolve); })));
	watchUpdateCardNudges();
	const stale = loadUpdateCards('replacement-pending', 7);
	for (let i = 0; i < 20; i++) emit('thread_updates_changed', { thread_ids: ['replacement-pending'] });
	expect(resolves).toHaveLength(1);
	resolves[0]({ ok: true, json: async () => ({ cards: [card(1)], divider: null }) });
	await stale;
	expect(get(threadUpdateCards)['replacement-pending']).toBeUndefined();
	expect(resolves).toHaveLength(2);
	resolves[1]({ ok: true, json: async () => ({ cards: [card(2)], divider: null }) });
	await vi.waitFor(() => expect(get(threadUpdateCards)['replacement-pending']?.cards.map(item => item.id)).toEqual([2]));
	endVisit('replacement-pending');
});

it('coalesces all nudge types and follows a failed read with the latest window', async () => {
	respond({ cards: [card(1)], divider: null });
	await loadUpdateCards('burst', 0);
	const resolves: Array<(response: object) => void> = [];
	const fetchMock = vi.fn((_url: string) => new Promise(resolve => { resolves.push(resolve); }));
	vi.stubGlobal('fetch', fetchMock);
	watchUpdateCardNudges();
	const pending = loadUpdateCards('burst', 3);
	for (let i = 0; i < 20; i++) {
		emit('plans_changed', { type: 'plans_changed', contract_version: 1, plans: [{ plan_id: 'p', revision: i, owner: { kind: 'thread', id: 'burst' }, scope: { workspace_id: 'w', project_id: null } }] });
		emit('artifacts_changed', { artifact_id: 'a' });
		emit('thread_updates_changed', { thread_ids: ['burst'] });
	}
	expect(fetchMock).toHaveBeenCalledTimes(1);
	resolves[0]({ ok: false, status: 503 });
	await pending;
	expect(fetchMock).toHaveBeenCalledTimes(2);
	expect(String(fetchMock.mock.calls[1][0])).toContain('from_seq=3');
	resolves[1]({ ok: true, json: async () => ({ cards: [card(2)], divider: null }) });
	await vi.waitFor(() => expect(get(threadUpdateCards).burst.cards[0].id).toBe(2));
	endVisit('burst');
});

it('reads an explicit new window immediately and refreshes that window after a nudge', async () => {
	const resolves: Array<(response: object) => void> = [];
	const fetchMock = vi.fn((_url: string) => new Promise(resolve => { resolves.push(resolve); }));
	vi.stubGlobal('fetch', fetchMock);
	watchUpdateCardNudges();
	const first = loadUpdateCards('window', 10, { divider: true });
	emit('thread_updates_changed', { thread_ids: ['window'] });
	const next = loadUpdateCards('window', 3);
	expect(fetchMock).toHaveBeenCalledTimes(2);
	expect(String(fetchMock.mock.calls[0][0])).toContain('from_seq=10&divider=true');
	expect(String(fetchMock.mock.calls[1][0])).toContain('from_seq=3');
	resolves[1]({ ok: true, json: async () => ({ cards: [card(2)], divider: null }) });
	await next;
	resolves[0]({ ok: true, json: async () => ({ cards: [card(1)], divider }) });
	await first;
	expect(get(visitDividers).window).toEqual({ divider, failed: false });
	expect(fetchMock).toHaveBeenCalledTimes(3);
	expect(String(fetchMock.mock.calls[2][0])).toContain('from_seq=3');
	resolves[2]({ ok: true, json: async () => ({ cards: [card(3)], divider: null }) });
	await vi.waitFor(() => expect(get(threadUpdateCards).window.cards[0].id).toBe(3));
	endVisit('window');
});

it('does not follow a dirty read after its visit ends', async () => {
	let resolveResponse!: (response: object) => void;
	const fetchMock = vi.fn(() => new Promise(resolve => { resolveResponse = resolve; }));
	vi.stubGlobal('fetch', fetchMock);
	watchUpdateCardNudges();
	const pending = loadUpdateCards('closed', 0, { divider: true });
	emit('thread_updates_changed', { thread_ids: ['closed'] });
	endVisit('closed');
	resolveResponse({ ok: true, json: async () => ({ cards: [card(1)], divider }) });
	await pending;
	expect(fetchMock).toHaveBeenCalledTimes(1);
	expect(get(threadUpdateCards).closed).toBeUndefined();
	expect(get(visitDividers).closed).toBeUndefined();
});

describe('threadUpdateCards', () => {
	it('uses one newest-window read and freezes the divider read on open', async () => {
		const fetchMock = respond({ cards: [card(1), card(2), card(3, 2)], has_more: true, divider });

		await loadUpdateCards('t1', 0, { divider: true });

		expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([
			'/api/threads/t1/updates?from_seq=0&divider=true',
		]);
		expect(get(threadUpdateCards).t1.cards.map((c) => c.id)).toEqual([1, 2, 3]);
		expect(get(visitDividers).t1).toEqual({ divider, failed: false });
	});

	it('keeps the divider through later reads and drops it when the visit ends', async () => {
		respond({ cards: [], has_more: false, divider });
		await loadUpdateCards('t1', 0, { divider: true });
		const fetchMock = respond({ cards: [card(1)], has_more: false, divider: null });

		await loadUpdateCards('t1', 5);

		expect(String(fetchMock.mock.calls[0][0])).toBe('/api/threads/t1/updates?from_seq=5');
		expect(get(visitDividers).t1).toEqual({ divider, failed: false });
		endVisit('t1');
		expect('t1' in get(visitDividers)).toBe(false);
		expect('t1' in get(threadUpdateCards)).toBe(false);
	});

	it('does not restore a divider when its visit ends before the opening read returns', async () => {
		let resolveResponse!: (response: object) => void;
		vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => { resolveResponse = resolve; })));

		const loading = loadUpdateCards('t1', 0, { divider: true });
		endVisit('t1');
		resolveResponse({ ok: true, json: async () => ({ cards: [], has_more: false, divider }) });
		await loading;

		expect('t1' in get(visitDividers)).toBe(false);
	});

	it('keeps the newest divider when overlapping opening reads finish out of order', async () => {
		const resolves: Array<(response: object) => void> = [];
		vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => { resolves.push(resolve); })));
		const newer = { since: '2026-09-29T09:30:00.000000', first_seq: 5 };

		const olderRead = loadUpdateCards('t1', 0, { divider: true });
		const newerRead = loadUpdateCards('t1', 0, { divider: true });
		resolves[1]({ ok: true, json: async () => ({ cards: [card(2)], has_more: false, divider: newer }) });
		await newerRead;
		resolves[0]({ ok: true, json: async () => ({ cards: [card(1)], has_more: false, divider }) });
		await olderRead;

		expect(get(visitDividers).t1).toEqual({ divider: newer, failed: false });
		expect(get(threadUpdateCards).t1.cards.map((item) => item.id)).toEqual([2]);
	});

	it('keeps the last card reading when a refresh fails', async () => {
		respond({ cards: [card(1)], has_more: false, divider: null });
		await loadUpdateCards('t1', 0);
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 503 }));

		await loadUpdateCards('t1', 0);

		expect(get(threadUpdateCards).t1.cards.map((c) => c.id)).toEqual([1]);
	});

	it('records and logs a failed opening read, then clears divider landing', async () => {
		const error = vi.spyOn(console, 'error').mockImplementation(() => {});
		openAtDivider.set('t1');
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 503 }));

		await loadUpdateCards('t1', 0, { divider: true });

		expect(get(visitDividers).t1).toEqual({ divider: null, failed: true });
		expect(get(openAtDivider)).toBeNull();
		expect(error).toHaveBeenCalledWith('[thread updates] read rejected', 't1', 503);
		error.mockRestore();
	});

	it('keeps cards only for the thread whose visit remains open', async () => {
		respond(
			{ cards: [card(1)], has_more: false, divider },
			{ cards: [card(2)], has_more: false, divider },
			{ cards: [card(3)], has_more: false, divider },
		);
		await loadUpdateCards('t1', 0, { divider: true });
		await loadUpdateCards('t2', 0, { divider: true });
		await loadUpdateCards('t3', 0, { divider: true });

		endVisit('t1');
		endVisit('t2');

		expect(Object.keys(get(threadUpdateCards))).toEqual(['t3']);
	});
});
