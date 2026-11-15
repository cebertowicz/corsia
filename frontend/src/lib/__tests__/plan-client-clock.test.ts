import { describe, expect, it } from 'vitest';
import { measureServerClock, onClientClock } from '$lib/api/planClock';
import { liveSeconds } from '$lib/components/plans/nodeDetailsText';

const asOf = '2026-11-14T12:00:00.123456+00:00';
const serverNow = Date.parse('2026-11-14T12:00:00.123Z');

type Span = { settled_seconds: number; open_starts: string[] };

// A decoded body never shares objects, so each amount is its own.
const amount = () => ({
	execution: { settled_seconds: 30, open_starts: ['2026-11-14T11:58:00+00:00'] } as Span,
	waiting: { settled_seconds: 0, open_starts: [] } as Span,
});
const node = () => ({
	node_cost: { own: amount(), rolled: amount() },
	initiative: { cost: amount() },
	commitment_cost: { elapsed_seconds: { settled_seconds: 0, open_starts: ['2026-11-14T11:50:00+00:00'] } as Span },
});

describe('open spans on the browser clock', () => {
	it.each([
		['runs ahead of the server', 5 * 60_000],
		['runs behind the server', -5 * 60_000],
		['agrees with the server', 0],
	])('counts the same live time when the browser clock %s', (_label, skew) => {
		const receivedAt = serverNow + skew;
		measureServerClock(asOf, receivedAt);
		const listed = { plans: [{ nodes: [node()] }] };
		onClientClock(listed.plans);
		// A single-node read later in the same session uses the offset the last plan read measured.
		const opened = node();
		onClientClock(opened);
		// One minute after arrival, on the browser's own clock.
		const now = receivedAt + 60_000;
		for (const read of [listed.plans[0].nodes[0], opened]) {
			expect(liveSeconds(read.node_cost.rolled.execution, now)).toBe(30 + 180);
			expect(liveSeconds(read.initiative.cost.execution, now)).toBe(30 + 180);
			expect(liveSeconds(read.commitment_cost.elapsed_seconds, now)).toBe(660);
		}
	});
});
