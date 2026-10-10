import { describe, expect, it } from 'vitest';

import { card, human, present, runFaces, said, turn, wake, work } from '$lib/__tests__/fixtures/focusRuns';
import { PLAN_LIMIT_REASON } from '$lib/stores/overageSetting';
import type { TranscriptEntry } from './messageListModel';
import { attachPlanLimitPauses, planLimitPauseDescriptors } from './planLimitPause';
import { sourceMembers } from './contextEventModel';
import { runLabels, runWeight } from './transcriptRunLabel';

const paused = { ...turn('10:00:00', '10:05:00', 2, 'cancelled'), cause: PLAN_LIMIT_REASON };
const stopped = { ...turn('10:00:00', '10:05:00', 2, 'cancelled'), cause: null };

/** What the pause line follows: a run by the `seq` of its rows, a message by its id. */
function pausedAfter(entries: TranscriptEntry[], summaries: Record<string, { status?: string; cause?: string | null }>): unknown[] {
	const raw = [...new Map(entries.flatMap((entry) => entry.kind === 'fold' ? entry.items : entry.kind === 'message' ? [entry] : [])
		.flatMap((item) => [...sourceMembers(item.message)]).map((row) => [row.id, row])).values()];
	const composed = attachPlanLimitPauses(entries, planLimitPauseDescriptors(raw, summaries));
	expect(composed.unavailable).toEqual([]);
	return composed.entries.flatMap((entry) => {
		if (entry.kind === 'fold' && entry.pauses?.length) return [entry.items.map((item) => item.message.seq)];
		return entry.kind === 'pause' ? [`message-${entry.pause.source.id}`] : [];
	});
}

describe('plan-limit pause line on runs', () => {
	it('follows the last run of a Turn that a post_update split, not the first', () => {
		const rows = [
			human('ask', 1, '10:00:00', 'p', '10:00:00'),
			work('a', 2, '10:01:00', 'p'),
			said('update', 3, '10:02:00', 'p'),
			work('b', 4, '10:03:00', 'p'),
		];

		expect(pausedAfter(present(rows), { p: paused })).toEqual([[4]]);
		// A Turn the user cancelled carries no plan-limit cause and gets no line.
		expect(pausedAfter(present(rows), { p: stopped })).toEqual([]);
	});

	it('follows the last part when a card splits the paused Turn\'s run', () => {
		const rows = [human('ask', 1, '10:00:00', 'p', '10:00:00'), work('a', 2, '10:01:00', 'p'), work('b', 3, '10:02:00', 'p')];

		expect(pausedAfter(present(rows, { cards: [card(1, 2)] }), { p: paused })).toEqual([[3]]);
	});

	it('names only the paused Turn\'s entry when a later Turn follows it', () => {
		const rows = [
			human('ask', 1, '10:00:00', 'p', '10:00:00'),
			work('a', 2, '10:01:00', 'p'),
			human('again', 3, '10:06:00', 'next', '10:06:00'),
			work('b', 4, '10:07:00', 'next'),
			said('reply', 5, '10:08:00', 'next'),
		];

		expect(pausedAfter(present(rows), { p: paused, next: turn('10:06:00', '10:08:00', 1) })).toEqual([[2]]);
	});

	it('stays on the same rows when an older page loads above the paused Turn', () => {
		const older = [human('first', 1, '09:00:00', 'old', '09:00:00'), said('done', 2, '09:01:00', 'old'), work('a', 3, '10:01:00', 'p')];
		const loaded = [work('b', 4, '10:02:00', 'p'), work('c', 5, '10:03:00', 'p')];

		expect(pausedAfter(present(loaded), { p: paused })).toEqual([[4, 5]]);
		expect(pausedAfter(present([...older, ...loaded]), { p: paused })).toEqual([[3, 4, 5]]);
	});

	it('gives the same line after a reconnect refetch of the same rows', () => {
		const rows = () => [human('ask', 1, '10:00:00', 'p', '10:00:00'), work('a', 2, '10:01:00', 'p')];
		const before = planLimitPauseDescriptors(rows(), { p: paused });

		expect(planLimitPauseDescriptors(rows(), { p: paused })).toEqual(before);
		expect(before).toHaveLength(1);
	});

	it('follows the run that holds a hidden wake when the Turn stopped before any output', () => {
		const rows = [
			human('ask', 1, '09:00:00', 'old', '09:00:00'),
			said('done', 2, '09:01:00', 'old'),
			wake('wake', 3, '10:00:00', 'p'),
		];
		const entries = present(rows);

		expect(pausedAfter(entries, { p: paused })).toEqual([[3]]);
		// Naming the Turn that took the wake is provenance only: it adds no step and no count.
		const run = entries.find((entry) => entry.kind === 'fold');
		expect(run?.kind === 'fold' ? [run.turnIds, runWeight(run)] : null).toEqual([[], 0]);
		expect(runFaces(entries, runLabels(entries, { old: turn('09:00:00', '09:01:00'), p: paused }, true))).toEqual(['Agent message']);
	});

	it('follows the message a Turn took when it stopped before any output', () => {
		const entries = present([human('ask', 1, '10:00:00', 'p', '10:00:00')]);

		expect(pausedAfter(entries, { p: paused })).toEqual(['message-ask']);
	});
});
