import { describe, expect, it } from 'vitest';
import { artifact, auto, card, compact, input, said, summary, suppressed, tool, work, CAUSE } from '$lib/__tests__/fixtures/focusPauseProof';
import { buildMessageListPresentation, buildTranscriptPresentation, buildVisibleMessages, type TranscriptEntry } from './messageListModel';
import { sourceMembers, sourceRange } from './contextEventModel';
import { attachPlanLimitPauses, planLimitPauseDescriptors } from './planLimitPause';
import { countVisibleEntries, runTokens } from './transcriptFocusModel';
import { runLabels, runWeight } from './transcriptRunLabel';
import { mergeTranscriptUpdates } from './transcriptUpdates';

const paused = { p: summary('cancelled', 0, CAUSE) };
const realLeaves = (entries: TranscriptEntry[]) => entries.flatMap((entry) => entry.kind === 'fold' ? entry.items : entry.kind === 'message' ? [entry] : []);

function compose(rows: ReturnType<typeof work>[], cards: ReturnType<typeof card>[] = [], focusMode = true) {
	const pauses = planLimitPauseDescriptors(rows, paused);
	const visible = buildMessageListPresentation(rows, pauses.map((pause) => pause.source)).visibleMessages;
	const entries = mergeTranscriptUpdates(buildTranscriptPresentation(visible, focusMode, { turnId: 'n' }), cards, { focusMode, divider: null });
	return { pauses, entries, result: attachPlanLimitPauses(entries, pauses) };
}

describe('raw source conservation and transparent historical slots', () => {
	it('keeps raw references without serializing hidden digest details', () => {
		const row = { ...suppressed('s1', 1, 'p'), metadata: { ...suppressed('s1', 1, 'p').metadata,
			context_events: [{ id: 'hidden', chip: null, summary: 'Private raw detail' }, { id: 'shown', summary: 'Visible detail' }] } };
		const [visible] = buildVisibleMessages([row]);
		expect(sourceMembers(visible)).toEqual([row]);
		expect(sourceMembers(visible)[0]).toBe(row);
		expect(JSON.stringify(visible)).not.toContain('Private raw detail');
		expect(row.metadata.context_events).toHaveLength(2);
	});

	for (const kind of ['compact', 'tool', 'artifact'] as const) it(`conserves every ${kind} member/weight across an inside card`, () => {
		const rows = [1, 2, 3].map((seq) => kind === 'compact' ? compact(`r${seq}`, seq, 'p')
			: kind === 'tool' ? tool(`r${seq}`, seq, 'p') : artifact(`r${seq}`, seq));
		const visible = buildVisibleMessages(rows);
		expect(visible).toHaveLength(1);
		expect(visible[0].seq).toBeUndefined();
		expect(sourceMembers(visible[0])).toEqual(rows);
		expect(sourceRange(visible[0])).toEqual({ min: 1, max: 3 });
		const entries = mergeTranscriptUpdates(buildTranscriptPresentation(visible, true), [card(10, 2)], { focusMode: true, divider: null });
		expect(entries.map((entry) => entry.kind)).toEqual(['fold', 'update', 'fold']);
		expect(realLeaves(entries).flatMap((item) => [...sourceMembers(item.message)])).toEqual(rows);
		expect(entries.flatMap((entry) => entry.kind === 'fold' ? [runWeight(entry)] : [])).toEqual(kind === 'tool' ? [2, 1] : [0, 0]);
		expect(entries.flatMap((entry, index) => entry.kind === 'fold' ? [runTokens(entries, index, false).filter((token) => token.startsWith('row:'))] : [])).toEqual([['row:r1', 'row:r2'], ['row:r3']]);
	});

	it('retains a suppressed barrier through aggregate rebuilding without showing its source', () => {
		const rows = [compact('a1', 1, 'old'), compact('a2', 2, 'old'), suppressed('s3', 3, 'p'), compact('a4', 4, 'n'), compact('a5', 5, 'n'), compact('a6', 6, 'n')];
		const built = compose(rows, [card(10, 5)]);
		expect(built.pauses.map((pause) => pause.source)).toEqual([rows[2]]);
		expect(built.result.unavailable).toEqual([]);
		const represented = realLeaves(built.result.entries).flatMap((item) => [...sourceMembers(item.message)]);
		expect(represented.map((row) => row.id)).toEqual(['a1', 'a2', 'a4', 'a5', 'a6']);
		for (const item of realLeaves(built.result.entries)) expect(sourceMembers(item.message).some((row) => row.seq < 3)
			&& sourceMembers(item.message).some((row) => row.seq > 3)).toBe(false);
		const first = built.result.entries[0];
		expect(first).toMatchObject({ kind: 'fold', pauses: [{ pause: { source: rows[2] }, afterItem: 1, showProxy: true }] });
	});

	it('uses a standalone suppressed slot before its equal card and preserves successor expansion/timing/count', () => {
		const rows = [said('old1', 1, 'old'), suppressed('s2', 2, 'p'), auto('n3', 3, 'p', 'n'), work('n4', 4, 'n')];
		const built = compose(rows, [card(10, 2)]);
		expect(built.result.unavailable).toEqual([]);
		expect(built.entries.map((entry) => entry.kind)).toEqual(['message', 'update', 'fold']);
		expect(built.result.entries.map((entry) => entry.kind)).toEqual(['message', 'pause', 'update', 'fold']);
		expect(built.result.entries[1]).toMatchObject({ pause: { stoppedTurnId: 'p', source: rows[1] }, showProxy: true });
		expect(runTokens(built.entries, 2, true)).toEqual(runTokens(built.result.entries, 3, true));
		expect(countVisibleEntries(built.result.entries)).toBe(countVisibleEntries(built.entries));
		expect(runLabels(built.result.entries, paused, true)).toEqual(runLabels(built.entries, paused, true));
	});

	it('keeps an internal suppressed slot in one run until a shown card leaves neither side containing it', () => {
		const rows = [work('old1', 1, 'old'), suppressed('s2', 2, 'p'), auto('n3', 3, 'p', 'n'), work('n4', 4, 'n')];
		const inside = compose(rows);
		expect(inside.result.entries).toHaveLength(1);
		expect(inside.result.entries[0]).toMatchObject({ kind: 'fold', live: true, pauses: [{ afterItem: 0, showProxy: true }] });
		const split = compose(rows, [card(10, 2)]);
		expect(split.result.entries.map((entry) => entry.kind)).toEqual(['fold', 'pause', 'update', 'fold']);
		expect(split.result.entries.filter((entry) => entry.kind === 'fold').map((entry) => entry.pauses)).toEqual([[], []]);
	});

	it('attaches once after the final content phase and before equal cards', () => {
		const row = said('final1', 1, 'p', true);
		const built = compose([row, work('n2', 2, 'n')], [card(11, 1), card(10, 1)]);
		expect(built.result.unavailable).toEqual([]);
		expect(built.result.entries.map((entry) => entry.kind)).toEqual(['fold', 'message', 'pause', 'update', 'update', 'fold']);
		expect(built.result.entries[2]).toMatchObject({ pause: { source: row }, showProxy: false });
		expect(built.result.entries[0]).toMatchObject({ pauses: [] });
		expect(realLeaves(built.result.entries).filter((item) => sourceMembers(item.message)[0] === row)).toHaveLength(2);
	});

	it('keeps multiple stopped Turns in one live run without adding weights/tokens', () => {
		const rows = [work('p1', 1, 'p'), auto('r2', 2, 'p', 'r'), work('r3', 3, 'r'), auto('n4', 4, 'r', 'n'), work('n5', 5, 'n')];
		const pauses = planLimitPauseDescriptors(rows, { ...paused, r: summary('cancelled', 1, CAUSE) });
		const entries = buildTranscriptPresentation(rows, true, { turnId: 'n' });
		const result = attachPlanLimitPauses(entries, pauses);
		expect(result.entries).toHaveLength(1);
		expect(result.entries[0]).toMatchObject({ live: true, pauses: [{ afterItem: 0 }, { afterItem: 2 }] });
		const run = result.entries[0];
		expect(run.kind === 'fold' && runWeight(run)).toBe(3);
		expect(runTokens(result.entries, 0, true)).toEqual(runTokens(entries, 0, true));
		expect(countVisibleEntries(result.entries)).toBe(1);
		expect(entries[0]).not.toHaveProperty('pauses');
	});

	it('requires cancelled canonical cause and never takes a continuation predecessor as its claimant', () => {
		const rows = [auto('n1', 1, 'p', 'n')];
		expect(planLimitPauseDescriptors(rows, paused)).toEqual([]);
		expect(planLimitPauseDescriptors([work('p1', 1, 'p')], { p: summary('completed', 1, CAUSE) })).toEqual([]);
		expect(planLimitPauseDescriptors([input('p1', 1, 'p')], { p: { cause: CAUSE } })).toEqual([]);
	});

	it('reports unavailable instead of moving an anchor past an aggregate that spans it', () => {
		const rows = [compact('p1', 1, 'p'), compact('n2', 2, 'n'), compact('n3', 3, 'n')];
		const pauses = planLimitPauseDescriptors(rows, paused);
		const entries = buildTranscriptPresentation(buildVisibleMessages(rows), true);
		const result = attachPlanLimitPauses(entries, pauses);
		expect(result.unavailable).toEqual(pauses);
		expect(result.entries[0]).toMatchObject({ pauses: [] });
	});
});

describe('Focus review boundary', () => {
	const threadId = 'focus-pause-proof';
	const idle = { threadId, phase: 'idle' as const, turnId: null };
	const active = { threadId, phase: 'active' as const, turnId: 'n' };
	function history(boundary: number | null = 2) {
		const rows = [said('old1', 1, 'old'), suppressed('p2', 2, 'p'), work('n3', 3, 'n')];
		const pauses = planLimitPauseDescriptors(rows, paused);
		const visible = buildMessageListPresentation(rows, pauses.map((pause) => pause.source)).visibleMessages;
		const entries = mergeTranscriptUpdates(buildTranscriptPresentation(visible, true), boundary === null ? [card(10, 2)] : [], {
			focusMode: true, divider: { since: '2026-10-02T10:02:00Z', first_seq: boundary },
		});
		return { rows, pauses, entries };
	}
	for (const boundary of [2, 3]) it(`B2 suppressed source respects visit boundary ${boundary}`, () => {
		const { pauses, entries } = history(boundary);
		const result = attachPlanLimitPauses(entries, pauses);
		expect(result.unavailable).toEqual([]);
		expect(result.entries.map((entry) => entry.kind)).toEqual(boundary === 2
			? ['message', 'divider', 'pause', 'fold'] : ['message', 'pause', 'divider', 'fold']);
		expect(result.entries.filter((entry) => entry.kind !== 'pause')).toEqual(entries.map((entry) => entry.kind === 'fold' ? { ...entry, pauses: [] } : entry));
		expect(result.entries.find((entry) => entry.kind === 'divider')).toBe(entries.find((entry) => entry.kind === 'divider'));
		expect(countVisibleEntries(result.entries)).toBe(countVisibleEntries(entries));
		expect(runLabels(result.entries, paused, true)).toEqual(runLabels(entries, paused, true));
		expect(runTokens(result.entries, result.entries.findIndex((entry) => entry.kind === 'fold'), true))
			.toEqual(runTokens(entries, entries.findIndex((entry) => entry.kind === 'fold'), true));
	});
	it('B2 unknown visit boundary makes suppressed history unavailable', () => {
		const { pauses, entries } = history(null);
		expect(attachPlanLimitPauses(entries, pauses).unavailable).toEqual(pauses);
	});
	it('B2 represented message cannot bypass an unknown divider boundary', () => {
		const row = input('p1', 1, 'p');
		const pauses = planLimitPauseDescriptors([row], paused);
		const entries = buildTranscriptPresentation([row], true);
		entries.push({ kind: 'divider', id: 'unknown-visit' });
		expect(attachPlanLimitPauses(entries, pauses).unavailable).toEqual(pauses);
	});
	it('B2 represented message preserves a known later visit boundary', () => {
		const row = input('p1', 1, 'p');
		const pauses = planLimitPauseDescriptors([row], paused);
		const entries = mergeTranscriptUpdates(buildTranscriptPresentation([row, work('n3', 3, 'n')], true), [], {
			focusMode: true, divider: { since: '2026-10-02T10:02:00Z', first_seq: 2 },
		});
		expect(attachPlanLimitPauses(entries, pauses).entries.map((entry) => entry.kind))
			.toEqual(['message', 'pause', 'divider', 'fold']);
	});
	for (const kind of ['idle echo', 'active claimed input'] as const) it(`F1 unrelated ${kind} preserves settled history`, () => {
		const { pauses, entries } = history();
		const future = kind === 'idle echo'
			? { ...input('future', null, null), delivery_state: undefined, _optimistic: true }
			: { ...input('future', null, 'n'), delivery_state: 'queued' };
		const withFuture = [...entries, ...buildTranscriptPresentation([future], true)];
		const result = attachPlanLimitPauses(withFuture, pauses, kind === 'idle echo' ? idle : active);
		expect(result.unavailable).toEqual([]);
		expect(result.entries.filter((entry) => entry.kind === 'pause')).toHaveLength(1);
		expect(future.seq).toBeNull();
	});
	for (const phase of ['requested', 'terminal'] as const) it(`F1 canonical ${phase} unreceipted echo preserves settled history`, () => {
		const { pauses, entries } = history();
		const echo = { ...input('new-input', null, null), delivery_state: undefined, _optimistic: true };
		const result = attachPlanLimitPauses([...entries, ...buildTranscriptPresentation([echo], true)], pauses,
			{ threadId, phase, turnId: phase === 'terminal' ? 'old' : null });
		expect(result.unavailable).toEqual([]);
		expect(result.entries.filter((entry) => entry.kind === 'pause')).toHaveLength(1);
	});
	for (const kind of ['unknown history', 'invalid sequence', 'wrong thread', 'wrong turn', 'adverse echo'] as const)
		it(`F1 ${kind} is not a proven future edge`, () => {
			const { pauses, entries } = history();
			const row = kind === 'unknown history' ? input('unknown', null, null)
				: kind === 'invalid sequence' ? { ...input('invalid', 0, null), _optimistic: true, delivery_state: undefined }
				: kind === 'wrong thread' ? { ...input('other', null, null), thread_id: 'other', _optimistic: true, delivery_state: undefined }
				: kind === 'wrong turn' ? { ...input('other-turn', null, 'other'), delivery_state: 'queued' }
				: { ...input('adverse', null, null), _optimistic: true, delivery_state: 'failed' };
			expect(attachPlanLimitPauses([...entries, ...buildTranscriptPresentation([row], true)], pauses, active).unavailable).toEqual(pauses);
		});
	it('F1 a mixed aggregate with unknown history stays unavailable', () => {
		const { pauses, entries } = history();
		const group = buildVisibleMessages([tool('tool4', 4, 'n'), tool('tool5', 5, 'n'), tool('tool6', 6, 'n')])[0];
		sourceMembers(group)[1].seq = null;
		expect(attachPlanLimitPauses([...entries, ...buildTranscriptPresentation([group], true)], pauses, active).unavailable).toEqual(pauses);
	});
	for (const kind of ['out of order', 'later unknown', 'later invalid', 'duplicate', 'conflicting owners', 'queued stopped input'] as const)
		it(`F2 ${kind} cannot borrow an earlier claimant`, () => {
			const first = work('p1', 1, 'p');
			const later = kind === 'out of order' ? [work('p3', 3, 'p'), work('p2', 2, 'p')]
				: kind === 'later unknown' ? [input('p2', null, 'p')]
				: kind === 'later invalid' ? [work('p0', 0, 'p')]
				: kind === 'duplicate' ? [work('p-again', 1, 'p')]
				: kind === 'queued stopped input' ? [{ ...input('p2', null, 'p'), delivery_state: 'queued' }]
				: [{ ...input('conflict2', 2, 'p'), metadata: { turn_id: 'q' } }];
			const rows = [first, ...later, work('r4', 4, 'r')];
			const summaries = { ...paused, q: summary('cancelled', 0, CAUSE), r: summary('cancelled', 1, CAUSE) };
			const descriptors = planLimitPauseDescriptors(rows, summaries);
			expect(descriptors.map((pause) => pause.stoppedTurnId)).toEqual(['r']);
			const result = attachPlanLimitPauses(buildTranscriptPresentation(rows, true), descriptors);
			expect(result.entries.flatMap((entry) => entry.kind === 'fold' ? entry.pauses ?? [] : []))
				.toMatchObject([{ pause: { stoppedTurnId: 'r' } }]);
		});
	it('F2 a contradictory row invalidates both known owners and preserves an unrelated Turn', () => {
		const rows = [work('q1', 1, 'q'), work('p2', 2, 'p'),
			{ ...input('conflict3', 3, 'p'), metadata: { turn_id: 'q' } }, work('r4', 4, 'r')];
		const summaries = { ...paused, q: summary('cancelled', 1, CAUSE), r: summary('cancelled', 1, CAUSE) };
		expect(planLimitPauseDescriptors(rows, summaries).map((pause) => pause.stoppedTurnId)).toEqual(['r']);
	});
	it('F2 valid settled history retains its last source', () => {
		const rows = [work('p1', 1, 'p'), work('p2', 2, 'p'), work('r3', 3, 'r')];
		expect(planLimitPauseDescriptors(rows, { ...paused, r: summary('cancelled', 1, CAUSE) }).map((pause) => pause.source))
			.toEqual([rows[1], rows[2]]);
	});
	it('F2 excluded seed and routed rows do not invalidate a valid source', () => {
		const rows = [work('p1', 1, 'p'), { ...input('seed', null, 'p', 'system'), metadata: { type: 'seed' } },
			{ ...input('routed', null, 'p'), metadata: { routed_event: true } }];
		expect(planLimitPauseDescriptors(rows, paused).map((pause) => pause.source)).toEqual([rows[0]]);
	});
});
