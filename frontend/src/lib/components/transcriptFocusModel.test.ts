import { describe, expect, it } from 'vitest';

import {
	buildTranscriptPresentation,
	countVisibleEntries,
	isRunExpanded,
	OPEN_RUN_KEY,
	runTokens,
	toggleRun,
} from './transcriptFocusModel';

function assistant(message: Record<string, unknown>) {
	return {
		id: 'assistant', thread_id: 'thread', role: 'assistant', content: 'ordinary output',
		metadata: { turn_id: 'turn-2' }, ...message,
	};
}

describe('Focus transcript presentation', () => {

	it('hides unmarked running assistant text, reasoning, and tools', () => {
		const [entry] = buildTranscriptPresentation([
			assistant({
				id: 'running', reasoning: 'checking',
				metadata: { turn_id: 'turn-2', tool_calls: [{ id: 'tool-1' }] },
			}),
		], true);

		expect(entry).toMatchObject({
			kind: 'fold',
			items: [{ message: {
				id: 'running', content: 'ordinary output', reasoning: 'checking',
				metadata: { tool_calls: [{ id: 'tool-1' }] },
			} }],
		});
	});

	it('hides activity rows and agent inputs with ordinary running work', () => {
		const entries = buildTranscriptPresentation([
			assistant({ id: 'before' }),
			{ id: 'child', thread_id: 'thread', role: 'system', content: 'child started', metadata: { type: 'subagent_ref' } },
			{ id: 'context', thread_id: 'thread', role: 'system', content: 'context loaded', metadata: { type: 'context_event' } },
			{ id: 'agent', thread_id: 'thread', role: 'user', content: 'continue', metadata: { sender_kind: 'agent' } },
			assistant({ id: 'after' }),
		], true);

		expect(entries).toMatchObject([{ kind: 'fold', items: [
			{ message: { id: 'before' } },
			{ message: { id: 'child' } },
			{ message: { id: 'context' } },
			{ message: { id: 'agent' } },
			{ message: { id: 'after' } },
		] }]);
	});

	it('omits usage-recording failure receipts while retaining other activity receipts', () => {
		const entries = buildTranscriptPresentation([
			{
				id: 'usage-failure', thread_id: 'thread', role: 'system',
				content: 'Usage recording failed',
				metadata: { type: 'activity_receipt', event_type: 'usage.recording_failed' },
			},
			{
				id: 'model-switch', thread_id: 'thread', role: 'system',
				content: 'Switched model',
				metadata: { type: 'activity_receipt', event_type: 'model.switched' },
			},
		], true);

		expect(entries).toMatchObject([{ kind: 'fold', items: [{ message: { id: 'model-switch' } }] }]);
	});

	it('keeps a subagent_done notice with the work on both sides of it', () => {
		const entries = buildTranscriptPresentation([
			assistant({ id: 'before-done' }),
			{
				id: 'done', thread_id: 'thread', role: 'system',
				content: 'Subagent “Worker” closed.', metadata: { type: 'subagent_done' },
			},
			assistant({ id: 'after-done' }),
		], true);

		expect(entries).toHaveLength(1);
		expect(entries).toMatchObject([{ kind: 'fold', items: [
			{ message: { id: 'before-done' } },
			{ message: { id: 'done' } },
			{ message: { id: 'after-done' } },
		] }]);
	});

	it('shows explicit for_user content while folding activity attached to it', () => {
		const entries = buildTranscriptPresentation([
			assistant({
				id: 'update', content: 'Your update', reasoning: 'checked',
				metadata: { turn_id: 'turn-2', for_user: true, tool_calls: [{ id: 'tool-1' }] },
			}),
		], true);

		expect(entries).toMatchObject([
			{ kind: 'fold', items: [{ message: { id: 'update' }, showActivity: true, showContent: false }] },
			{ kind: 'message', message: { id: 'update', content: 'Your update', reasoning: undefined, metadata: { for_user: true, tool_calls: [] } }, showActivity: false, showContent: true },
		]);
	});

	it('keeps human messages and actionable system notices visible', () => {
		const entries = buildTranscriptPresentation([
			{ id: 'human', thread_id: 'thread', role: 'user', content: 'Please investigate.', metadata: {} },
			assistant({ id: 'work' }),
			{ id: 'interrupt', thread_id: 'thread', role: 'system', content: 'Interrupted', metadata: { type: 'system_interrupt' } },
		], true);

		expect(entries).toMatchObject([
			{ kind: 'message', message: { id: 'human' } },
			{ kind: 'fold', items: [{ message: { id: 'work' } }] },
			{ kind: 'message', message: { id: 'interrupt' } },
		]);
	});

	it('folds pending activity-only system rows', () => {
		expect(buildTranscriptPresentation([{
			id: 'context', thread_id: 'thread', role: 'system', content: 'digest',
			metadata: { type: 'context_event' },
		}], true)).toMatchObject([{ kind: 'fold', items: [{ message: { id: 'context' } }] }]);
	});

	it('counts shown messages, notices, folds, and update entries but not dividers', () => {
		const entries = [
			{ kind: 'message', id: 'shown', showContent: true },
			{ kind: 'message', id: 'activity', showContent: false },
			{ kind: 'fold', id: 'fold' },
			{ kind: 'update', id: 'update' },
			{ kind: 'divider', id: 'divider' },
		] as never;

		expect(countVisibleEntries(entries)).toBe(3);
	});

	it('preserves the complete transcript when Focus is off', () => {
		const rows = [
			assistant({ id: 'work' }),
			{ id: 'agent', thread_id: 'thread', role: 'user', content: 'continue', metadata: { sender_kind: 'agent' } },
		];

		expect(buildTranscriptPresentation(rows, false)).toEqual([
			{ kind: 'message', id: 'message-work', message: rows[0], showActivity: true, showContent: true },
			{ kind: 'message', id: 'message-agent', message: rows[1], showActivity: true, showContent: true },
		]);
	});
});

describe('Focus runs while a Turn is live', () => {
	it('keeps turnless activity interleaved with a live Turn in one live fold', () => {
		const entries = buildTranscriptPresentation([
			assistant({ id: 'live-start', metadata: { turn_id: 'turn-2' } }),
			{ id: 'context', thread_id: 'thread', role: 'system', content: 'context loaded', metadata: { type: 'context_event' } },
			{ id: 'child', thread_id: 'thread', role: 'system', content: 'child started', metadata: { type: 'subagent_ref' } },
			{ id: 'agent', thread_id: 'thread', role: 'user', content: 'continue', metadata: { sender_kind: 'agent' } },
			assistant({ id: 'live-end', metadata: { turn_id: 'turn-2' } }),
		], true, { turnId: 'turn-2' });

		expect(entries).toMatchObject([{
			kind: 'fold', live: true, turnIds: ['turn-2'], items: [
				{ message: { id: 'live-start' } }, { message: { id: 'context' } },
				{ message: { id: 'child' } }, { message: { id: 'agent' } },
				{ message: { id: 'live-end' } },
			],
		}]);
	});

	it('keeps a turnless first persisted row with the live Turn after a human message', () => {
		const entries = buildTranscriptPresentation([
			{ id: 'human', thread_id: 'thread', role: 'user', content: 'Please investigate.', metadata: {} },
			{ id: 'context', thread_id: 'thread', role: 'system', content: 'context loaded', metadata: { type: 'context_event' } },
			assistant({ id: 'live', metadata: { turn_id: 'turn-2' } }),
		], true, { turnId: 'turn-2' });

		expect(entries).toMatchObject([
			{ kind: 'message', message: { id: 'human' } },
			{ kind: 'fold', live: true, turnIds: ['turn-2'], items: [
				{ message: { id: 'context' } }, { message: { id: 'live' } },
			] },
		]);
	});

	it('holds earlier-Turn work and the running Turn in one live run, earlier Turns first', () => {
		const entries = buildTranscriptPresentation([
			assistant({ id: 'wake', metadata: { turn_id: 'turn-1' } }),
			assistant({ id: 'live', metadata: { turn_id: 'turn-2' } }),
		], true, { turnId: 'turn-2' });

		expect(entries).toHaveLength(1);
		expect(entries).toMatchObject([{
			kind: 'fold', id: OPEN_RUN_KEY, live: true, turnIds: ['turn-1', 'turn-2'],
			items: [{ message: { id: 'wake' } }, { message: { id: 'live' } }],
		}]);
	});

	it('appends an empty live run before work persists, including an unnamed live Turn', () => {
		expect(buildTranscriptPresentation([], true, { turnId: 'turn-2' })).toMatchObject([{
			kind: 'fold', id: OPEN_RUN_KEY, items: [], turnIds: ['turn-2'], live: true,
		}]);
		expect(buildTranscriptPresentation([], true, { turnId: null })).toMatchObject([{
			kind: 'fold', id: OPEN_RUN_KEY, items: [], turnIds: [], live: true,
		}]);
	});

	it('keeps an opened live run expanded after its Turn finishes', () => {
		const rows = [
			assistant({ id: 'wake', metadata: { turn_id: 'turn-1' } }),
			assistant({ id: 'live', metadata: { turn_id: 'turn-2' } }),
		];
		const live = buildTranscriptPresentation(rows, true, { turnId: 'turn-2' });
		expect(live).toMatchObject([{ kind: 'fold', live: true }]);
		const opened = toggleRun(runTokens(live, 0, true), new Set());
		expect(opened).toEqual(new Set(['row:wake', 'row:live', 'after:start']));

		const finished = buildTranscriptPresentation(rows, true);
		expect(finished).toMatchObject([{ kind: 'fold', live: false, turnIds: ['turn-1', 'turn-2'] }]);
		const tokens = runTokens(finished, 0, true);
		expect(isRunExpanded(tokens, opened)).toBe(true);
		expect(toggleRun(tokens, opened)).toEqual(new Set());
	});
});
