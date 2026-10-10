import { describe, expect, it } from 'vitest';

import {
	DAY, THREAD, card, human, inZone, notice, present, runFaces, runSeqs, said, turn, wake, work,
} from '$lib/__tests__/fixtures/focusRuns';
import type { TranscriptEntry } from './messageListModel';
import { runLabels, runWeight, type TurnSummary } from './transcriptRunLabel';

const faces = (entries: TranscriptEntry[], summaries: Record<string, TurnSummary>, historyComplete = true) => (
	runFaces(entries, runLabels(entries, summaries, historyComplete))
);

describe('run time: the worked cases', () => {
	it('W1: a post_update splits a Turn into its own minutes', () => {
		const entries = present([
			human('ask', 1, '10:00:00', 't1', '10:00:00'),
			work('a', 2, '10:01:00'), work('b', 3, '10:02:00'),
			said('update', 4, '10:04:30'),
			work('c', 5, '10:06:00'),
			said('reply', 6, '10:10:00'),
		]);

		expect(faces(entries, { t1: turn('10:00:00', '10:10:00', 3) }))
			.toEqual(['Worked · 2 steps · 4 min', 'Worked · 1 step · 5 min']);
	});

	it('W2: an input queued during the previous Turn never lends its admission time', () => {
		const rows = (written: string | null) => [
			human('ask-1', 1, '10:00:00', 't1', '10:00:00'),
			work('a', 2, '10:02:00', 't1'),
			human('queued', 3, '10:01:00', 't2', written),
			work('b', 4, '10:06:00', 't2'),
			said('reply', 5, '10:08:01', 't2'),
		];
		const summaries = { t1: turn('10:00:00', '10:05:00', 1), t2: turn('10:05:01', '10:08:01', 1) };

		// The run before it ends at its own Turn's end; the run after starts when the next Turn took the input.
		expect(faces(present(rows('10:05:01')), summaries))
			.toEqual(['Worked · 1 step · 5 min', 'Worked · 1 step · 3 min']);
		// Seen only through the live frame: the rows before it are another Turn's, so it started `t2`.
		expect(faces(present(rows(null)), summaries, false))
			.toEqual(['Worked · 1 step · 5 min', 'Worked · 1 step · 3 min']);
	});

	it('W3: a notice inside a Turn is an instant, not the first and last row', () => {
		const entries = present([
			human('ask', 1, '22:20:00', 't1', '22:20:00'),
			work('a', 2, '22:20:10'),
			notice('n', 3, '22:21:00'),
			work('b', 4, '22:22:00'),
			said('reply', 5, '22:25:00'),
		]);

		expect(faces(entries, { t1: turn('22:20:00', '22:25:00', 2) }))
			.toEqual(['Worked · 1 step · 1 min', 'Worked · 1 step · 4 min']);
	});

	it('W4: a mid-Turn steer clips at its provider write, and withholds both times until that is known', () => {
		const rows = (written: string | null) => [
			human('ask', 1, '10:00:00', 't1', '10:00:00'),
			work('a', 2, '10:03:00'),
			human('steer', 3, '10:07:00', 't1', written),
			work('b', 4, '10:10:00'),
			said('reply', 5, '10:20:00'),
		];
		const summaries = { t1: turn('10:00:00', '10:20:00', 2) };

		expect(faces(present(rows('10:07:40')), summaries))
			.toEqual(['Worked · 1 step · 7 min', 'Worked · 1 step · 12 min']);
		expect(faces(present(rows(null)), summaries)).toEqual(['Worked · 1 step', 'Worked · 1 step']);
	});

	it('W5: a notice after the last step ends the run there, and the rest of the Turn is claimed by no run', () => {
		const entries = present([
			human('ask', 1, '10:00:00', 't1', '10:00:00'),
			work('a', 2, '10:03:00'),
			notice('n', 3, '10:06:00'),
		]);

		expect(faces(entries, { t1: turn('10:00:00', '10:10:00', 1) })).toEqual(['Worked · 1 step · 6 min']);
	});

	it('W5b: an unproven bound shows the count and no time, never the whole Turn', () => {
		const rows = (written: string | null) => [
			human('ask', 1, '10:00:00', 't1', '10:00:00'),
			work('a', 2, '10:03:00'),
			human('steer', 3, '10:06:00', 't1', written),
			work('b', 4, '10:08:00'),
		];
		const summaries = { t1: turn('10:00:00', '10:10:00', 2) };

		expect(faces(present(rows(null)), summaries)).toEqual(['Worked · 1 step', 'Worked · 1 step']);
		expect(faces(present(rows('10:06:00')), summaries))
			.toEqual(['Worked · 1 step · 6 min', 'Worked · 1 step · 4 min']);
	});

	it('W6: a card recorded mid-run splits the weights exactly and proves no time', () => {
		const rows = [
			human('ask', 9, '10:00:00', 't1', '10:00:00'),
			...[10, 11, 12, 13, 14].map((seq) => work(`w${seq}`, seq, '10:01:00')),
			said('reply', 15, '10:10:00'),
		];
		const entries = present(rows, { cards: [card(7, 12)] });

		expect(entries.map((entry) => entry.kind)).toEqual(['message', 'fold', 'update', 'fold', 'message']);
		expect(runSeqs(entries)).toEqual([[10, 11, 12], [13, 14]]);
		expect(faces(entries, { t1: turn('10:00:00', '10:10:00', 5) })).toEqual(['Worked · 3 steps', 'Worked · 2 steps']);
	});

	it('W7: a card recorded while no Turn runs places no time on either side', () => {
		const rows = [
			human('ask', 18, '10:00:00', 't1', '10:00:00'),
			work('a', 19, '10:02:00', 't1'), work('b', 20, '10:04:00', 't1'),
			wake('wake', 21, '10:30:00', 't2'),
			work('c', 22, '10:31:00', 't2'),
			said('reply', 23, '10:35:00', 't2'),
		];
		const entries = present(rows, { cards: [card(8, 20, 'node', '10:20:00')] });

		expect(runSeqs(entries)).toEqual([[19, 20], [21, 22]]);
		expect(faces(entries, { t1: turn('10:00:00', '10:05:00', 2), t2: turn('10:30:00', '10:35:00', 1) }))
			.toEqual(['Worked · 2 steps', 'Worked · 1 step']);
	});

	it('W10: a run clipped at the page top shows no count, no "since" and no Turn number until the older page loads', () => {
		const older = [human('ask', 1, '20:59:00', 't0', '20:59:00'), said('first', 2, '20:59:30', 't0')];
		const run = [
			wake('wake-1', 3, '21:00:00', 't1'), work('a', 4, '21:01:00', 't1'),
			wake('wake-2', 5, '21:10:00', 't2'), work('b', 6, '21:11:00', 't2'),
			said('reply', 7, '21:12:00', 't2'),
		];
		const summaries = {
			t0: turn('20:59:00', '20:59:30'), t1: turn('21:00:00', '21:02:00', 1), t2: turn('21:10:00', '21:12:00', 1),
		};

		inZone('America/Los_Angeles', () => {
			expect(faces(present(run.slice(1)), summaries, false)).toEqual(['Worked · 2+ steps']);
			expect(faces(present([...older, ...run]), summaries, false)).toEqual(['Worked · 2 steps · 4 min']);
		});
	});

	it('W11: a naive start and an explicit-offset end are the same clock in a zone that is not UTC', () => {
		const rows = [
			human('ask', 1, '04:00:00', 't1', '04:00:00'),
			work('a', 2, '04:01:00'),
			{ ...said('reply', 3, '04:03:00'), created_at: `${DAY}04:03:00+00:00` },
		];
		const summaries = { t1: { ...turn('04:00:00', null, 1), ended_at: `${DAY}04:03:00+00:00` } };

		for (const zone of ['America/Los_Angeles', 'Asia/Kolkata', 'UTC']) {
			inZone(zone, () => expect(faces(present(rows), summaries)).toEqual(['Worked · 1 step · 3 min']));
		}
	});

	it('W12: on a clipped page an input claimed by the Turn bounds the run only by its provider write', () => {
		const rows = (written: string | null) => [
			human('ask', 40, '10:07:00', 't1', written),
			work('a', 41, '10:10:00'),
			said('reply', 42, '10:20:00'),
		];
		const summaries = { t1: turn('10:00:00', '10:20:00', 9) };

		// (a) written: the bound, whether or not older rows of the Turn sit above the page.
		expect(faces(present(rows('10:07:40')), summaries, false)).toEqual(['Worked · 1 step · 12 min']);
		// (b) not written yet, older history exists: start or steer cannot be told apart.
		expect(faces(present(rows(null)), summaries, false)).toEqual(['Worked · 1 step']);
		// (c) not written yet, no older history: it started the Turn.
		expect(faces(present(rows(null)), summaries, true)).toEqual(['Worked · 1 step · 20 min']);
	});

	it('W13: after an input an earlier Turn took, the run starts at its own Turn\'s start', () => {
		const entries = present([
			human('x', 1, '10:00:00', 't0', '10:00:00'),
			wake('wake', 2, '10:30:00', 't1'),
			work('a', 3, '10:31:00'),
			said('reply', 4, '10:35:00'),
		]);

		expect(faces(entries, { t0: turn('10:00:00', '10:02:00'), t1: turn('10:30:00', '10:35:00', 1) }))
			.toEqual(['Worked · 1 step · 5 min']);
	});

	it('W16: a marked update draws no card; its message closes the run', () => {
		// `read_newest_cards` leaves `marked` rows out, so the transcript receives no card for a post_update.
		const entries = present([
			human('ask', 1, '10:00:00', 't1', '10:00:00'),
			work('a', 2, '10:01:00'),
			said('update', 3, '10:04:30'),
			work('b', 4, '10:06:00'),
			said('reply', 5, '10:10:00'),
		], { cards: [] });

		expect(entries.some((entry) => entry.kind === 'update')).toBe(false);
		expect(faces(entries, { t1: turn('10:00:00', '10:10:00', 2) }))
			.toEqual(['Worked · 1 step · 4 min', 'Worked · 1 step · 5 min']);
	});
});

/** One Turn, 10:00 to 10:10, cut into three runs by two notices stamped `a` then `b`. */
function threeRuns(a: string, b: string, written = '10:00:00') {
	return [
		human('ask', 30, '10:00:00', 't1', written),
		work('r1', 31, '10:01:00'),
		notice('A', 32, a),
		work('r2', 33, '10:02:00'),
		notice('B', 34, b),
		work('r3', 35, '10:03:00'),
		said('reply', 36, '10:10:00'),
	];
}

describe('run time: stamps that go backwards', () => {
	const summaries = { t1: turn('10:00:00', '10:10:00', 3) };

	it('W17: two inverted notices withhold the time of every run of the Turn, the outside runs included', () => {
		expect(faces(present(threeRuns('10:08:00', '10:07:00')), summaries))
			.toEqual(['Worked · 1 step', 'Worked · 1 step', 'Worked · 1 step']);
	});

	it('W17 control: the same Turn with ordered notices keeps all three times', () => {
		expect(faces(present(threeRuns('10:07:00', '10:08:00')), summaries))
			.toEqual(['Worked · 1 step · 7 min', 'Worked · 1 step · 1 min', 'Worked · 1 step · 2 min']);
	});

	it('W17: a multi-Turn run that starts in the failing Turn shows "Worked", not a "since"', () => {
		const inverted = threeRuns('10:08:00', '10:07:00').slice(0, -1);
		const tail = [wake('wake', 36, '10:20:00', 't2'), work('later', 37, '10:21:00', 't2'), said('reply', 38, '10:22:00', 't2')];
		const both = { ...summaries, t2: turn('10:20:00', '10:22:00', 1) };

		expect(faces(present([...inverted, ...tail]), both)).toEqual(['Worked · 1 step', 'Worked · 1 step', 'Worked · 2 steps']);
		const ordered = threeRuns('10:07:00', '10:08:00').slice(0, -1);
		inZone('UTC', () => expect(faces(present([...ordered, ...tail]), both))
			.toEqual(['Worked · 1 step · 7 min', 'Worked · 1 step · 1 min', 'Worked · 2 steps · 4 min']));
	});

	it('an edge clamp beside an in-range instant does not hide an inversion between two in-range stamps', () => {
		// The ask was written 10 s before the Turn's start, so its bound clamps to the Turn edge.
		expect(faces(present(threeRuns('10:08:00', '10:07:00', '09:59:50')), summaries))
			.toEqual(['Worked · 1 step', 'Worked · 1 step', 'Worked · 1 step']);
		// Control: the same clamp with ordered stamps keeps every time, the clamped run from the Turn's start.
		expect(faces(present(threeRuns('10:06:00', '10:07:00', '09:59:50')), summaries))
			.toEqual(['Worked · 1 step · 6 min', 'Worked · 1 step · 1 min', 'Worked · 1 step · 3 min']);
	});

	it('W18: an inversion above the page top is unseen until that page loads, then withholds the loaded runs too', () => {
		const older = [
			human('ask', 29, '10:00:00', 't1', '10:00:00'),
			work('r0', 30, '10:00:30'),
			notice('A', 31, '10:02:00'),
			work('r1', 32, '10:02:30'),
			notice('B', 33, '10:01:00'),
			work('r2', 34, '10:03:00'),
		];
		const loaded = [notice('C', 50, '10:05:00'), work('r3', 51, '10:06:00'), said('reply', 52, '10:10:00')];
		const turns = { t1: turn('10:00:00', '10:10:00', 4) };

		expect(faces(present(loaded), turns, false)).toEqual(['Worked · 1 step · 5 min']);
		expect(faces(present([...older, ...loaded]), turns, true))
			.toEqual(['Worked · 1 step', 'Worked · 1 step', 'Worked · 1 step', 'Worked · 1 step']);
	});

	it('uses a Turn edge in the check only when a run uses it', () => {
		const rows = [
			human('ask', 1, '10:00:00', 't1', '10:00:00'),
			work('a', 2, '10:01:00'),
			said('update', 3, '10:07:00'),
			work('b', 4, '10:08:00'),
		];
		// The Turn's recorded end is before its own update's stamp.
		const early = { t1: turn('10:00:00', '10:05:00', 2), t2: turn('10:20:00', '10:21:00') };

		// No run ends at that edge: it is not evidence, and the late stamps clamp to it.
		expect(faces(present([...rows, said('reply', 5, '10:09:00')]), early))
			.toEqual(['Worked · 1 step · 5 min', 'Worked · 1 step · under 1 min']);
		// The next Turn's ask ends the last run at that edge: now the edge is used, and it goes backwards.
		expect(faces(present([...rows, human('next', 5, '10:20:00', 't2', '10:20:00')]), early))
			.toEqual(['Worked · 1 step', 'Worked · 1 step']);
	});
});

describe('run time: clamps and withholds', () => {
	const summaries = { t1: turn('10:00:00', '10:10:00', 1) };
	const one = (before: ReturnType<typeof human>, after: ReturnType<typeof human>) => (
		faces(present([before, work('a', 2, '10:01:00'), after]), summaries)
	);

	it('clamps a bound before the Turn\'s start or after its end to the Turn edge', () => {
		expect(one(human('ask', 1, '09:58:00', 't1', '09:59:00'), said('reply', 3, '10:04:00')))
			.toEqual(['Worked · 1 step · 4 min']);
		expect(one(human('ask', 1, '10:00:00', 't1', '10:02:00'), said('reply', 3, '10:10:30')))
			.toEqual(['Worked · 1 step · 8 min']);
	});

	it('takes the Turn edge from a neighbour stamped outside the Turn', () => {
		// A reply of the Turn before, and a notice inserted after this Turn ended.
		expect(one(said('earlier', 1, '09:50:00', 't0'), notice('late', 3, '10:12:00')))
			.toEqual(['Worked · 1 step · 10 min']);
	});

	it('withholds for a stamp that does not parse', () => {
		expect(one(human('ask', 1, '10:00:00', 't1', '10:00:00'), { ...said('reply', 3, '10:04:00'), created_at: 'soon' }))
			.toEqual(['Worked · 1 step']);
		expect(faces(
			present([human('ask', 1, '10:00:00', 't1', '10:00:00'), work('a', 2, '10:01:00'), said('reply', 3, '10:04:00')]),
			{ t1: { ...summaries.t1, started_at: 'not a time' } },
		)).toEqual(['Worked · 1 step']);
	});

	it('withholds for an input with no claiming Turn', () => {
		expect(one(human('ask', 1, '10:00:00', null, '10:00:00'), said('reply', 3, '10:04:00')))
			.toEqual(['Worked · 1 step']);
		expect(one(human('ask', 1, '10:00:00', 't1', '10:00:00'), human('next', 3, '10:04:00', null, null)))
			.toEqual(['Worked · 1 step']);
	});

	it('withholds beside a notice of a type whose writer was not read', () => {
		expect(one(human('ask', 1, '10:00:00', 't1', '10:00:00'), notice('n', 3, '10:04:00', 'sandbox_denied')))
			.toEqual(['Worked · 1 step']);
		for (const type of ['cc_error', 'tool_bridge_unavailable']) {
			expect(one(human('ask', 1, '10:00:00', 't1', '10:00:00'), notice('n', 3, '10:04:00', type)))
				.toEqual(['Worked · 1 step · 4 min']);
		}
	});

	it('uses the Turn end after a subagent close folds into its work block', () => {
		expect(one(human('ask', 1, '10:00:00', 't1', '10:00:00'), notice('n', 3, '10:04:00', 'subagent_done')))
			.toEqual(['Worked · 1 step · 10 min']);
	});

	it('withholds for a notice inside a running Turn unless a later row of that Turn is loaded', () => {
		const running = { t1: turn('10:00:00', null, 1, 'running') };
		const rows = [human('ask', 1, '10:00:00', 't1', '10:00:00'), work('a', 2, '10:01:00'), notice('n', 3, '10:04:00')];

		expect(faces(present(rows), running)).toEqual(['Worked · 1 step']);
		expect(faces(present([...rows, work('b', 4, '10:05:00')], { live: { turnId: 't1' } }), running))
			.toEqual(['Worked · 1 step · 4 min']);
	});

	it('withholds when the run would end at the end of a Turn that is still running or waiting to retry', () => {
		const rows = [human('ask', 1, '10:00:00', 't1', '10:00:00'), work('a', 2, '10:01:00')];

		for (const status of ['running', 'waiting_retry']) {
			expect(faces(present(rows), { t1: turn('10:00:00', null, 1, status) })).toEqual(['Worked · 1 step']);
		}
		for (const status of ['completed', 'cancelled', 'error']) {
			expect(faces(present(rows), { t1: turn('10:00:00', '10:02:00', 1, status) })).toEqual(['Worked · 1 step · 2 min']);
		}
	});

	it('shows no time for a Turn with no summary, and counts from the rows', () => {
		expect(faces(present([human('ask', 1, '10:00:00', 't1', '10:00:00'), work('a', 2, '10:01:00')]), {}))
			.toEqual(['Worked · 1 step']);
	});

	it('reads 59 seconds as "under 1 min" and 60 seconds as "1 min"', () => {
		const lasting = (end: string) => faces(
			present([human('ask', 1, '10:00:00', 't1', '10:00:00'), work('a', 2, '10:00:10'), said('reply', 3, end)]),
			{ t1: turn('10:00:00', end, 1) },
		);

		expect(lasting('10:00:59')).toEqual(['Worked · 1 step · under 1 min']);
		expect(lasting('10:01:00')).toEqual(['Worked · 1 step · 1 min']);
	});

	it('spans a daylight-saving change by elapsed time, in a zone that has one and a zone that does not', () => {
		// US clocks go forward at 2026-03-08 10:00 UTC and back at 2026-11-01 09:00 UTC.
		const spans = [['2026-03-08T09:50:00', '2026-03-08T10:20:00'], ['2026-11-01T08:50:00', '2026-11-01T09:20:00']];
		for (const zone of ['America/Los_Angeles', 'Asia/Kolkata']) {
			for (const [start, end] of spans) {
				const rows = [
					{ ...human('ask', 1, '', 't1', null), created_at: start, write_started_at: start },
					{ ...work('a', 2, ''), created_at: start },
					{ ...said('reply', 3, ''), created_at: `${end}+00:00` },
				];
				const summary = { started_at: start, ended_at: end, status: 'completed', activity_count: 1 };
				inZone(zone, () => expect(faces(present(rows), { t1: summary })).toEqual(['Worked · 1 step · 30 min']));
			}
		}
	});

	it('labels a multi-Turn block by active time, independent of the browser zone', () => {
		const rows = [
			human('ask', 1, '21:00:00', 't1', '21:00:00'), work('a', 2, '21:01:00', 't1'),
			wake('wake', 3, '21:10:00', 't2'), work('b', 4, '21:11:00', 't2'),
		];
		const summaries2 = { t1: turn('21:00:00', '21:02:00', 1), t2: turn('21:10:00', '21:12:00', 1) };

		const label = faces(present(rows), summaries2);
		inZone('America/Los_Angeles', () => expect(faces(present(rows), summaries2)).toEqual(label));
		inZone('Asia/Kolkata', () => expect(faces(present(rows), summaries2)).toEqual(label));
		expect(label[0]).not.toMatch(/since|AM|PM/);
	});
});

describe('run count', () => {
	const summaries = { t1: turn('10:00:00', '10:10:00', 4) };
	const context = (id: string, seq: number, summary: string) => ({
		id, thread_id: THREAD, role: 'system', content: summary, seq, created_at: `${DAY}09:59:00`,
		activity_weight: 0, metadata: { type: 'context_event', summary },
	});

	it('names a known-zero run of context rows by its context, never "Worked" or "0 steps"', () => {
		expect(faces(present([context('c', 1, 'Plan refreshed'), human('ask', 2, '10:00:00', null, null)]), {}))
			.toEqual(['Context updated']);
	});

	it('shows no number as soon as one row\'s weight is unknown, never a partial sum', () => {
		const rows = [
			human('ask', 1, '10:00:00', 't1', '10:00:00'),
			work('a', 2, '10:01:00', 't1', 3), work('live', 3, '10:02:00', 't1', null),
			said('reply', 4, '10:10:00'),
		];
		const entries = present(rows);

		expect(faces(entries, summaries)).toEqual(['Worked · 10 min']);
		const run = entries.find((entry) => entry.kind === 'fold');
		expect(run?.kind === 'fold' ? runWeight(run) : 0).toBeNull();
	});

	it('keeps a known zero apart from unknown', () => {
		const entries = present([context('c', 1, 'Plan refreshed'), human('ask', 2, '10:00:00', null, null)]);
		const run = entries.find((entry) => entry.kind === 'fold');

		expect(run?.kind === 'fold' ? runWeight(run) : null).toBe(0);
	});

	it('sums a whole-Turn run to the Turn\'s own count', () => {
		const rows = [
			human('ask', 1, '10:00:00', 't1', '10:00:00'),
			work('a', 2, '10:01:00', 't1', 3), work('b', 3, '10:02:00', 't1', 1),
			said('reply', 4, '10:10:00'),
		];

		expect(faces(present(rows), summaries)).toEqual(['Worked · 4 steps · 10 min']);
	});

	it('shows no count while the run\'s start is above the loaded page', () => {
		const rows = [work('a', 2, '10:01:00'), said('reply', 3, '10:10:00')];

		expect(faces(present(rows), summaries, false)).toEqual(['Worked · 1+ steps']);
		expect(faces(present(rows), summaries, true)).toEqual(['Worked · 1 step · 10 min']);
	});
});
