import { describe, expect, it } from 'vitest';

import type { Message } from './threads';
import { rangesToRefresh } from './messageCardRefresh';

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

	it('also re-reads the finished turn, from its first step to the newest row', () => {
		expect(rangesToRefresh(rows, 't1', true)).toEqual([
			{ from: 'a3', to: 'a4' }, { from: 'a1', to: 'a1' },
		]);
	});

	it('leaves other threads and unsent rows alone', () => {
		const mixed = [row('x', 'assistant', { thread_id: 't2', cards: card }), row('o', 'assistant', { cards: card, _optimistic: true })];
		expect(rangesToRefresh(mixed, 't1', true)).toEqual([]);
	});

	it('caps the carded messages it re-reads at the newest few', () => {
		const many = Array.from({ length: 20 }, (_, i) => row(`m${i}`, 'assistant', { cards: card }));
		const ranges = rangesToRefresh(many, 't1', false);
		expect(ranges).toHaveLength(12);
		expect(ranges.at(-1)).toEqual({ from: 'm19', to: 'm19' });
	});
});
