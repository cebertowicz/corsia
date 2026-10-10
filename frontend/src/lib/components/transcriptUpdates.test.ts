import { describe, expect, it } from 'vitest';

import type { UpdateCard } from '$lib/api/threadUpdates';
import type { TranscriptEntry } from './transcriptFocusModel';
import { mergeTranscriptUpdates } from './transcriptUpdates';

function message(seq: number | null): TranscriptEntry {
	return {
		kind: 'message', id: `message-${seq}`,
		message: { id: `m${seq}`, role: 'user', content: 'hi', seq },
		showActivity: true, showContent: true,
	};
}

function fold(...seqs: number[]): TranscriptEntry {
	return {
		kind: 'fold', id: `fold-${seqs[0]}`,
		items: seqs.map((seq) => message(seq) as Extract<TranscriptEntry, { kind: 'message' }>),
	};
}

let nextId = 1;
function card(afterSeq: number, overrides: Partial<UpdateCard> = {}): UpdateCard {
	const id = nextId++;
	return {
		id, thread_id: 't', kind: 'done', node_id: `n${id}`, title: `Item ${id}`,
		after_seq: afterSeq, created_at: `2026-09-29T10:00:${String(id).padStart(2, '0')}.000000`,
		node_exists: true, asks_user: true, blocked_asks_user: true, ...overrides,
	};
}

const ids = (entries: TranscriptEntry[]) => entries.map((entry) => entry.id);
const off = { focusMode: false, divider: null };

describe('mergeTranscriptUpdates', () => {
	it('places each card after the last message at or before its position, 0 before the first', () => {
		nextId = 1;
		const merged = mergeTranscriptUpdates(
			[message(1), message(2), message(3)],
			[card(2), card(0), card(3)],
			off,
		);

		expect(ids(merged)).toEqual(['update-2', 'message-1', 'message-2', 'update-1', 'message-3', 'update-3']);
	});

	it('never puts a card inside a fold, and an unsequenced message stays at the live edge', () => {
		nextId = 1;
		const merged = mergeTranscriptUpdates([fold(1, 2, 3), message(null)], [card(2)], off);

		expect(ids(merged)).toEqual(['fold-1', 'update-1', 'message-null']);
	});

	it('shows every card when Focus is off', () => {
		nextId = 1;
		const cards = [card(1), card(1), card(1), card(1), card(1, { node_id: 'n1' })];
		const merged = mergeTranscriptUpdates([message(1)], cards, off);

		expect(merged.filter((entry) => entry.kind === 'update')).toHaveLength(5);
	});

	it('in Focus shows the latest card per item with an earlier count', () => {
		nextId = 1;
		const merged = mergeTranscriptUpdates(
			[message(1)],
			[card(1, { node_id: 'a', kind: 'failed' }), card(1, { node_id: 'b' }), card(1, { node_id: 'a' })],
			{ focusMode: true, divider: null },
		);

		expect(ids(merged)).toEqual(['message-1', 'update-1', 'passive-update-2']);
		expect(merged[2]).toMatchObject({ kind: 'passive', cards: [{ id: 2 }, { id: 3 }] });
	});

	it('in Focus keeps the newest three of a longer run and tucks the rest behind one line', () => {
		nextId = 1;
		const merged = mergeTranscriptUpdates(
			[message(1), message(2)],
			[card(1), card(1), card(1), card(1), card(1), card(2)],
			{ focusMode: true, divider: null },
		);

		expect(ids(merged)).toEqual(['message-1', 'passive-update-1', 'message-2', 'passive-update-6']);
		expect(merged[1]).toMatchObject({ kind: 'passive', cards: [{ id: 1 }, { id: 2 }, { id: 3 }, { id: 4 }, { id: 5 }] });
	});

	it('keeps passive outcomes after one uninterrupted work run in Focus', () => {
		nextId = 1;
		const merged = mergeTranscriptUpdates(
			[fold(1, 2, 3)],
			[card(1), card(2), card(3, { kind: 'closed' })],
			{ focusMode: true, divider: null },
		);

		expect(ids(merged)).toEqual(['fold-1', 'passive-update-1']);
		expect(merged[1]).toMatchObject({ kind: 'passive', cards: [
			{ kind: 'done' }, { kind: 'done' }, { kind: 'closed' },
		] });
	});

	it('keeps the divider inside the block that spans the last visit and marks it new', () => {
		nextId = 1;
		const merged = mergeTranscriptUpdates(
			[message(1), fold(2, 3), message(4)],
			[],
			{ focusMode: true, divider: { since: '2026-09-29T09:00:00.000000', first_seq: 3 } },
		);

		expect(ids(merged)).toEqual(['message-1', 'fold-2', 'message-4']);
		expect(merged.filter((entry) => entry.kind === 'fold').map((entry) => entry.items.map((item) => item.message.seq))).toEqual([[2, 3]]);
		expect(merged[1]).toMatchObject({ kind: 'fold', new: true, divider: { truncated: false, first_seq: 3 } });
	});

	it('carries truncation to the divider rendering entry', () => {
		const merged = mergeTranscriptUpdates(
			[message(1)], [],
			{ focusMode: false, divider: { since: '2026-09-29T09:00:00.000000', first_seq: 1, truncated: true } },
		);

		expect(merged).toContainEqual({ kind: 'divider', id: 'visit-divider', truncated: true, first_seq: 1 });
	});

	it('draws it before a new card that precedes the first new message', () => {
		nextId = 1;
		const old = card(1, { created_at: '2026-09-29T08:00:00.000000' });
		const fresh = card(1, { created_at: '2026-09-29T09:30:00.000000' });
		const merged = mergeTranscriptUpdates(
			[message(1), message(2)],
			[old, fresh],
			{ focusMode: false, divider: { since: '2026-09-29T09:00:00.000000', first_seq: 2 } },
		);

		expect(ids(merged)).toEqual(['message-1', 'update-1', 'visit-divider', 'update-2', 'message-2']);
	});

	it('draws it before a tucked run that holds a new card, and not at all when nothing new is in view', () => {
		nextId = 1;
		const since = '2026-09-29T09:00:00.000000';
		const cards = [
			card(1, { created_at: '2026-09-29T09:30:00.000000' }),
			...[1, 2, 3].map(() => card(1, { created_at: '2026-09-29T09:40:00.000000' })),
		];
		const merged = mergeTranscriptUpdates([message(1)], cards, { focusMode: true, divider: { since, first_seq: null } });
		expect(ids(merged).slice(0, 3)).toEqual(['message-1', 'visit-divider', 'passive-update-1']);

		const nothing = mergeTranscriptUpdates([message(1)], [], { focusMode: true, divider: { since, first_seq: null } });
		expect(ids(nothing)).toEqual(['message-1']);
	});
});
