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
		const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ messages: [] }) }));
		vi.stubGlobal('fetch', fetch);
		watchMessageCards();
		emit('plans_changed', {});
		await vi.advanceTimersByTimeAsync(400);
		expect(fetch).toHaveBeenCalledTimes(1);
	});
});

afterEach(() => {
	vi.useRealTimers();
	vi.unstubAllGlobals();
	activeThreadId.set(null);
	messages.set([]);
	token.set(null);
});
