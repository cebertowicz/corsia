import { describe, expect, it } from 'vitest';

import { DAY, human, present, said, turn, work } from '$lib/__tests__/fixtures/focusRuns';
import { parseWsTimestamp } from '$lib/time';
import { waitRange } from './transcriptRunLabel';

const at = (clock: string) => parseWsTimestamp(DAY + clock);

describe('waitRange: where an outside wait counts from', () => {
	const summaries = { t1: turn('10:00:00', '10:10:00', 1) };

	it('counts a wait standing alone after a reply from that reply, to now', () => {
		const entries = present([work('a', 2, '10:01:00'), said('reply', 3, '10:10:00')]);

		expect(waitRange(entries.filter((entry) => entry.kind === 'message'), summaries, true))
			.toEqual({ id: 'reply', since: at('10:10:00'), until: null });
	});

	it('counts a wait inside an open run from that run\'s start, to now', () => {
		const entries = present([human('ask', 1, '10:00:00', 't1', '10:00:00'), work('a', 2, '10:01:00')]);

		expect(waitRange(entries, summaries, true)).toMatchObject({ since: at('10:00:00'), until: null });
	});

	it('shows no number when the shown items prove no start', () => {
		expect(waitRange([], summaries, true)).toBeNull();
		expect(waitRange(present([work('a', 2, '10:01:00')]), summaries, false)).toBeNull();
	});
});
