import { describe, it, expect } from 'vitest';
import {
	buildMessageListPresentation,
	buildTranscriptPresentation,
	buildVisibleMessages,
	passiveContextEventLabel,
} from './messageListModel';

import { artifact, compact, tool } from '$lib/__tests__/fixtures/focusPauseProof';
import { canMergeRows, sourceMembers } from './contextEventModel';

const THREAD = 'thread-chips';

/** One backend digest, as `passive_changes.materialize_pending_digest` writes it. */
function backendDigest(
	id: string,
	events: Array<Record<string, unknown>>,
	overrides: Record<string, unknown> = {},
): Record<string, unknown> {
	return {
		id,
		thread_id: THREAD,
		role: 'system',
		content: `[PASSIVE CONTEXT DIGEST]\n${events.length} context updates`,
		created_at: '2026-09-18T21:24:00',
		delivery_state: 'delivered',
		metadata: {
			type: 'context_event',
			event_type: 'context.digest',
			event_disposition: 'passive',
			summary: `${events.length} context updates`,
			context_events: events,
		},
		...overrides,
	};
}

function ev(id: string, summary: string, extra: Record<string, unknown> = {}) {
	return { id, summary, created_at: '2026-09-18T21:24:00', ...extra };
}

function wake(id: string, summary: string): Record<string, unknown> {
	return {
		id,
		thread_id: THREAD,
		role: 'system',
		content: `[EVENT: plan.child_returned]\nSummary: ${summary}`,
		created_at: '2026-09-18T21:30:00',
		delivery_state: 'delivered',
		metadata: {
			type: 'context_event',
			event_type: 'plan.child_returned',
			event_disposition: 'wake',
			summary,
		},
	};
}

function detailSummaries(row: Record<string, any>): string[] {
	return (row.metadata.context_events as Array<{ summary: string }>).map((e) => e.summary);
}

describe('grouped context chips', () => {
	it('flattens backend digests one level: header count equals the detail rows', () => {
		const rows = [
			backendDigest('d1', [
				ev('a', 'Context source updated: AGENTS.md'),
				ev('b', 'Context source updated: PROJECT.md'),
				ev('c', 'Project context updated'),
				ev('d', 'Thread context updated'),
			]),
			wake('w1', 'Delegated work returned'),
			backendDigest('d2', [ev('e', 'Current time: 2026-09-18 15:37 PDT'), ev('f', 'Plan paused')]),
		];

		const visible = buildVisibleMessages(rows);

		expect(visible).toHaveLength(1);
		const group = visible[0] as Record<string, any>;
		expect(group.metadata.summary).toBe('7 context updates');
		expect(passiveContextEventLabel(group)).toBe('7 context updates');
		expect(detailSummaries(group)).toEqual([
			'Context source updated: AGENTS.md',
			'Context source updated: PROJECT.md',
			'Project context updated',
			'Thread context updated',
			'Delegated work returned',
			'Current time: 2026-09-18 15:37 PDT',
			'Plan paused',
		]);
		expect(detailSummaries(group).some((s) => /context updates/.test(s))).toBe(false);
	});

	it('shows each event\'s own chip line, never the text written for the agent', () => {
		const rows = [
			backendDigest('d1', [
				ev('a', 'You were replaced on Step 1: stop working on it.', { chip: 'Step handed to another worker' }),
				ev('b', 'Context source updated: A.md'),
			]),
			wake('w1', 'Delegated work returned'),
			wake('w2', 'Decision answered'),
		];

		const group = buildVisibleMessages(rows)[0] as Record<string, any>;

		expect(detailSummaries(group)).toEqual([
			'Step handed to another worker',
			'Context source updated: A.md',
			'Delegated work returned',
			'Decision answered',
		]);
	});

	it('gives routine agent-only state no row, and counts only the rows it shows', () => {
		const status = ev('s', 'Current plan status: {"plan":{"id":"p"}}', {
			chip: null, change_kind: 'plan.status',
		});
		const rows = [
			backendDigest('d1', [status, ev('a', 'Plan paused')]),
			wake('w1', 'Delegated work returned'),
			backendDigest('d2', [{ ...status, id: 's2' }]),
			wake('w2', 'Decision answered'),
		];

		const visible = buildVisibleMessages(rows);

		// The status-only digest is not a row at all, so it cannot count toward a run.
		expect(visible).toHaveLength(1);
		const group = visible[0] as Record<string, any>;
		expect(group.metadata.summary).toBe('3 context updates');
		expect(detailSummaries(group)).toEqual([
			'Plan paused', 'Delegated work returned', 'Decision answered',
		]);
		expect(JSON.stringify(visible)).not.toContain('Current plan status');
	});

	it('renders no row for a digest that carries only agent-only state', () => {
		const only = backendDigest('d1', [
			ev('s', 'Current plan status: {}', { chip: null, change_kind: 'plan.status' }),
		]);
		expect(buildVisibleMessages([only])).toEqual([]);
	});

	it('shows a digest stored before the chip field by its summary', () => {
		const stored = backendDigest('d1', [
			ev('s', 'Current plan status: {"plan":{}}', { change_kind: 'plan.status' }),
			ev('a', 'Plan paused'),
		]);

		const [row] = buildVisibleMessages([stored]) as Array<Record<string, any>>;

		expect(row.metadata.summary).toBe('2 context updates');
		expect(detailSummaries(row)).toEqual(['Current plan status: {"plan":{}}', 'Plan paused']);
	});

	it('keeps the count honest for a lone digest that mixes shown and hidden rows', () => {
		const lone = backendDigest('d1', [
			ev('s', 'Current plan status: {}', { chip: null }),
			ev('a', 'Plan paused'),
			ev('b', 'Review requested'),
		]);

		const [row] = buildVisibleMessages([lone]) as Array<Record<string, any>>;

		expect(row.metadata.summary).toBe('2 context updates');
		expect(detailSummaries(row)).toEqual(['Plan paused', 'Review requested']);
	});

	it('flattens the pending digest the same way', () => {
		const pending = [
			backendDigest('d1', [ev('a', 'Plan paused'), ev('b', 'Review requested')], {
				delivery_state: 'queued',
			}),
			backendDigest('d2', [ev('c', 'Project context updated')], { delivery_state: 'queued' }),
		];
		for (const row of pending) {
			(row.metadata as Record<string, unknown>).event_disposition = 'passive';
		}

		const { pendingContextDigest, visibleMessages } = buildMessageListPresentation(pending);

		expect(visibleMessages).toEqual([]);
		const digest = pendingContextDigest as Record<string, any>;
		expect(digest.metadata.summary).toBe('3 context updates');
		expect(detailSummaries(digest)).toEqual([
			'Plan paused', 'Review requested', 'Project context updated',
		]);
	});
});

describe('a correction note is for the agent only', () => {
	const NOTE = 'Correction recorded on your work (message c1, sent 2026-10-16 13:05 PDT): "Wrong file."\n'
		+ 'Proposed tier: none yet; you set it.';
	const note = ev('n', NOTE, {
		chip: null, object_kind: 'thread_context', change_kind: 'note',
		details: { kind: 'correction_note', note_key: 'message:c1', correction_subjects: ['message:c1'], reoffer_unwritten: true },
	});
	/** The lone-note digest as `materialize_pending_digest` stores it: its summary is the note's first line. */
	function noteDigest(id: string, state: string, events = [note]) {
		return backendDigest(id, events, {
			content: `[PASSIVE CONTEXT DIGEST]\n${NOTE}`,
			delivery_state: state,
			metadata: {
				type: 'context_event', event_type: 'context.digest', event_disposition: 'passive',
				summary: events.length === 1 ? NOTE.split('\n')[0] : `${events.length} context updates`,
				context_events: events,
			},
		});
	}

	it('shows no row, chip line or preview in the transcript, Focus or not', () => {
		const rows = [
			{ id: 'ask', thread_id: THREAD, role: 'user', content: 'Thanks.' },
			noteDigest('d1', 'delivered'),
			{ id: 'work', thread_id: THREAD, role: 'assistant', content: '', metadata: { turn_id: 't', tool_calls: [{ id: 'x' }] } },
		];
		const { visibleMessages, pendingContextDigest } = buildMessageListPresentation(rows);

		expect(visibleMessages.map((row) => row.id)).toEqual(['ask', 'work']);
		expect(pendingContextDigest).toBeNull();
		for (const focus of [true, false]) {
			expect(JSON.stringify(buildTranscriptPresentation(visibleMessages, focus))).not.toContain('Correction recorded');
		}
	});

	it('shows nothing while it waits to be delivered', () => {
		const { visibleMessages, pendingContextDigest } = buildMessageListPresentation([noteDigest('d1', 'queued')]);

		expect(visibleMessages).toEqual([]);
		expect(pendingContextDigest).toBeNull();
	});

	it('shows nothing for the hidden wake that carries it to an idle agent', () => {
		const wake = backendDigest('correction-wake:message:c1', [note], {
			content: `[PASSIVE CONTEXT DIGEST]\n${NOTE}`,
			delivery_state: 'queued',
			metadata: {
				type: 'context_event', event_type: 'context.digest', event_disposition: 'wake',
				summary: NOTE.split('\n')[0], context_events: [note],
			},
		});
		const { visibleMessages, pendingContextDigest } = buildMessageListPresentation([wake]);

		expect(visibleMessages).toEqual([]);
		expect(pendingContextDigest).toBeNull();
	});

	it('leaves the rest of a mixed digest and counts only what it shows', () => {
		const mixed = noteDigest('d1', 'delivered', [note, ev('a', 'Plan paused')]);
		const [row] = buildMessageListPresentation([mixed]).visibleMessages as Array<Record<string, any>>;

		expect(row.metadata.summary).toBe('Plan paused');
		expect(passiveContextEventLabel(row)).toBe('Plan paused');
		expect(JSON.stringify(row.metadata)).not.toContain('Correction recorded');
	});
});

describe('Focus-mode transcript runs', () => {
	it('uses only an explicit for_user mark for assistant content', () => {
		const rows = [
			{ id: 'user', thread_id: THREAD, role: 'user', content: 'Please investigate.', active_turn_id: 'turn-1' },
			{ id: 'work', thread_id: THREAD, role: 'assistant', content: 'Background work', metadata: { turn_id: 'turn-1', tool_calls: [{ id: 'tool-1' }] } },
			{ id: 'reply', thread_id: THREAD, role: 'assistant', content: 'Ordinary final text', metadata: { turn_id: 'turn-1' } },
			wake('wake', 'Background worker returned'),
			{ id: 'update', thread_id: THREAD, role: 'assistant', content: 'The deployment window moved.', metadata: { turn_id: 'turn-2', for_user: true } },
		];

		expect(buildTranscriptPresentation(rows, true)).toMatchObject([
			{ kind: 'message', message: { id: 'user' } },
			{ kind: 'fold', items: [{ message: { id: 'work' } }, { message: { id: 'reply' } }, { message: { id: 'wake' } }] },
			{ kind: 'message', message: { id: 'update' } },
		]);
	});

	it('keeps work-fold identity stable as ordinary running work appends', () => {
		const initial = buildTranscriptPresentation([
			{ id: 'work-1', thread_id: THREAD, role: 'assistant', content: '', metadata: { turn_id: 'background', tool_calls: [{ id: 'tool-1' }] } },
		], true);
		const appended = buildTranscriptPresentation([
			{ id: 'work-1', thread_id: THREAD, role: 'assistant', content: '', metadata: { turn_id: 'background', tool_calls: [{ id: 'tool-1' }] } },
			{ id: 'work-2', thread_id: THREAD, role: 'assistant', content: '', metadata: { turn_id: 'background', tool_calls: [{ id: 'tool-2' }] } },
		], true);

		expect(initial[0]).toMatchObject({ kind: 'fold', id: 'run:open' });
		expect(appended[0]).toMatchObject({ kind: 'fold', id: 'run:open' });
	});

	it('folds a pending context event while preserving its pending-region boundary', () => {
		const pendingContext = backendDigest('pending', [ev('e', 'Plan paused')], { delivery_state: 'queued' });
		expect(buildTranscriptPresentation([pendingContext], true)).toMatchObject([
			{ kind: 'fold', items: [{ message: { id: 'pending' } }] },
		]);
	});

	it('leaves the existing complete transcript ungrouped when Focus mode is off', () => {
		const rows = [
			{ id: 'user', thread_id: THREAD, role: 'user', content: 'Please investigate.', active_turn_id: 'turn-1' },
			wake('wake', 'Background worker returned'),
		];

		expect(buildTranscriptPresentation(rows, false)).toEqual([
			{ kind: 'message', id: 'message-user', message: rows[0], showActivity: true, showContent: true },
			{ kind: 'message', id: 'message-wake', message: rows[1], showActivity: true, showContent: true },
		]);
	});
});

describe('Focus review boundary', () => {
	for (const kind of ['compact', 'tool', 'artifact'] as const) {
		for (const reverse of [false, true]) it(`F3 ${kind} ordinary state equivalence ${reverse ? 'reverse' : 'forward'}`, () => {
			const rows = [1, 2, 3].map((seq) => kind === 'compact' ? compact(`c${seq}`, seq, 'p')
				: kind === 'tool' ? tool(`t${seq}`, seq, 'p') : artifact(`a${seq}`, seq));
			const states = reverse ? ['delivered', null, undefined] : [undefined, null, 'delivered'];
			rows.forEach((row, index) => { if (states[index] === undefined) delete row.delivery_state;
				else row.delivery_state = states[index]; });
			const [group, ...rest] = buildVisibleMessages(rows);
			expect(rest).toEqual([]);
			expect(sourceMembers(group)).toEqual(rows);
			rows.forEach((row, index) => expect(sourceMembers(group)[index]).toBe(row));
			expect(sourceMembers(group).map((row) => row.delivery_state)).toEqual(states);
			expect(group.activity_weight).toBe(kind === 'tool' ? 3 : 0);
			expect(Object.hasOwn(rows[reverse ? 2 : 0], 'delivery_state')).toBe(false);
		});
	}
	for (const state of ['queued', 'ambiguous', 'failed', 'cancelled'] as const)
		it(`F3 ${state} rows remain separate in both directions and together`, () => {
			const ordinary = compact('ordinary', 1, 'p');
			const adverse = { ...compact('adverse', 2, 'p'), delivery_state: state };
			expect(canMergeRows(ordinary, adverse)).toBe(false);
			expect(canMergeRows(adverse, ordinary)).toBe(false);
			expect(canMergeRows(adverse, { ...adverse, id: 'another', seq: 3 })).toBe(false);
		});
	it('F3 optimistic ordinary rows cannot share a settled aggregate', () => {
		const ordinary = compact('ordinary', 1, 'p');
		const optimistic = { ...compact('optimistic', 2, 'p'), _optimistic: true };
		expect(canMergeRows(ordinary, optimistic)).toBe(false);
		expect(canMergeRows(optimistic, ordinary)).toBe(false);
	});
});
