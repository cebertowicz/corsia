import { describe, expect, it } from 'vitest';

import type { UpdateCard } from '$lib/api/threadUpdates';
import { THREAD, card, human, present, runSeqs, said, work } from '$lib/__tests__/fixtures/focusRuns';
import { collapseToolOnlyAssistantSteps, type TranscriptEntry, type TranscriptMessage } from './messageListModel';
import { isRunExpanded, runTokens, toggleRun } from './transcriptFocusModel';

const runIndexes = (entries: TranscriptEntry[]) => entries.flatMap((entry, index) => (entry.kind === 'fold' ? [index] : []));
const expandedRuns = (entries: TranscriptEntry[], open: Set<string>, historyComplete = true) => (
	runIndexes(entries).map((index) => isRunExpanded(runTokens(entries, index, historyComplete), open))
);

describe('run expansion', () => {
	it('opens runs that share a Turn separately', () => {
		const entries = present([
			human('ask', 1, '10:00:00', 't1', '10:00:00'),
			work('a', 2, '10:01:00'),
			said('update', 3, '10:04:30'),
			work('b', 4, '10:06:00'),
			said('reply', 5, '10:10:00'),
		]);
		const [first, second] = runIndexes(entries);

		const open = toggleRun(runTokens(entries, first, true), new Set());
		expect(expandedRuns(entries, open)).toEqual([true, false]);
		const both = toggleRun(runTokens(entries, second, true), open);
		expect(expandedRuns(entries, both)).toEqual([true, true]);
		expect(expandedRuns(entries, toggleRun(runTokens(entries, first, true), both))).toEqual([false, true]);
	});

	it('W8: a run opened at the page top stays open when it closes and when the older page arrives', () => {
		const rows = Array.from({ length: 60 }, (_, index) => work(`w${index + 1}`, index + 1, '10:00:00'));
		const loaded = rows.slice(10);
		const live = present(loaded, { live: { turnId: 't1' } });

		const tokens = runTokens(live, 0, false);
		expect(tokens).toHaveLength(50);
		expect(tokens.every((token) => token.startsWith('row:'))).toBe(true);
		const open = toggleRun(tokens, new Set());

		const closed = present([...loaded, said('reply', 61, '10:10:00')]);
		expect(expandedRuns(closed, open, false)).toEqual([true]);
		const paged = present([...rows, said('reply', 61, '10:10:00')]);
		expect(runSeqs(paged)[0]).toHaveLength(60);
		expect(expandedRuns(paged, open, true)).toEqual([true]);
	});

	it('W9: a run opened before it holds a row stays open through delivery, its rows and its reply', () => {
		const optimistic = { id: 'tmp-1', thread_id: THREAD, role: 'user', content: 'go', _optimistic: true, metadata: { input_id: 'X' } };
		const delivered = { ...human('m-9', 9, '10:00:00', 't1', '10:00:00'), metadata: { input_id: 'X' } };
		const empty = present([optimistic], { live: { turnId: 't1' } });

		const tokens = runTokens(empty, 1, true);
		expect(tokens).toEqual(['after:X']);
		const open = toggleRun(tokens, new Set());

		const taken = present([delivered], { live: { turnId: 't1' } });
		expect(expandedRuns(taken, open)).toEqual([true]);
		const working = present([delivered, work('a', 10, '10:01:00')], { live: { turnId: 't1' } });
		expect(expandedRuns(working, open)).toEqual([true]);
		const replied = present([delivered, work('a', 10, '10:01:00'), said('reply', 11, '10:02:00')]);
		expect(expandedRuns(replied, open)).toEqual([true]);
	});

	it('marks an empty open run in a thread with no rows as the start of history', () => {
		const empty = present([], { live: { turnId: 't1' } });

		expect(runTokens(empty, 0, true)).toEqual(['after:start']);
		const open = toggleRun(runTokens(empty, 0, true), new Set());
		expect(expandedRuns(present([work('a', 1, '10:00:00')], { live: { turnId: 't1' } }), open)).toEqual([true]);
	});

	it('never records an unsaved row as a token', () => {
		const streaming = { id: 'local-stream', thread_id: THREAD, role: 'assistant', content: '', _optimistic: true, metadata: { turn_id: 't1' } };
		const entries = present([human('ask', 1, '10:00:00', 't1', '10:00:00'), streaming], { live: { turnId: 't1' } });

		expect(runTokens(entries, 1, true)).toEqual(['after:ask']);
	});

	it('keeps a run open when the page edge falls inside a merged group of tool-only steps', () => {
		const step = (id: string, seq: number): TranscriptMessage => ({
			id, thread_id: THREAD, role: 'assistant', content: '', seq, activity_weight: 1,
			metadata: { turn_id: 't1', tool_calls: [{ id: `call-${id}` }] },
		});
		const steps = [step('s1', 1), step('s2', 2), step('s3', 3)];
		const clipped = present(collapseToolOnlyAssistantSteps(steps.slice(1)));
		const open = toggleRun(runTokens(clipped, 0, false), new Set());
		expect(open).toEqual(new Set(['row:s2', 'row:s3']));

		const paged = present(collapseToolOnlyAssistantSteps(steps));
		const [merged] = paged;
		expect(merged.kind === 'fold' && merged.items[0].message).toMatchObject({ mergedIds: ['s1', 's2', 's3'], activity_weight: 3 });
		expect(expandedRuns(paged, open, true)).toEqual([true]);
	});

	it('keeps both parts open when a card lands inside an open run, and leaves later work closed', () => {
		const rows = [10, 11, 12, 13, 14].map((seq) => work(`w${seq}`, seq, '10:00:00'));
		const whole = present(rows, { live: { turnId: 't1' } });
		const open = toggleRun(runTokens(whole, 0, true), new Set());

		const split = present(rows, { live: { turnId: 't1' }, cards: [card(1, 12)] });
		expect(runSeqs(split)).toEqual([[10, 11, 12], [13, 14]]);
		expect(expandedRuns(split, open)).toEqual([true, true]);

		// New work after a second card is a run the user never opened.
		const later = present([...rows, work('w15', 15, '10:01:00')], { live: { turnId: 't1' }, cards: [card(1, 12), card(2, 14)] });
		expect(runSeqs(later)).toEqual([[10, 11, 12], [13, 14], [15]]);
		expect(expandedRuns(later, open)).toEqual([true, true, false]);
	});
});

describe('runs and grouped cards', () => {
	const rows = () => [
		human('ask', 9, '10:00:00', 't1', '10:00:00'),
		...Array.from({ length: 11 }, (_, index) => work(`w${index + 10}`, index + 10, '10:01:00')),
		said('reply', 21, '10:10:00'),
	];

	it('W14: only the cards left after grouping split the run', () => {
		const entries = present(rows(), { cards: [card(1, 12, 'A'), card(2, 15, 'B'), card(3, 17, 'A')] });

		expect(entries.map((entry) => entry.kind))
			.toEqual(['message', 'fold', 'update', 'fold', 'update', 'fold', 'message']);
		expect(runSeqs(entries)).toEqual([[10, 11, 12, 13, 14, 15], [16, 17], [18, 19, 20]]);
		expect(entries.flatMap((entry) => (entry.kind === 'update' ? [[entry.card.node_id, entry.earlier]] : [])))
			.toEqual([['B', 0], ['A', 1]]);
	});

	it('W15: no card is tucked, so each shown card splits the run at its own place', () => {
		const cards = [11, 13, 15, 17, 19].map((after, index) => card(index + 1, after));
		const entries = present(rows(), { cards });

		expect(entries.map((entry) => entry.kind)).toEqual([
			'message', 'fold', 'update', 'fold', 'update', 'fold', 'update', 'fold', 'update', 'fold', 'update', 'fold', 'message',
		]);
		expect(runSeqs(entries)).toEqual([[10, 11], [12, 13], [14, 15], [16, 17], [18, 19], [20]]);
	});

	it('keeps cards without an item apart: each closes the run at its own place', () => {
		const entries = present(rows(), { cards: [card(1, 12, null), card(2, 15, null)] });

		expect(runSeqs(entries)).toEqual([[10, 11, 12], [13, 14, 15], [16, 17, 18, 19, 20]]);
	});
});

/** A small seeded generator, so a failing sequence can be replayed from its seed. */
function seeded(seed: number): () => number {
	let state = seed >>> 0;
	return () => {
		state = (state + 0x6D2B79F5) >>> 0;
		let mixed = Math.imul(state ^ (state >>> 15), 1 | state);
		mixed = (mixed + Math.imul(mixed ^ (mixed >>> 7), 61 | mixed)) ^ mixed;
		return ((mixed ^ (mixed >>> 14)) >>> 0) / 4294967296;
	};
}

type World = {
	older: TranscriptMessage[];
	loaded: TranscriptMessage[];
	cards: UpdateCard[];
	live: boolean;
	open: Set<string>;
	watched: Set<string>;
	nextSeq: number;
	nextCard: number;
	pending: number | null;
};

function view(world: World): { entries: TranscriptEntry[]; historyComplete: boolean } {
	return {
		entries: present(world.loaded, { live: world.live ? { turnId: 'live' } : null, cards: world.cards }),
		historyComplete: world.older.length === 0,
	};
}

/** One random event: append, prepend, close, card insert, id canonicalization, or a click that expands a run. */
function step(world: World, random: () => number): void {
	const pick = Math.floor(random() * 7);
	const seq = () => world.nextSeq++;
	if (world.pending !== null && pick !== 5) {
		// A sent message is delivered before anything else is stored: same `input_id`, canonical id.
		const index = world.loaded.findIndex((row) => row.metadata?.input_id === `in-${world.pending}`);
		const at = seq();
		world.loaded[index] = { ...human(`m-${at}`, at, '10:00:00', 'live', '10:00:00'), metadata: { input_id: `in-${world.pending}` } };
		world.pending = null;
		world.live = true;
		return;
	}
	if (pick === 0 || pick === 6) {
		const at = seq();
		world.loaded.push(work(`w-${at}`, at, '10:00:00', `turn-${Math.floor(random() * 3)}`));
	} else if (pick === 1) {
		const at = seq();
		world.loaded.push(said(`s-${at}`, at, '10:00:00'));
		world.live = random() < 0.5;
	} else if (pick === 2) {
		world.loaded.unshift(...world.older.splice(-Math.ceil(random() * 4)));
	} else if (pick === 3) {
		const seqs = world.loaded.flatMap((row) => (typeof row.seq === 'number' ? [row.seq] : []));
		if (seqs.length === 0) return;
		const node = random() < 0.2 ? null : `node-${Math.floor(random() * 3)}`;
		world.cards.push(card(world.nextCard++, seqs[Math.floor(random() * seqs.length)], node));
	} else if (pick === 4) {
		world.pending = world.nextSeq;
		world.loaded.push({
			id: `tmp-${world.pending}`, thread_id: THREAD, role: 'user', content: 'go',
			_optimistic: true, metadata: { input_id: `in-${world.pending}` },
		});
	} else {
		const { entries, historyComplete } = view(world);
		const closed = runIndexes(entries).filter((index) => !isRunExpanded(runTokens(entries, index, historyComplete), world.open));
		if (closed.length === 0) return;
		const index = closed[Math.floor(random() * closed.length)];
		world.open = toggleRun(runTokens(entries, index, historyComplete), world.open);
		// Watch the rows the clicked run holds, not the marks the model chose for it.
		const run = entries[index];
		if (run.kind === 'fold') for (const item of run.items) world.watched.add(item.message.id);
	}
}

/** What the transcript breaks after an event, in words; empty when every rule holds. */
function broken(world: World): string[] {
	const { entries, historyComplete } = view(world);
	const found: string[] = [];
	const held = new Map<string, number>();
	entries.forEach((entry, index) => {
		if (entry.kind !== 'fold') return;
		if (entries[index - 1]?.kind === 'fold') found.push(`two runs adjacent at entry ${index}`);
		for (const item of entry.items) held.set(item.message.id, index);
	});
	const hidden = world.loaded.filter((row) => row.role === 'assistant' && row.metadata?.for_user !== true).map((row) => row.id);
	if ([...held.keys()].join() !== hidden.join()) found.push(`runs hold [${[...held.keys()]}] but the hidden rows are [${hidden}]`);
	for (const id of world.watched) {
		const index = held.get(id);
		if (index === undefined) found.push(`row ${id} left its run`);
		else if (!isRunExpanded(runTokens(entries, index, historyComplete), world.open)) found.push(`row ${id} is shown closed`);
	}
	return found;
}

describe('run expansion under any sequence of transcript events', () => {
	it('keeps every row of an opened run shown open, and never leaves two runs adjacent or a row lost', () => {
		// Plain checks with one assertion: an `expect` per row per step made this the suite's slowest test.
		const failures: string[] = [];
		for (let seed = 1; seed <= 400; seed += 1) {
			const random = seeded(seed);
			const history = Array.from({ length: 12 }, (_, index) => (
				random() < 0.25 ? said(`s-${index + 1}`, index + 1, '09:00:00') : work(`w-${index + 1}`, index + 1, '09:00:00', 'turn-0')
			));
			const world: World = {
				older: history.slice(0, 8), loaded: history.slice(8), cards: [], live: random() < 0.5,
				open: new Set(), watched: new Set(), nextSeq: 13, nextCard: 1, pending: null,
			};
			for (let turn = 0; turn < 40; turn += 1) {
				step(world, random);
				const found = broken(world);
				if (found.length === 0) continue;
				// The first broken step of a seed is the one to replay; later steps only repeat it.
				failures.push(`seed ${seed}, step ${turn}: ${found.join('; ')}`);
				break;
			}
		}
		expect(failures).toEqual([]);
	});
});
