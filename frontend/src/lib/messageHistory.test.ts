import { describe, expect, it } from 'vitest';
import { mergeMessageDelivery } from './stores/threads';
import { compareMessageOrder, mergeChronologicalMessageHistory, mergeFetchedHistoryRows } from './messageHistory';

function message(index: number, overrides: Record<string, unknown> = {}) {
	return {
		id: `message-${String(index).padStart(3, '0')}`,
		thread_id: 'session-1',
		role: 'assistant' as const,
		content: `message ${index}`,
		created_at: new Date(Date.UTC(2026, 6, 27, 0, index)).toISOString(),
		...overrides,
	};
}

describe('mergeChronologicalMessageHistory', () => {
	it('keeps a >50-row first-page refetch, retained rows, and newest row in chronological order', () => {
		const retained = [
			message(1),
			message(2),
			message(101, { delivery_state: 'delivered', delivery_revision: 4 }),
		];
		const newestServerPage = Array.from({ length: 50 }, (_, offset) => {
			const index = offset + 52;
			return message(index, index === 101
				? { delivery_state: 'failed', delivery_revision: 3 }
				: {});
		});

		const merged = mergeChronologicalMessageHistory(
			retained,
			newestServerPage,
			mergeMessageDelivery,
		);

		expect(merged).toHaveLength(52);
		expect(merged.map((row) => row.id)).toEqual([
			'message-001',
			'message-002',
			...Array.from({ length: 50 }, (_, offset) => `message-${String(offset + 52).padStart(3, '0')}`),
		]);
		expect(merged.at(-1)?.id).toBe('message-101');
		expect(merged.find((row) => row.id === 'message-101')).toMatchObject({
			delivery_state: 'delivered',
			delivery_revision: 4,
		});
	});

	it('orders backdated rows first while preserving equal-time legacy order', () => {
		const sameTime = '2026-07-27T00:04:00.000Z';
		const merged = mergeChronologicalMessageHistory(
			[
				message(4, { created_at: sameTime }),
				message(3, { created_at: '2026-07-27T00:05:00.000Z' }),
			],
			[
				message(1, { created_at: sameTime }),
				message(2, { created_at: '2026-07-27T00:03:00.000Z' }),
			],
			mergeMessageDelivery,
		);

		expect(merged.map((row) => row.id)).toEqual([
			'message-002', 'message-004', 'message-001', 'message-003',
		]);
	});

	it('uses durable sequence before conflicting created_at for canonical rows', () => {
		const merged = mergeChronologicalMessageHistory(
			[
				message(1, { id: 'current-output', created_at: '2026-07-27T00:10:00.000Z', seq: 40 }),
				message(2, { id: 'second-input', created_at: '2026-07-27T00:02:00.000Z', seq: 42 }),
			],
			[
				message(3, { id: 'first-input', created_at: '2026-07-27T00:01:00.000Z', seq: 41 }),
			],
			mergeMessageDelivery,
		);

		expect(merged.map((row) => row.id)).toEqual([
			'current-output', 'first-input', 'second-input',
		]);
	});

	it('uses durable sequence rather than random IDs for equal timestamps', () => {
		const sameTime = '2026-07-27T00:04:00.000Z';
		const merged = mergeChronologicalMessageHistory(
			[
				message(4, { id: 'a-random-id', created_at: sameTime, seq: 3 }),
				message(5, { id: 'z-random-id', created_at: sameTime, seq: 4 }),
			],
			[
				message(1, { id: 'y-random-id', created_at: sameTime, seq: 1 }),
				message(2, { id: 'b-random-id', created_at: sameTime, seq: 2 }),
			],
			mergeMessageDelivery,
		);

		expect(merged.map((row) => row.id)).toEqual([
			'y-random-id', 'b-random-id', 'a-random-id', 'z-random-id',
		]);
	});

	it('preserves existing order when equal-time legacy rows have no sequence', () => {
		const sameTime = '2026-07-27T00:04:00.000Z';
		const merged = mergeChronologicalMessageHistory(
			[
				message(1, { id: 'z-legacy-id', created_at: sameTime, seq: null }),
				message(2, { id: 'a-legacy-id', created_at: sameTime }),
			],
			[message(3, { id: 'b-legacy-id', created_at: sameTime, seq: null })],
			mergeMessageDelivery,
		);

		expect(merged.map((row) => row.id)).toEqual([
			'z-legacy-id', 'a-legacy-id', 'b-legacy-id',
		]);
	});

	it('keeps an overlapping older page unique after a first-page refetch', () => {
		const newest = Array.from({ length: 50 }, (_, offset) => message(offset + 51));
		const older = Array.from({ length: 50 }, (_, offset) => message(offset + 1));
		older[0] = message(1, { delivery_state: 'delivered', delivery_revision: 2 });
		const staleOlder = Array.from({ length: 50 }, (_, offset) => message(offset + 1));
		staleOlder[0] = message(1, { delivery_state: 'failed', delivery_revision: 1 });

		let rows = mergeChronologicalMessageHistory([], newest, mergeMessageDelivery);
		rows = mergeChronologicalMessageHistory(rows, older, mergeMessageDelivery);
		rows = mergeChronologicalMessageHistory(rows, newest, mergeMessageDelivery);
		rows = mergeChronologicalMessageHistory(rows, staleOlder, mergeMessageDelivery);

		expect(rows).toHaveLength(100);
		expect(new Set(rows.map((row) => row.id)).size).toBe(100);
		expect(rows.at(0)?.id).toBe('message-001');
		expect(rows.at(-1)?.id).toBe('message-100');
		expect(rows.find((row) => row.id === 'message-001')).toMatchObject({
			delivery_state: 'delivered', delivery_revision: 2,
		});
	});

	it('keeps a canonical apply-now pending receipt unique and chronologically stable across reconnect hydration', () => {
		const pending = message(2, {
			id: 'canonical-pending', role: 'user' as const,
			metadata: { input_id: 'input-1' }, delivery_state: 'queued',
			delivery_error: null, delivery_revision: 3,
		});
		const output = message(3, { id: 'assistant-after-pending' });
		const merged = mergeChronologicalMessageHistory(
			[pending, output],
			[
				{ ...pending, delivery_revision: 2 },
				output,
			],
			mergeMessageDelivery,
		);

		expect(merged.map((row) => row.id)).toEqual([
			'canonical-pending', 'assistant-after-pending',
		]);
		expect(merged.filter((row) => row.id === 'canonical-pending')).toHaveLength(1);
		expect(merged[0]).toMatchObject({
			delivery_state: 'queued', delivery_revision: 3,
		});
	});

	it('adopts ownership from an equal-revision claim projection', () => {
		const queued = message(1, {
			delivery_state: 'queued', delivery_revision: 1, active_turn_id: null,
		});

		expect(mergeMessageDelivery(queued, {
			...queued,
			active_turn_id: 'turn-1',
		})).toMatchObject({
			delivery_state: 'queued',
			delivery_revision: 1,
			active_turn_id: 'turn-1',
		});
	});

	it('keeps delivered sequence when a delayed page still contains the checkpoint hold', () => {
		const delivered = message(1, {
			delivery_state: 'delivered', delivery_revision: 5, seq: 7,
			write_outcome: 'accepted', active_turn_id: null,
		});
		const held = { ...delivered, delivery_state: 'queued' as const,
			delivery_revision: 3, seq: null, write_outcome: 'proved_no_write' as const };
		const rows = mergeChronologicalMessageHistory([delivered], [held], mergeMessageDelivery);
		expect(rows).toHaveLength(1);
		expect(rows[0]).toMatchObject({ delivery_state: 'delivered', delivery_revision: 5, seq: 7 });
	});

	it('does not resurrect a retired owner or clear hold evidence from an older frame', () => {
		const held = message(1, {
			delivery_state: 'queued', delivery_revision: 3, active_turn_id: null,
			write_outcome: 'proved_no_write', delivery_error: 'checkpoint unavailable',
		});
		expect(mergeMessageDelivery(held, {
			...held, delivery_revision: 2, active_turn_id: 'retired-turn',
			write_outcome: null, delivery_error: null,
		})).toMatchObject({
			delivery_revision: 3, active_turn_id: null,
			write_outcome: 'proved_no_write', delivery_error: 'checkpoint unavailable',
		});
	});
});

describe('a queued message keeps the position its delivery earned', () => {
	it('sorts an unsequenced row after every sequenced one, not by its timestamp', () => {
		// Real thread 35c10de4, 2026-08-12. The user queued at 17:43:01 while a
		// turn was running; the reply landed at 17:43:20 and took seq 10; the
		// queue delivered afterwards and took seq 11. Comparing the pair by time
		// put the queue back above the reply -- the spot it was queued.
		const reply = { id: 'a', seq: 10, created_at: '2026-08-12T17:43:20.742858' };
		const queued = { id: 'b', seq: null, created_at: '2026-08-12T17:43:01.403900' };

		expect(compareMessageOrder(reply, queued)).toBeLessThan(0);
		expect(compareMessageOrder(queued, reply)).toBeGreaterThan(0);

		const ordered = [queued, reply].sort(compareMessageOrder).map((m) => m.id);
		expect(ordered).toEqual(['a', 'b']);
	});

	it('still orders two sequenced rows by sequence, never by time', () => {
		const later = { id: 'a', seq: 10, created_at: '2026-08-12T17:43:20.742858' };
		const delivered = { id: 'b', seq: 11, created_at: '2026-08-12T17:43:01.403900' };
		expect([delivered, later].sort(compareMessageOrder).map((m) => m.id)).toEqual(['a', 'b']);
	});

	it('falls back to time only when neither row has a sequence', () => {
		const first = { id: 'a', seq: null, created_at: '2026-08-12T17:43:01.000000' };
		const second = { id: 'b', seq: null, created_at: '2026-08-12T17:43:20.000000' };
		expect([second, first].sort(compareMessageOrder).map((m) => m.id)).toEqual(['a', 'b']);
	});
});

describe('merging a refetched page into a live row', () => {
	const liveRow = {
		id: 'step', thread_id: 'session-1', role: 'assistant', content: 'streamed answer',
		created_at: '2026-09-27T20:00:00Z', seq: 4, typewriterPos: 12, reasoning: 'why',
		metadata: {
			turn_id: 'turn-1', reasoning: 'why',
			tool_calls: [{ id: 'call-1', result: 'loaded output', result_state: 'loaded' }],
		},
	};
	const pageRow = {
		id: 'step', thread_id: 'session-1', role: 'assistant', content: 'persisted answer',
		created_at: '2026-09-27T20:00:00Z', seq: 4,
		metadata: { turn_id: 'turn-1', tool_calls: [{ id: 'call-1', result_state: 'deferred' }] },
	};


	it('never thins content a stale page snapshot could not have seen', () => {
		// The page was read before the Step settled and arrived after the frame.
		const stale = { ...pageRow, content: 'streamed' };
		const [row] = mergeFetchedHistoryRows(
			[{ ...liveRow, content: 'streamed answer' }], [stale],
		);
		expect(row.content).toBe('streamed answer');
	});

	it('takes content the page proves is richer', () => {
		const [row] = mergeFetchedHistoryRows(
			[{ ...liveRow, content: 'streamed' }], [{ ...pageRow, content: 'streamed answer' }],
		);
		expect(row.content).toBe('streamed answer');
	});

	it('keeps streaming content the page does not extend while the typewriter is active', () => {
		const [row] = mergeFetchedHistoryRows(
			[{ ...liveRow, content: 'streamed' }], [{ ...pageRow, content: 'other text' }],
		);
		expect(row.content).toBe('streamed');
	});

	it('never thins settled content a stale page snapshot could not have seen', () => {
		const [row] = mergeFetchedHistoryRows(
			[{ ...liveRow, content: 'streamed answer', typewriterPos: undefined }],
			[{ ...pageRow, content: 'streamed' }],
		);
		expect(row.content).toBe('streamed answer');
	});

	it('lets the server replace divergent content once streaming has settled', () => {
		const [row] = mergeFetchedHistoryRows(
			[{ ...liveRow, content: 'client-only text', typewriterPos: undefined }], [pageRow],
		);
		expect(row.content).toBe('persisted answer');
	});

	it('keeps output the page deferred and the live row already held', () => {
		const [row] = mergeFetchedHistoryRows([liveRow], [pageRow]);
		expect(row.metadata.tool_calls).toEqual([
			{ id: 'call-1', result: 'loaded output', result_state: 'loaded' },
		]);
		expect(row.metadata.reasoning).toBe('why');
		expect(row.typewriterPos).toBe(12);
	});

	it('keeps a call the page omits instead of erasing the live block', () => {
		const omitted = { ...pageRow, metadata: { turn_id: 'turn-1' } };
		const [row] = mergeFetchedHistoryRows([liveRow], [omitted]);
		expect(row.metadata.tool_calls).toEqual([
			{ id: 'call-1', result: 'loaded output', result_state: 'loaded' },
		]);
	});

	it('keeps a call an older page snapshot states as an empty list', () => {
		const emptied = { ...pageRow, metadata: { turn_id: 'turn-1', tool_calls: [] } };
		const [row] = mergeFetchedHistoryRows([liveRow], [emptied]);
		expect(row.metadata.tool_calls).toEqual([
			{ id: 'call-1', result: 'loaded output', result_state: 'loaded' },
		]);
	});

	it('keeps a live-only call alongside calls the page states', () => {
		const second = { id: 'call-2', result: 'second output', result_state: 'loaded' };
		const page = {
			...pageRow,
			metadata: {
				turn_id: 'turn-1',
				tool_calls: [{ id: 'call-1', result_state: 'deferred' }],
			},
		};
		const [row] = mergeFetchedHistoryRows(
			[{ ...liveRow, metadata: { ...liveRow.metadata, tool_calls: [liveRow.metadata.tool_calls[0], second] } }],
			[page],
		);
		expect(row.metadata.tool_calls).toEqual([
			{ id: 'call-1', result: 'loaded output', result_state: 'loaded' },
			second,
		]);
	});

});
