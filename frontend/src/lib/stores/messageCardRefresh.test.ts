import { get } from 'svelte/store';
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

	it('re-reads only the carded messages when no turn ended', () => {
		expect(rangesToRefresh(rows, 't1', false)).toEqual([{ from: 'a1', to: 'a1' }]);
	});

	it('coalesces the finished turn and earlier carded rows into one range', () => {
		expect(rangesToRefresh(rows, 't1', true)).toEqual([{ from: 'a1', to: 'a4' }]);
	});

	it('leaves other threads and unsent rows alone', () => {
		const mixed = [row('x', 'assistant', { thread_id: 't2', cards: card }), row('o', 'assistant', { cards: card, _optimistic: true })];
		expect(rangesToRefresh(mixed, 't1', true)).toEqual([]);
	});

	it('caps the carded messages it re-reads at the newest few', () => {
		const many = Array.from({ length: 20 }, (_, i) => row(`m${i}`, 'assistant', { cards: card }));
		expect(rangesToRefresh(many, 't1', false)).toEqual([{ from: 'm8', to: 'm19' }]);
	});

	it('sends one request for one nudge with twelve sparse carded rows', async () => {
		vi.useFakeTimers();
		activeThreadId.set('t1');
		token.set('test-token');
		messages.set(Array.from({ length: 50 }, (_, index) => row(`m${index}`, 'assistant', {
			cards: index < 48 && index % 4 === 0 ? card : undefined,
		})));
		const fetch = vi.fn(async () => ({
			ok: true,
			json: async () => ({ messages: Array.from({ length: 12 }, (_, index) => row(`m${index * 4}`, 'assistant', { cards: card })) }),
		}));
		vi.stubGlobal('fetch', fetch);
		watchMessageCards();
		emit('plans_changed', {});
		await vi.advanceTimersByTimeAsync(400);
		expect(fetch).toHaveBeenCalledTimes(1);
	});

	it('falls back only for sparse selected rows and never merges collapsed rows', async () => {
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
			if (from === 'm0' && to === 'm60') {
				return { ok: true, json: async () => ({
					messages: [row('m60', 'assistant', { cards: card }), row('hidden', 'assistant')], has_more: true,
				}) };
			}
			return { ok: true, json: async () => ({ messages: [row(from, 'assistant', { cards: card })], has_more: false }) };
		});
		vi.stubGlobal('fetch', fetch);
		watchMessageCards();
		emit('plans_changed', {});
		await vi.advanceTimersByTimeAsync(400);

		expect(fetch).toHaveBeenCalledTimes(7);
		const fallbacks = fetch.mock.calls.slice(1).map(([input]) => {
			const params = new URL(String(input), 'http://corsia.test').searchParams;
			return [params.get('from_id'), params.get('to_id')];
		});
		expect(fallbacks).toEqual([['m0', 'm0'], ['m10', 'm10'], ['m20', 'm20'], ['m30', 'm30'], ['m40', 'm40'], ['m50', 'm50']]);
		expect(get(messages).some((message) => message.id === 'hidden')).toBe(false);
	});

	it('refreshes a carded row older than the latest fifty loaded rows', async () => {
		vi.useFakeTimers();
		activeThreadId.set('t1');
		token.set('test-token');
		messages.set(Array.from({ length: 70 }, (_, index) => row(`m${index}`, 'assistant', {
			cards: index === 0 ? card : undefined,
		})));
		const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ messages: [row('m0', 'assistant', { cards: card })] }) }));
		vi.stubGlobal('fetch', fetch);
		watchMessageCards();
		emit('plans_changed', {});
		await vi.advanceTimersByTimeAsync(400);

		expect(fetch).toHaveBeenCalledTimes(1);
		const params = new URL(String(fetch.mock.calls[0][0]), 'http://corsia.test').searchParams;
		expect([params.get('from_id'), params.get('to_id')]).toEqual(['m0', 'm0']);
	});
});

afterEach(() => {
	vi.useRealTimers();
	vi.unstubAllGlobals();
	activeThreadId.set(null);
	messages.set([]);
	token.set(null);
});
