import { afterEach, describe, expect, it, vi } from 'vitest';

import { emit } from '$lib/ws/events';
import { token } from './auth';
import { rangesToRefresh, watchMessageCards } from './messageCardRefresh';
import { activeThreadId, messages, type Message } from './threads';

const row = (id: string, role: Message['role'], extra: Partial<Message> = {}): Message => ({
	id, thread_id: 't1', role, content: id, created_at: '2026-10-10T10:00:00', ...extra,
});
const card = [{ kind: 'ask', artifact_id: 'a', title: 'T', type: null, icon: 'file-text', name: null, path: null, available: true, opens: true }] as Message['cards'];

describe('rangesToRefresh', () => {
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
		const fetch = vi.fn(async (input: RequestInfo | URL) => {
			const params = new URL(String(input), 'http://corsia.test').searchParams;
			const from = params.get('from_id')!;
			const to = params.get('to_id')!;
			return { ok: true, json: async () => ({ messages: [row(from, 'assistant', { cards: card })] }) };
		});
		vi.stubGlobal('fetch', fetch);
		watchMessageCards();
		emit('plans_changed', {});
		await vi.advanceTimersByTimeAsync(400);

		expect(fetch).toHaveBeenCalledTimes(7);
		for (const [input] of fetch.mock.calls) {
			const params = new URL(String(input), 'http://corsia.test').searchParams;
			expect(params.get('from_id')).toBe(params.get('to_id'));
		}
	});

	it('uses at most thirteen requests for a completed turn with twelve older cards', async () => {
		vi.useFakeTimers();
		activeThreadId.set('t1');
		token.set('test-token');
		messages.set([
			...Array.from({ length: 12 }, (_, index) => row(`c${index}`, 'assistant', { cards: card })),
			row('u1', 'user'),
			...Array.from({ length: 51 }, (_, index) => row(`a${index}`, 'assistant')),
		]);
		const fetch = vi.fn(async (input: RequestInfo | URL) => {
			const params = new URL(String(input), 'http://corsia.test').searchParams;
			const from = params.get('from_id')!;
			const to = params.get('to_id')!;
			return { ok: true, json: async () => ({ messages: [row(from, 'assistant')] }) };
		});
		vi.stubGlobal('fetch', fetch);
		watchMessageCards();
		emit('turn_complete', {});
		await vi.advanceTimersByTimeAsync(400);

		expect(fetch).toHaveBeenCalledTimes(13);
		const completedTurn = new URL(String(fetch.mock.calls[0][0]), 'http://corsia.test').searchParams;
		expect([completedTurn.get('from_id'), completedTurn.get('to_id')]).toEqual(['a1', 'a50']);
	});
});

afterEach(() => {
	vi.useRealTimers();
	vi.unstubAllGlobals();
	activeThreadId.set(null);
	messages.set([]);
	token.set(null);
});
