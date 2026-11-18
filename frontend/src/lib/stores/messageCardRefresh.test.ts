import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import limits from '../../../../backend/api/card_refresh_limits.json';

import { emit } from '$lib/ws/events';
import { token } from './auth';
import { _cancelPendingMessageCardRefreshForTests, MAX_CARDED, TURN_RANGE_LIMIT, rangesToRefresh, watchMessageCards } from './messageCardRefresh';
import { activeThreadId, messages, type Message } from './threads';

const row = (id: string, role: Message['role'], extra: Partial<Message> = {}): Message => ({
	id, thread_id: 't1', role, content: id, created_at: '2026-10-10T10:00:00', ...extra,
});
const card = [{ kind: 'ask', artifact_id: 'a', title: 'T', type: null, icon: 'file-text', name: null, path: null, available: true, opens: true }] as Message['cards'];

describe('rangesToRefresh', () => {
	it('uses the same limits as the server', () => {
		expect(MAX_CARDED).toBe(limits.max_carded);
		expect(TURN_RANGE_LIMIT).toBe(limits.turn_range_limit);
	});

	const rows = [
		row('u1', 'user'), row('a1', 'assistant', { cards: card }), row('a2', 'assistant'),
		row('u2', 'user'), row('a3', 'assistant'), row('a4', 'assistant'),
	];

	it('re-reads carded messages through exact reads when no turn ended', () => {
		expect(rangesToRefresh(rows, 't1', false)).toEqual([{ from: 'a1', to: 'a1' }]);
	});

	it('reads the completed turn as one range and earlier cards exactly', () => {
		expect(rangesToRefresh(rows, 't1', true)).toEqual([
			{ from: 'a3', to: 'a4' }, { from: 'a1', to: 'a1' },
		]);
	});

	it('leaves other threads and unsent rows alone', () => {
		const mixed = [row('x', 'assistant', { thread_id: 't2', cards: card }), row('o', 'assistant', { cards: card, _optimistic: true })];
		expect(rangesToRefresh(mixed, 't1', true)).toEqual([]);
	});

	it('caps card refreshes at twelve exact reads', () => {
		const many = Array.from({ length: 20 }, (_, index) => row(`m${index}`, 'assistant', { cards: card }));
		expect(rangesToRefresh(many, 't1', false)).toEqual(
			Array.from({ length: 12 }, (_, index) => ({ from: `m${index + 8}`, to: `m${index + 8}` })),
		);
	});

	it('caps a completed-turn range at the newest fifty rows', () => {
		const completed = [row('u1', 'user'), ...Array.from({ length: 70 }, (_, index) => row(`a${index}`, 'assistant'))];
		expect(rangesToRefresh(completed, 't1', true)).toEqual([{ from: 'a20', to: 'a69' }]);
	});

	it('keeps sparse Focus-like card reads exact and never merges collapsed rows', async () => {
		vi.useFakeTimers();
		activeThreadId.set('t1');
		token.set('test-token');
		messages.set(Array.from({ length: 70 }, (_, index) => row(`m${index}`, 'assistant', {
			cards: index % 10 === 0 ? card : undefined,
		})));
		const fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			const body = JSON.parse(String(init?.body));
			return { ok: true, json: async () => ({ messages: body.message_ids.map((id: string) => row(id, 'assistant', { cards: card })) }) };
		});
		vi.stubGlobal('fetch', fetch);
		watchMessageCards();
		emit('plans_changed', {});
		await vi.advanceTimersByTimeAsync(400);

		expect(fetch).toHaveBeenCalledTimes(1);
		const body = JSON.parse(String(fetch.mock.calls[0][1]?.body));
		expect(body.message_ids).toEqual(['m0', 'm10', 'm20', 'm30', 'm40', 'm50', 'm60']);
		expect(body.turn_range).toBeNull();
	});

	it('batches a completed turn with twelve older cards in one request', async () => {
		vi.useFakeTimers();
		activeThreadId.set('t1');
		token.set('test-token');
		messages.set([
			...Array.from({ length: 12 }, (_, index) => row(`c${index}`, 'assistant', { cards: card })),
			row('u1', 'user'),
			...Array.from({ length: 51 }, (_, index) => row(`a${index}`, 'assistant')),
		]);
		const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({
			ok: true, json: async () => ({ messages: [] }),
		}));
		vi.stubGlobal('fetch', fetch);
		watchMessageCards();
		emit('turn_complete', {});
		await vi.advanceTimersByTimeAsync(400);

		expect(fetch).toHaveBeenCalledTimes(1);
		const body = JSON.parse(String(fetch.mock.calls[0][1]?.body));
		expect(body.message_ids).toEqual(Array.from({ length: 12 }, (_, index) => `c${index}`));
		expect(body.turn_range).toEqual({ from_id: 'a1', to_id: 'a50' });
	});

	it('runs one trailing refresh when events arrive during a read, retaining turn end', async () => {
		vi.useFakeTimers();
		activeThreadId.set('t1');
		token.set('test-token');
		messages.set([row('c1', 'assistant', { cards: card }), row('u1', 'user'), row('a1', 'assistant')]);
		let finish!: (value: { ok: boolean; json: () => Promise<{ messages: Message[] }> }) => void;
		const first = new Promise<{ ok: boolean; json: () => Promise<{ messages: Message[] }> }>((resolve) => { finish = resolve; });
		const fetch = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => first);
		vi.stubGlobal('fetch', fetch);
		watchMessageCards();
		emit('plans_changed', {});
		await vi.advanceTimersByTimeAsync(400);
		emit('artifacts_changed', {});
		emit('turn_complete', {});
		emit('plans_changed', {});
		expect(fetch).toHaveBeenCalledTimes(1);
		finish({ ok: true, json: async () => ({ messages: [] }) });
		await vi.advanceTimersByTimeAsync(0);
		expect(fetch).toHaveBeenCalledTimes(2);
		const body = JSON.parse(String(fetch.mock.calls[1][1]?.body));
		expect(body.message_ids).toEqual(['c1']);
		expect(body.turn_range).toEqual({ from_id: 'a1', to_id: 'a1' });
	});

	it.each(['not-ok', 'throws'])('allows a later event after a read %s', async (failure) => {
		vi.useFakeTimers();
		vi.spyOn(console, 'error').mockImplementation(() => {});
		activeThreadId.set('t1');
		token.set('test-token');
		messages.set([row('c1', 'assistant', { cards: card })]);
		const fetch = vi.fn()
			.mockImplementationOnce(() => failure === 'throws' ? Promise.reject(new Error('offline')) : Promise.resolve({ ok: false }))
			.mockResolvedValue({ ok: true, json: async () => ({ messages: [] }) });
		vi.stubGlobal('fetch', fetch);
		watchMessageCards();
		emit('plans_changed', {});
		await vi.advanceTimersByTimeAsync(400);
		expect(fetch).toHaveBeenCalledTimes(1);
		emit('artifacts_changed', {});
		await vi.advanceTimersByTimeAsync(400);
		expect(fetch).toHaveBeenCalledTimes(2);
	});

	it('runs one trailing read after an in-flight read fails', async () => {
		vi.useFakeTimers();
		vi.spyOn(console, 'error').mockImplementation(() => {});
		activeThreadId.set('t1');
		token.set('test-token');
		messages.set([row('c1', 'assistant', { cards: card })]);
		let reject!: (reason: Error) => void;
		const first = new Promise((_resolve, fail) => { reject = fail; });
		const fetch = vi.fn().mockReturnValueOnce(first).mockResolvedValue({ ok: true, json: async () => ({ messages: [] }) });
		vi.stubGlobal('fetch', fetch);
		watchMessageCards();
		emit('plans_changed', {});
		await vi.advanceTimersByTimeAsync(400);
		emit('artifacts_changed', {});
		emit('plans_changed', {});
		expect(fetch).toHaveBeenCalledTimes(1);
		reject(new Error('offline'));
		await vi.advanceTimersByTimeAsync(0);
		expect(fetch).toHaveBeenCalledTimes(2);
	});

	it('drops an old thread response and refreshes the newly active thread', async () => {
		vi.useFakeTimers();
		activeThreadId.set('t1');
		token.set('test-token');
		messages.set([row('c1', 'assistant', { cards: card })]);
		let finish!: (response: unknown) => void;
		const first = new Promise((resolve) => { finish = resolve; });
		const fetch = vi.fn().mockReturnValueOnce(first).mockResolvedValue({ ok: true, json: async () => ({ messages: [] }) });
		vi.stubGlobal('fetch', fetch);
		watchMessageCards();
		emit('plans_changed', {});
		await vi.advanceTimersByTimeAsync(400);
		activeThreadId.set('t2');
		messages.set([row('c2', 'assistant', { thread_id: 't2', cards: card })]);
		emit('artifacts_changed', {});
		finish({ ok: true, json: async () => ({ messages: [row('c1', 'assistant', { content: 'stale', cards: card })] }) });
		await vi.advanceTimersByTimeAsync(0);
		expect(fetch).toHaveBeenCalledTimes(2);
		expect(String(fetch.mock.calls[1][0])).toContain('/threads/t2/messages/refresh');
		expect(get(messages).map((message) => message.id)).toEqual(['c2']);
	});

	it('does not read when an unrelated event finds no carded or turn rows', async () => {
		vi.useFakeTimers();
		activeThreadId.set('t1');
		token.set('test-token');
		messages.set([row('u1', 'user')]);
		const fetch = vi.fn();
		vi.stubGlobal('fetch', fetch);
		watchMessageCards();
		emit('artifacts_changed', { artifact_id: 'unrelated' });
		await vi.advanceTimersByTimeAsync(400);
		expect(fetch).not.toHaveBeenCalled();
	});
});

afterEach(() => {
	_cancelPendingMessageCardRefreshForTests();
	vi.useRealTimers();
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
	activeThreadId.set(null);
	messages.set([]);
	token.set(null);
});
