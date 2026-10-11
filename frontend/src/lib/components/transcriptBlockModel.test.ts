import { describe, expect, it } from 'vitest';

import { card, DAY, human, present, said, work } from '$lib/__tests__/fixtures/focusRuns';
import { combineStubs, type RunStub } from '$lib/focusRuns';
import { blockEntries } from './transcriptBlockEntries';
import { buildTranscriptPresentation, type TranscriptMessage } from './messageListModel';
import { countVisibleEntries, type TranscriptEntry, type TranscriptFoldEntry } from './transcriptFocusModel';
import { formatDuration, liveLabel, NEW_SUFFIX, runLabels, runToggleName, runWeight } from './transcriptRunLabel';
import { mergeTranscriptUpdates } from './transcriptUpdates';

const row = (id: string, seq: number, metadata: Record<string, unknown>, role = 'system', over: Record<string, unknown> = {}) => ({
	id, thread_id: 'thread-1', role, content: 'x', seq, created_at: `${DAY}10:00:00`, activity_weight: 0, metadata, ...over,
}) as unknown as TranscriptMessage;
const stub = (over: Partial<RunStub> = {}): RunStub => ({
	firstId: 'a', lastId: 'b', firstSeq: 1, lastSeq: 8, count: 8, turnIds: ['t1'], weight: 8,
	kinds: ['steps'], continuesOlder: false, ...over,
});
const folds = (entries: TranscriptEntry[]) => entries.filter((e): e is TranscriptFoldEntry => e.kind === 'fold');
const faces = (rows: TranscriptMessage[]) => {
	const entries = present([...rows, human('end', 99, '11:00:00', null, null)]);
	const labels = runLabels(entries, {}, true);
	return folds(entries).map((run) => labels.get(run.id));
};
const blocked = (id: number, after: number, over: object = {}) => ({ ...card(id, after), kind: 'blocked', ...over }) as ReturnType<typeof card>;

describe('block labels without steps (A14, A15)', () => {
	it('names a block by the one kind of row it holds', () => {
		expect(faces([row('c', 1, { type: 'context_event' })])).toEqual(['Context updated']);
		expect(faces([row('m', 1, { type: 'model_switch' })])).toEqual(['Model switched']);
		expect(faces([row('s', 1, { type: 'subagent_done' })])).toEqual(['Subagent finished']);
		expect(faces([row('a', 1, { type: 'account_switch' })])).toEqual(['Account moved']);
	});

	it('names two kinds, and counts events beyond that', () => {
		expect(faces([row('c', 1, { type: 'context_event' }), row('m', 2, { type: 'model_switch' })])).toEqual(['Context updated · Model switched']);
		expect(faces([
			row('c', 1, { type: 'context_event' }), row('m', 2, { type: 'model_switch' }), row('s', 3, { type: 'subagent_done' }),
		])).toEqual(['3 events']);
	});

	it('leaves steps in charge of the label once the block has any', () => {
		expect(faces([row('c', 1, { type: 'context_event' }), work('w', 2, '10:01:00')])[0]).toMatch(/^Worked · 1 step/);
	});
});

describe('block label forms (A7, A8)', () => {
	it('formats active time as minutes, hours and minutes, or whole hours', () => {
		expect(formatDuration(59_000)).toBe('under 1 min');
		expect(formatDuration(5 * 60_000)).toBe('5 min');
		expect(formatDuration(65 * 60_000)).toBe('1 h 5 min');
		expect(formatDuration(120 * 60_000)).toBe('2 h');
	});

	it('joins the live label from what is known and drops the rest', () => {
		expect(liveLabel('3 min', 'Reading files')).toBe('Working · 3 min · Reading files');
		expect(liveLabel(null, null)).toBe('Working');
	});

	it('names the toggle by its verb, and says when the block is new (F2)', () => {
		const run = { ...folds(present([work('w', 1, '10:00:00')]))[0], new: true };
		expect(runToggleName(run, `Worked · 1 step${NEW_SUFFIX}`, false)).toBe('Expand Worked · 1 step, new since your last visit');
		expect(runToggleName({ ...run, new: false }, 'Worked · 1 step', true)).toBe('Collapse Worked · 1 step');
	});
});

describe('stub kinds and continuation', () => {
	it('reads N+ steps while the block continues older than the loaded page', () => {
		const entries = buildTranscriptPresentation([], true, null, undefined, [stub({ continuesOlder: true })]);
		expect([...runLabels(entries, {}, false).values()]).toEqual(['Worked · 8+ steps']);
		const whole = buildTranscriptPresentation([], true, null, undefined, [stub()]);
		expect([...runLabels(whole, {}, false).values()][0]).toBe('Worked · 8 steps');
	});

	it('names a stub that holds only bookkeeping kinds by its kind', () => {
		const entries = buildTranscriptPresentation([], true, null, undefined, [stub({ kinds: ['context_update'], weight: 0 })]);
		expect([...runLabels(entries, {}, true).values()]).toEqual(['Context updated']);
	});

	it('joins stubs into one block: the oldest piece decides continuation, kinds union sorted', () => {
		const joined = combineStubs([
			stub({ firstSeq: 5, lastSeq: 8, continuesOlder: true, kinds: ['steps'] }),
			stub({ firstSeq: 1, lastSeq: 4, continuesOlder: false, kinds: ['context_update'] }),
		]);
		expect(joined.continuesOlder).toBe(false);
		expect(joined.kinds).toEqual(['context_update', 'steps']);
		expect(combineStubs([
			stub({ firstSeq: 1, lastSeq: 4, continuesOlder: true }), stub({ firstSeq: 5, lastSeq: 8, continuesOlder: false }),
		]).continuesOlder).toBe(true);
	});

	it('counts a block as a shown entry, but not a row Focus omits entirely', () => {
		expect(countVisibleEntries(present([row('c', 1, { type: 'context_event' })]))).toBe(1);
		expect(countVisibleEntries(present([row('r', 1, { type: 'activity_receipt', event_type: 'usage.recording_failed' })]))).toBe(0);
	});
});

describe('plan cards in a block (B6, B22)', () => {
	const rows = [10, 11, 12].map((seq) => work(`w${seq}`, seq, '10:00:00'));
	const merge = (cards: ReturnType<typeof card>[]) => mergeTranscriptUpdates(
		buildTranscriptPresentation(rows, true), cards, { focusMode: true, divider: null },
	);

	it('puts a blocked card that asks nothing of you inside the block, as a line', () => {
		const entries = merge([blocked(1, 11, { blocked_asks_user: false })]);
		expect(entries.map((e) => e.kind)).toEqual(['fold']);
		const lines = blockEntries(folds(entries)[0]).filter((e) => e.kind === 'line');
		expect(lines).toMatchObject([{ line: { type: 'plan_card', outcome: 'Blocked' } }]);
	});

	it('tucks a blocked card that no longer asks you into one title-and-state line, with no ask card', () => {
		const ask = { kind: 'ask', artifact_id: 'a1', title: 'Launch plan', hold_id: 'h1', state: 'answered' };
		const entries = merge([blocked(1, 11, { title: 'Ship the export', blocked_asks_user: false, ask })]);
		const lines = blockEntries(folds(entries)[0]).filter((e) => e.kind === 'line');
		expect(lines).toMatchObject([{ line: { type: 'plan_card', text: 'Ship the export · Blocked' } }]);
		expect(entries.map((e) => e.kind)).not.toContain('update');
	});

	it('keeps a blocked card that asks you as its own card', () => {
		expect(merge([blocked(1, 11, { blocked_asks_user: true })]).map((e) => e.kind)).toContain('update');
	});
});

describe('block entries (B17, F2, F3)', () => {
	it('orders rows, card lines and the visit divider by position', () => {
		const rows = [1, 2, 3, 4].map((seq) => work(`w${seq}`, seq, '10:00:00'));
		const entries = mergeTranscriptUpdates(
			buildTranscriptPresentation(rows, true), [blocked(1, 2, { blocked_asks_user: false })],
			{ focusMode: true, divider: { since: `${DAY}09:00:00Z`, first_seq: 3 } },
		);
		const run = folds(entries)[0];
		expect(run.new).toBe(true);
		expect(blockEntries(run).map((e) => e.kind)).toEqual(['step', 'step', 'line', 'divider', 'step', 'step']);
	});

	it('leaves the divider off a loaded first row of a block whose start is unread', () => {
		const run = folds(mergeTranscriptUpdates(
			buildTranscriptPresentation([work('w5', 5, '10:00:00')], true, null, undefined, [stub({ lastSeq: 4 })]),
			[], { focusMode: true, divider: { since: `${DAY}09:00:00Z`, first_seq: 5 } },
		))[0];
		expect(blockEntries(run, { complete: false }).map((e) => e.kind)).toEqual(['step']);
		expect(blockEntries(run, { complete: true }).map((e) => e.kind)).toEqual(['divider', 'step']);
	});

	it('turns an account move into one line (B17)', () => {
		const move = row('m', 1, { type: 'account_switch', reason: 'account_limit' });
		const [entry] = blockEntries(folds(present([move]))[0]);
		expect(entry).toMatchObject({ kind: 'line', line: { type: 'account_move' } });
	});
});

describe('weights stay the server count (A1)', () => {
	it('sums a block row weights and ignores bookkeeping rows', () => {
		const run = folds(present([work('a', 1, '10:00:00'), row('c', 2, { type: 'context_event' }), said('r', 3, '10:05:00')]))[0];
		expect(runWeight(run)).toBe(1);
	});
});
