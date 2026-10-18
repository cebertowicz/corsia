import { describe, expect, it } from 'vitest';

import parity from '../../../../testdata/focus-visibility-parity.json';
import { card, human, said, turn, work } from '$lib/__tests__/fixtures/focusRuns';
import { combineStubs, type RunStub } from '$lib/focusRuns';
import {
	buildTranscriptPresentation, type TranscriptEntry, type TranscriptMessage,
} from './messageListModel';
import { isFocusVisible, isRunExpanded, rowKind, runTokens, toggleRun } from './transcriptFocusModel';
import { runLabels, runWeight } from './transcriptRunLabel';
import { mergeTranscriptUpdates } from './transcriptUpdates';

const stub = (firstSeq: number, lastSeq: number, over: Partial<RunStub> = {}): RunStub => ({
	firstId: `m${firstSeq}`, lastId: `m${lastSeq}`, firstSeq, lastSeq,
	count: lastSeq - firstSeq + 1, turnIds: ['t1'], kinds: ['step'], continuesOlder: false, weight: lastSeq - firstSeq + 1, ...over,
});
const build = (rows: TranscriptMessage[], stubs: RunStub[], live: { turnId: string | null } | null = null) => (
	buildTranscriptPresentation(rows, true, live, undefined, stubs)
);
const folds = (entries: TranscriptEntry[]) => entries.flatMap((entry) => (entry.kind === 'fold' ? [entry] : []));

describe('the Focus visibility rule, shared with the backend', () => {
	it.each(parity.cases)('$name', (fixture) => {
		let metadata: Record<string, unknown> = {};
		try {
			const parsed = JSON.parse(fixture.metadata_raw ?? 'null');
			if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) metadata = parsed;
		} catch { /* the REST projection serves unparseable metadata as an empty object */ }
		const row = {
			id: 'row', thread_id: 'thread-1', role: fixture.role, content: fixture.content, seq: 5,
			created_at: '2026-10-02T10:00:00', metadata,
			...(typeof fixture.activity_weight === 'number' ? { activity_weight: fixture.activity_weight } : {}),
		} as unknown as TranscriptMessage;

		expect(isFocusVisible(row)).toBe(fixture.visible);
		expect(rowKind(row)).toBe(fixture.expected_kind === 'steps' ? 'steps' : fixture.expected_kind);
	});
});

describe('run stubs', () => {
	it('stand where their rows were: one fold between the items that close them', () => {
		const entries = build(
			[human('ask', 1, '10:00:00', 't1', '10:00:00'), said('reply', 6, '10:10:00')],
			[stub(2, 5, { weight: 7 })],
		);

		expect(entries.map((entry) => entry.kind)).toEqual(['message', 'fold', 'message']);
		const [run] = folds(entries);
		expect(run.items).toEqual([]);
		expect(run.id).toBe('run:reply');
		expect(run.turnIds).toEqual(['t1']);
		expect(runWeight(run)).toBe(7);
		expect(runLabels(entries, { t1: turn('10:00:00', '10:10:00', 7) }, true).get(run.id)).toMatch(/^Worked · 7 steps/);
	});

	it('count the stub once when its rows are also loaded', () => {
		const rows = [
			human('ask', 1, '10:00:00', 't1', '10:00:00'),
			work('a', 4, '10:01:00'), work('b', 5, '10:02:00'),
			said('reply', 6, '10:10:00'),
		];

		const [run] = folds(build(rows, [stub(2, 5, { weight: 6 })]));

		expect(run.items.map((item) => item.message.seq)).toEqual([4, 5]);
		expect(runWeight(run)).toBe(6);
	});

	it('join into one run when no visible row lies between them, keeping an expansion made before the join', () => {
		// The newest page ended inside a run; the next older page brings the rest of it.
		const reply = said('reply', 20, '10:10:00');
		const newer = stub(10, 19, { firstId: 'x10', lastId: 'x19' });
		const older = stub(3, 9, { firstId: 'x3', lastId: 'x9', weight: 4 });

		const before = build([reply], [newer]);
		const open = new Set([...toggleRun(runTokens(before, 0, false), new Set())].filter((token) => token.startsWith('row:')));
		const after = build([said('earlier', 2, '10:00:00'), reply], [older, newer]);

		const [joined] = folds(after);
		expect(folds(after)).toHaveLength(1);
		expect(joined.stub).toMatchObject({ firstId: 'x3', lastId: 'x19', count: 17 });
		const at = after.indexOf(joined);
		expect(isRunExpanded(runTokens(after, at, true), open)).toBe(true);
		// A run opened on the older half survives the join as well.
		const openOlder = toggleRun(runTokens(build([said('earlier', 2, '10:00:00')], [older]), 1, false), new Set());
		const rowTokens = new Set([...openOlder].filter((token) => token.startsWith('row:')));
		expect(rowTokens.size).toBe(2);
		expect(isRunExpanded(runTokens(after, at, true), rowTokens)).toBe(true);
	});

	it('are split by a visible row, even one of the same Turn', () => {
		const entries = build(
			[said('s1', 9, '10:00:00'), said('s2', 15, '10:05:00')],
			[stub(2, 8), stub(10, 14)],
		);

		expect(folds(entries).map((run) => run.stub?.firstId)).toEqual(['m2', 'm10']);
	});

	it('leave a Turn that starts after the read apart from them', () => {
		const read = [said('reply', 20, '10:10:00')];
		const live = [...read, work('later', 21, '10:20:00', 't2')];

		const entries = build(live, [stub(10, 19, { turnIds: ['t1'] })], { turnId: 't2' });

		const [finished, open] = folds(entries);
		expect(finished.stub?.turnIds).toEqual(['t1']);
		expect(finished.live).toBe(false);
		expect(open.stub).toBeUndefined();
		expect(open.turnIds).toEqual(['t2']);
		expect(open.live).toBe(true);
	});

	it('survive update cards: a card before one, after one, or inside one never drops it', () => {
		const rows = [human('ask', 1, '10:00:00', 't1', '10:00:00'), said('reply', 20, '10:10:00')];
		for (const afterSeq of [1, 12, 19]) {
			const entries = mergeTranscriptUpdates(
				build(rows, [stub(10, 19, { weight: 3 })]),
				[card(1, afterSeq)],
				{ focusMode: true, divider: null },
			);
			expect(folds(entries).map((run) => run.stub?.firstId)).toContain('m10');
			expect(entries.some((entry) => entry.kind === 'update')).toBe(true);
		}
	});

	it('combine by position, keeping each Turn once in order', () => {
		const joined = combineStubs([
			stub(10, 12, { turnIds: ['t1', 't2'], weight: 2 }),
			stub(3, 9, { turnIds: ['t1'], weight: 5 }),
		]);

		expect(joined).toMatchObject({ firstSeq: 3, lastSeq: 12, weight: 7, count: 10 });
		expect(joined.turnIds).toEqual(['t1', 't2']);
	});
});

describe('a seed, which the transcript never draws', () => {
	const seed = (seq: number) => ({
		id: `seed-${seq}`, thread_id: 'thread-1', role: 'system', content: 'starting context', seq,
		created_at: '2026-10-02T10:00:30', metadata: { cc: true, type: 'seed' },
	}) as unknown as TranscriptMessage;
	const context = (seq: number) => ({
		id: `ctx-${seq}`, thread_id: 'thread-1', role: 'system', content: 'Plan refreshed', seq,
		created_at: '2026-10-02T10:00:20', activity_weight: 0, metadata: { type: 'context_event' },
	}) as unknown as TranscriptMessage;

	it('makes no block between two visible rows, as the Focus page sends no stub for it', () => {
		const entries = build([human('ask', 1, '10:00:00', 't1', '10:00:00'), seed(2), said('reply', 3, '10:01:00')], []);

		expect(entries.map((entry) => entry.kind)).toEqual(['message', 'message']);
	});

	it('leaves a no-step block of context rows named for its context', () => {
		const entries = build([
			human('ask', 1, '10:00:00', 't1', '10:00:00'), context(2), seed(3), said('reply', 4, '10:01:00'),
		], []);

		const [run] = folds(entries);
		expect(run.items.map((item) => item.message.id)).toEqual(['ctx-2']);
		expect(runLabels(entries, {}, true).get(run.id)).toBe('Context updated');
	});
});
