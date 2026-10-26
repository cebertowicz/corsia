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

	it('Focus off: each step keeps its own line', () => {
		nextId = 1;
		const merged = updates(mergeTranscriptUpdates([message(1), message(2)], [step(1), step(1), step(2)], off));
		expect(merged).toHaveLength(3);
		expect(merged.every((entry) => entry.group === undefined)).toBe(true);
	});
});
