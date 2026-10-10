import { get } from 'svelte/store';
import { beforeEach, describe, expect, it } from 'vitest';
import { activeThreadId, messages } from '../stores/threads';
import { compareMessageOrder } from '../messageHistory';
import {
	handleContextUsageUpdated,
	handleToolStart,
	handleTurnActivity,
	upsertMessageByServerOrder,
} from './handlersTurns';
import { claimDisplayedThread } from '../stores/displayedThreads';
import { _resetTurnStateForTests, getTurnState } from './turnState';
import { on } from './events';
import { clearAllContextUsage, setContextUsage, getContextUsage } from '../stores/contextUsageByThread';

beforeEach(() => {
	activeThreadId.set('viewed');
	clearAllContextUsage();
	_resetTurnStateForTests();
});

describe('live message frames carry their transcript position', () => {
	it('keeps a streamed reply above a later user message that has a sequence', () => {
		// The bug: `step_text` frames omitted `seq`, so a streaming reply had no
		// position and was ordered by timestamp. The moment the user's own row
		// was confirmed WITH a sequence, the two were compared on mixed evidence
		// and the reply could land below the message that answered it -- curing
		// itself on reload, because the REST fetch has always carried sequences.
		const reply = { id: 'a', thread_id: 's', role: 'assistant', content: 'hi', seq: 10,
			created_at: '2026-08-12T21:00:00.000000' } as any;
		const mine = { id: 'b', thread_id: 's', role: 'user', content: 'q', seq: 11,
			created_at: '2026-08-12T20:59:30.000000' } as any;
		const { rows } = upsertMessageByServerOrder([mine], reply);
		expect(rows.map((r: any) => r.id)).toEqual(['a', 'b']);
	});

	it('orders a live cutover terminal exactly where reload puts it', () => {
		const earlier = { id: 'a', thread_id: 's', role: 'assistant', content: 'reply', seq: 10,
			created_at: '2026-08-12T21:00:00.000000' } as any;
		const terminal = { id: 'b', thread_id: 's', role: 'system', content: 'cutover ended', seq: 11,
			created_at: '2026-08-12T20:58:00.000000' } as any;
		const nextUser = { id: 'c', thread_id: 's', role: 'user', content: 'next', seq: 12,
			created_at: '2026-08-12T20:59:00.000000' } as any;

		const live = upsertMessageByServerOrder([earlier, nextUser], terminal).rows;
		const reload = [earlier, terminal, nextUser];
		expect(live.map((row: any) => row.id)).toEqual(reload.map((row: any) => row.id));
	});

	it('lands a delayed earlier-sequence row above rows it already rendered', () => {
		// A `message_added` for seq 11 can arrive after seq 12 (reconnect,
		// reordering, a slow settle broadcast). The durable position is the
		// sequence, so the late row must move up into place, not append.
		const seq10 = { id: 'a', thread_id: 's', role: 'assistant', content: 'reply', seq: 10,
			created_at: '2026-08-12T21:00:00.000000' } as any;
		const seq12 = { id: 'c', thread_id: 's', role: 'user', content: 'next', seq: 12,
			created_at: '2026-08-12T21:00:02.000000' } as any;
		const delayed11 = { id: 'b', thread_id: 's', role: 'user', content: 'queued mid-turn', seq: 11,
			created_at: '2026-08-12T20:58:00.000000' } as any;

		const rows = upsertMessageByServerOrder(
			upsertMessageByServerOrder([seq10], seq12).rows,
			delayed11,
		).rows;

		expect(rows.map((row: any) => row.id)).toEqual(['a', 'b', 'c']);
	});

	it('breaks equal timestamps by sequence, identically live and on reload', () => {
		const SAME_TS = '2026-08-12T21:00:00.000000';
		const rows = [
			{ id: 'late', thread_id: 's', role: 'user', content: 'x', seq: 14,
				created_at: SAME_TS } as any,
			{ id: 'mid', thread_id: 's', role: 'user', content: 'y', seq: 13,
				created_at: SAME_TS } as any,
			{ id: 'early', thread_id: 's', role: 'user', content: 'z', seq: 12,
				created_at: SAME_TS } as any,
		];
		const ordered = [...rows].sort(compareMessageOrder);
		expect(ordered.map((row: any) => row.id)).toEqual(['early', 'mid', 'late']);

		// The same three rows arriving live, one frame at a time, in the worst
		// order, must compose to the same transcript a reload would render.
		let live = [rows[2]] as any[];
		live = upsertMessageByServerOrder(live, rows[0]).rows;
		live = upsertMessageByServerOrder(live, rows[1]).rows;
		expect(live.map((row: any) => row.id)).toEqual(ordered.map((row: any) => row.id));
	});
});


describe('Turn lifecycle events', () => {
	it('publishes an admitted turn_activity for transcript recovery', () => {
		const seen: any[] = [];
		const off = on('turn_activity', data => seen.push(data));
		try {
			handleTurnActivity({
				type: 'turn_activity', thread_id: 'viewed',
				turn_id: 'turn-current', turn_order: 8,
			});
		} finally {
			off();
		}
		expect(seen).toHaveLength(1);
	});
});

describe('live context usage identity', () => {
	it('updates the viewed thread at the current safe boundary', () => {
		handleTurnActivity({
			type: 'turn_activity', thread_id: 'viewed',
			turn_id: 'turn-current', turn_order: 8,
		});

		handleContextUsageUpdated({
			type: 'context_usage_updated', thread_id: 'viewed',
			turn_id: 'turn-current', turn_order: 8,
			context_usage: { total_used: 90 },
		});

		expect(getContextUsage('viewed')?.total_used).toBe(90);
	});

	it('ignores a boundary update from an older Turn', () => {
		handleTurnActivity({
			type: 'turn_activity', thread_id: 'viewed',
			turn_id: 'turn-current', turn_order: 8,
		});
		setContextUsage('viewed', { total_used: 80 } as any);

		handleContextUsageUpdated({
			type: 'context_usage_updated', thread_id: 'viewed',
			turn_id: 'turn-old', turn_order: 7,
			context_usage: { total_used: 70 },
		});

		expect(getContextUsage('viewed')?.total_used).toBe(80);
	});

	it('retires the previous boundary receipt when a successor Turn starts', () => {
		handleTurnActivity({
			type: 'turn_activity', thread_id: 'viewed',
			turn_id: 'turn-before', turn_order: 8,
		});
		setContextUsage('viewed', { total_used: 80 } as any);

		handleTurnActivity({
			type: 'turn_activity', thread_id: 'viewed',
			turn_id: 'turn-after', turn_order: 9,
		});

		expect(getContextUsage('viewed')).toBeNull();
	});

	it('keeps the current boundary receipt on duplicate activity for the same Turn', () => {
		handleTurnActivity({
			type: 'turn_activity', thread_id: 'viewed',
			turn_id: 'turn-current', turn_order: 8,
		});
		setContextUsage('viewed', { total_used: 80 } as any);

		handleTurnActivity({
			type: 'turn_activity', thread_id: 'viewed',
			turn_id: 'turn-current', turn_order: 8,
		});

		expect(getContextUsage('viewed')?.total_used).toBe(80);
	});
});

describe('context usage is kept per displayed thread (#2269)', () => {
	it('stores a leaf-displayed subagent frame under its own thread, leaving the active one alone', () => {
		const release = claimDisplayedThread('worker');
		setContextUsage('viewed', { total_used: 80 } as any);

		handleContextUsageUpdated({
			type: 'context_usage_updated', thread_id: 'worker',
			turn_id: 'turn-w', turn_order: 3,
			context_usage: { total_used: 33 },
		});
		handleContextUsageUpdated({
			type: 'context_usage_updated', thread_id: 'viewed',
			turn_id: 'turn-v', turn_order: 8,
			context_usage: { total_used: 95 },
		});

		expect(getContextUsage('worker')?.total_used).toBe(33);
		expect(getContextUsage('viewed')?.total_used).toBe(95);
		release();
	});

	it('ignores a frame for a thread no panel displays', () => {
		handleContextUsageUpdated({
			type: 'context_usage_updated', thread_id: 'hidden',
			turn_id: 'turn-h', turn_order: 1,
			context_usage: { total_used: 10 },
		});
		expect(getContextUsage('hidden')).toBeNull();
	});

	it('a successor Turn on one thread retires only that thread\'s receipt', () => {
		const release = claimDisplayedThread('worker');
		setContextUsage('viewed', { total_used: 80 } as any);
		setContextUsage('worker', { total_used: 33 } as any);
		handleTurnActivity({ type: 'turn_activity', thread_id: 'worker', turn_id: 'w1', turn_order: 1 });
		handleTurnActivity({ type: 'turn_activity', thread_id: 'worker', turn_id: 'w2', turn_order: 2 });

		expect(getContextUsage('worker')).toBeNull();
		expect(getContextUsage('viewed')?.total_used).toBe(80);
		release();
	});
});

describe('turn frames carry visible-root lineage', () => {
	it('records the root a turn_activity frame names for a hidden subagent', () => {
		handleTurnActivity({
			type: 'turn_activity', thread_id: 'sub-cc',
			turn_id: 'turn-cc', turn_order: 4, visible_root_thread_id: 'parent-1',
		});

		expect(getTurnState('sub-cc').phase).toBe('active');
		expect(getTurnState('sub-cc').visibleRootThreadId).toBe('parent-1');
	});

	it('treats an explicit null root as truth, not as an absent field', () => {
		handleTurnActivity({
			type: 'turn_activity', thread_id: 'sub-cc',
			turn_id: 'turn-cc', turn_order: 4, visible_root_thread_id: 'parent-1',
		});
		handleTurnActivity({
			type: 'turn_activity', thread_id: 'sub-cc',
			turn_id: 'turn-cc', turn_order: 4, visible_root_thread_id: null,
		});

		expect(getTurnState('sub-cc').visibleRootThreadId).toBeNull();
	});

	it('keeps the recorded root when streaming frames re-admit without the field', () => {
		handleTurnActivity({
			type: 'turn_activity', thread_id: 'sub-cc',
			turn_id: 'turn-cc', turn_order: 4, visible_root_thread_id: 'parent-1',
		});
		// tool_start carries identity but no lineage.
		handleToolStart({
			type: 'tool_start', thread_id: 'sub-cc',
			turn_id: 'turn-cc', turn_order: 4,
			tool_id: 't1', name: 'read', hint: 'read',
		});

		expect(getTurnState('sub-cc').visibleRootThreadId).toBe('parent-1');
	});
});
