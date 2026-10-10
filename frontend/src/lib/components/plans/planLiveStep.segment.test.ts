import { beforeEach, describe, expect, it } from 'vitest';

import { NO_LIVE_LINE, UNKNOWN_STEP, _resetLiveStepForTests, advanceLiveLine } from './planLiveStep';

const concrete = { threadId: 't1', phrase: 'Reading plan', between: false, unknown: false, startedAt: 100 };
const unknown = { threadId: 't1', phrase: UNKNOWN_STEP, between: false, unknown: true, startedAt: 100 };

beforeEach(() => _resetLiveStepForTests());

describe('advanceLiveLine with a caller segment', () => {
	it('shows UNKNOWN_STEP on a remount in the same Turn after a shown update, never the recalled phrase', () => {
		advanceLiveLine(NO_LIVE_LINE, concrete, false, 100, 'before-update');

		const remounted = advanceLiveLine(NO_LIVE_LINE, unknown, false, 200, 'update');

		expect(remounted.show).toEqual(unknown);
		// A plan row, which names no segment, still recalls the Turn's phrase from the same memory.
		expect(advanceLiveLine(NO_LIVE_LINE, unknown, false, 200).show).toMatchObject({ phrase: 'Reading plan', between: true });
	});

	it('never recalls on a remount inside the same segment either: only this row\'s own memory carries', () => {
		advanceLiveLine(NO_LIVE_LINE, concrete, false, 100, 'update');

		expect(advanceLiveLine(NO_LIVE_LINE, unknown, false, 200, 'update').show).toEqual(unknown);
	});

	it('carries this row\'s last phrase while the segment is the same', () => {
		const running = advanceLiveLine(NO_LIVE_LINE, concrete, false, 100, 'update');

		const between = advanceLiveLine(running.line, unknown, false, 200, 'update');

		expect(between.show).toMatchObject({ phrase: 'Reading plan', between: true });
		expect(between.line).toEqual({ last: concrete, deadline: null, segment: 'update' });
		expect(advanceLiveLine(between.line, unknown, false, 300, 'update').show).toMatchObject({ phrase: 'Reading plan' });
	});

	it('shows UNKNOWN_STEP when the segment changes, and does not bring the old phrase back', () => {
		const running = advanceLiveLine(NO_LIVE_LINE, concrete, false, 100, 'ask');

		const afterUpdate = advanceLiveLine(running.line, unknown, false, 200, 'update');

		expect(afterUpdate.show).toEqual(unknown);
		expect(afterUpdate.line).toEqual({ last: unknown, deadline: null, segment: 'update' });
		expect(advanceLiveLine(afterUpdate.line, unknown, false, 300, 'update').show).toEqual(unknown);
	});

	it('shows a tool that is still running across the segment change', () => {
		const running = advanceLiveLine(NO_LIVE_LINE, concrete, false, 100, 'ask');

		const across = advanceLiveLine(running.line, concrete, false, 200, 'update');

		expect(across.show).toEqual(concrete);
		expect(across.line.segment).toBe('update');
		expect(advanceLiveLine(across.line, unknown, false, 300, 'update').show).toMatchObject({ phrase: 'Reading plan', between: true });
	});

	it('does not carry into a successor Turn in the same segment', () => {
		const running = advanceLiveLine(NO_LIVE_LINE, concrete, false, 100, 'ask');

		expect(advanceLiveLine(running.line, { ...unknown, startedAt: 900 }, false, 1000, 'ask').show)
			.toEqual({ ...unknown, startedAt: 900 });
	});
});

describe('advanceLiveLine without a segment', () => {
	it('leaves the new field unset on every line it returns', () => {
		const running = advanceLiveLine(NO_LIVE_LINE, concrete, false, 100);
		expect(running.line).toStrictEqual({ last: concrete, deadline: null });

		const between = advanceLiveLine(running.line, unknown, false, 200);
		expect(between.line).toStrictEqual({ last: concrete, deadline: null });
		expect(between.show).toMatchObject({ phrase: 'Reading plan', between: true });

		const lingering = advanceLiveLine(between.line, null, false, 300);
		expect('segment' in lingering.line).toBe(false);
		expect(advanceLiveLine(running.line, concrete, true, 400).line).toBe(NO_LIVE_LINE);
		expect('segment' in NO_LIVE_LINE).toBe(false);
	});

	it('still recalls the Turn\'s phrase for a row that mounts late', () => {
		advanceLiveLine(NO_LIVE_LINE, concrete, false, 100);

		expect(advanceLiveLine(NO_LIVE_LINE, unknown, false, 200).show).toMatchObject({ phrase: 'Reading plan', between: true });
	});
});
