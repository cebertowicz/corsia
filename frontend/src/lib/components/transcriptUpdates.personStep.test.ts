import { describe, expect, it } from 'vitest';
import type { UpdateCard } from '$lib/api/threadUpdates';
import type { TranscriptEntry, TranscriptUpdateEntry } from './transcriptFocusModel';
import { asksYou, isBlockLine, isReadyStep } from './transcriptPassive';
import { mergeTranscriptUpdates } from './transcriptUpdates';

const message = (seq: number): TranscriptEntry => ({
	kind: 'message', id: `message-${seq}`,
	message: { id: `m${seq}`, role: 'assistant', content: 'hi', seq },
	showActivity: true, showContent: true,
});
const work = (...seqs: number[]): TranscriptEntry => ({
	kind: 'fold', id: `fold-${seqs[0]}`, items: seqs.map((seq) => message(seq) as Extract<TranscriptEntry, { kind: 'message' }>),
} as never);

let nextId = 1;
const step = (afterSeq: number, over: Partial<UpdateCard> = {}): UpdateCard => {
	const id = nextId++;
	return {
		id, thread_id: 't', kind: 'person_step', node_id: `n${id}`, title: `Step ${id}`, after_seq: afterSeq,
		created_at: `2026-10-26T10:00:${String(id).padStart(2, '0')}.000000`, node_exists: true,
		asks_user: true, blocked_asks_user: false, step_state: 'open', ...over,
	};
};
const on = { focusMode: true, divider: null };
const off = { focusMode: false, divider: null };
const updates = (entries: TranscriptEntry[]) => entries.filter((entry): entry is TranscriptUpdateEntry => entry.kind === 'update');

describe('a person step in the transcript', () => {
	it('stands on its own in every state and never becomes a line inside the work block', () => {
		nextId = 1;
		const open = step(1);
		const done = step(1, { step_state: 'done' });
		expect(asksYou(open)).toBe(true);
		expect(asksYou(done)).toBe(true);
		expect(isBlockLine(open)).toBe(false);
		expect(isReadyStep(open)).toBe(true);
		expect(isReadyStep(done)).toBe(false);
		expect(isReadyStep({ ...open, kind: 'blocked' })).toBe(false);
	});

	it('Focus: breaks the work block it was raised in, like a raised decision', () => {
		nextId = 1;
		const merged = mergeTranscriptUpdates([work(1, 2, 3, 4)], [step(2)], on);
		expect(merged.map((entry) => entry.kind)).toEqual(['fold', 'update', 'fold']);
		expect(updates(merged)[0].card.kind).toBe('person_step');
	});

	it('Focus: several waiting tasks in one stretch read as one line, at the latest of them', () => {
		nextId = 1;
		const a = step(2);
		const b = step(2);
		const c = step(2);
		const merged = updates(mergeTranscriptUpdates([work(1, 2, 3, 4)], [a, b, c], on));
		expect(merged).toHaveLength(1);
		expect(merged[0].card.id).toBe(c.id);
		expect(merged[0].group?.map((card) => card.id)).toEqual([a.id, b.id, c.id]);
	});

	it('Focus: a settled step keeps its own line, so its receipt shows where it was raised', () => {
		nextId = 1;
		const a = step(2);
		const done = step(2, { step_state: 'done' });
		const b = step(2);
		const merged = updates(mergeTranscriptUpdates([work(1, 2, 3, 4)], [a, done, b], on));
		expect(merged.map((entry) => entry.group?.length ?? 1)).toEqual([1, 2]);
		expect(merged.map((entry) => entry.card.id)).toEqual([done.id, b.id]);
	});

	it('Focus: one ready step is not a group', () => {
		nextId = 1;
		const merged = updates(mergeTranscriptUpdates([work(1, 2, 3, 4)], [step(2)], on));
		expect(merged[0].group).toBeUndefined();
	});

	it.each([['done', 'done'], ['closed', 'closed']] as const)(
		'Focus: a displayed %s receipt replaces its %s outcome, leaving other outcomes', (state, kind) => {
			nextId = 1;
			const receipt = step(1, { node_id: 'person', step_state: state });
			const outcomes = ['person', 'other1', 'other2'].map((node_id) => step(2, { node_id, kind }));
			const merged = mergeTranscriptUpdates([work(1, 2, 3)], [receipt, ...outcomes], on);
			const passive = merged.filter((entry) => entry.kind === 'passive');
			expect(passive.flatMap((entry) => entry.cards.map((card) => card.node_id))).toEqual(['other1', 'other2']);
			expect(updates(merged).some((entry) => entry.card.id === receipt.id)).toBe(true);
		},
	);

	it.each([['done', 'done'], ['done', 'done_follow_up'], ['closed', 'closed']] as const)(
		'Focus: no passive row remains when its only %s receipt replaces %s', (state, kind) => {
		nextId = 1;
		const receipt = step(1, { node_id: 'person', step_state: state });
		const outcome = step(2, { node_id: 'person', kind });
		const merged = mergeTranscriptUpdates([work(1, 2, 3)], [receipt, outcome], on);
		expect(merged.some((entry) => entry.kind === 'passive')).toBe(false);
	});

	it('keeps outcomes when the receipt is outside the card window or grouped away', () => {
		nextId = 1;
		const outcome = step(2, { node_id: 'person', kind: 'done' });
		expect(mergeTranscriptUpdates([work(1, 2, 3)], [outcome], on).some((entry) => entry.kind === 'passive')).toBe(true);
		const receipt = step(1, { node_id: 'person', step_state: 'done' });
		const laterAsk = step(1, { node_id: 'person', kind: 'blocked', blocked_asks_user: true });
		expect(mergeTranscriptUpdates([work(1, 2, 3)], [receipt, laterAsk, outcome], on).some((entry) => entry.kind === 'passive')).toBe(true);
	});

	it('Focus off: each step and its outcome keeps its own line', () => {
		nextId = 1;
		const receipt = step(1, { node_id: 'person', step_state: 'done' });
		const outcome = step(2, { node_id: 'person', kind: 'done' });
		const another = step(1);
		const merged = updates(mergeTranscriptUpdates([message(1), message(2)], [receipt, another, outcome], off));
		expect(merged.map((entry) => entry.card.id)).toEqual([receipt.id, another.id, outcome.id]);
		expect(merged.every((entry) => entry.group === undefined)).toBe(true);
	});
});
