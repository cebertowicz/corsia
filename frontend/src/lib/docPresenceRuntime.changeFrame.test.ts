import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ServerComment } from '$lib/api/docs';
import { resetDocPresenceForTests, type DocActivityFrame } from './docPresence';
import { attachDocPresenceSurface, startDocPresence } from './docPresenceRuntime';
import { docComments, loadComments } from './stores/docComments';
import { prepareCommentArrival, revealCommentArrival } from './stores/docCommentArrivals';
import { emit, on } from './ws/events';

const wire = vi.hoisted(() => ({ rows: [] as unknown[] }));
vi.mock('$lib/api/docs', async (original) => ({
	...await original<typeof import('$lib/api/docs')>(),
	fetchComments: async () => ({
		comments: wire.rows,
		feedback: { open_count: 0, owner_label: null, grace_closes_at: null, undelivered_batches: [] },
	}),
}));

const ART = 'art';
const REVEAL_MS = 8_000;
let cleanups: Array<() => void> = [];
let history: string[][] = [];

const serverComment = (id: string): ServerComment => ({
	id, artifact_id: ART, parent_id: null, author: 'agent-a', author_kind: 'agent', body: 'A comment.',
	anchor_text: 'x', from_off: 1, to_off: 4, review: 0, resolved: 0, acked: 0, orphaned: false,
	created_at: '2026-10-08T20:00:00Z', updated_at: '2026-10-08T20:00:00Z', replies: [],
} as ServerComment);

const shown = () => (get(docComments)[ART] ?? []).map((row) => row.id);
const everShown = (id: string) => history.some((ids) => ids.includes(id));
const settle = () => vi.advanceTimersByTimeAsync(0);

/** What the server sends when an agent creates a comment: the change frame, and later the activity frame. */
const changeFrame = (id: string) => emit('doc_comment_changed', {
	type: 'doc_comment_changed', artifact_id: ART, comment_id: id, author_thread_id: 'agent-a',
});
const activityFrame = (id: string): DocActivityFrame => ({
	type: 'doc_activity', artifact_id: ART, thread_id: 'agent-a', agent: 'agent-a', kind: 'comment',
	span: { from: 1, to: 4 }, comment_id: id, revision: null, at: new Date().toISOString(),
});

function attach(options: { viewing?: boolean; reducedMotion?: boolean } = {}) {
	cleanups.push(attachDocPresenceSurface(ART, 'surface', {
		isViewing: () => options.viewing ?? true,
		reducedMotion: () => options.reducedMotion ?? false,
		prepare: async (frame) => {
			const arrival = await prepareCommentArrival(ART, frame.comment_id);
			return { chars: 10, payload: arrival, delayMs: REVEAL_MS };
		},
		reveal: (_frame, payload) => revealCommentArrival(ART, payload as Awaited<ReturnType<typeof prepareCommentArrival>>),
	}));
}

/** The viewer reloads the comments on every change frame, as the document canvas does. */
const reloadOnChange = () => cleanups.push(on('doc_comment_changed', () => { void loadComments(ART); }));

beforeEach(() => {
	vi.useFakeTimers();
	resetDocPresenceForTests();
	docComments.set({});
	wire.rows = [serverComment('c1')];
	history = [];
	cleanups.push(docComments.subscribe(() => history.push(shown())));
});

afterEach(() => {
	for (const cleanup of cleanups.reverse()) cleanup();
	cleanups = [];
	vi.useRealTimers();
	resetDocPresenceForTests();
});

describe('the hold started by a comment change frame', () => {
	it.each([['before', true], ['after', false]])('a reload registered %s the runtime never shows the comment before its reveal', async (_name, reloadFirst) => {
		if (reloadFirst) reloadOnChange();
		cleanups.push(startDocPresence());
		if (!reloadFirst) reloadOnChange();
		attach();
		changeFrame('c1');
		await settle();
		expect(everShown('c1')).toBe(false);
		emit('doc_activity', activityFrame('c1'));
		await vi.advanceTimersByTimeAsync(REVEAL_MS - 1);
		expect(everShown('c1')).toBe(false);
		await vi.advanceTimersByTimeAsync(1);
		expect(shown()).toEqual(['c1']);
	});

	it('a user comment frame names no agent thread, is never held, and shows with its load at once', async () => {
		reloadOnChange();
		cleanups.push(startDocPresence());
		attach();
		emit('doc_comment_changed', { type: 'doc_comment_changed', artifact_id: ART });
		await settle();
		expect(shown()).toEqual(['c1']);
	});

	it('the lane takes over the hold: the wait for the activity frame no longer ends it', async () => {
		reloadOnChange();
		cleanups.push(startDocPresence());
		attach();
		changeFrame('c1');
		await vi.advanceTimersByTimeAsync(1_000);
		emit('doc_activity', activityFrame('c1'));
		await vi.advanceTimersByTimeAsync(5_000);
		expect(everShown('c1')).toBe(false);
		await vi.advanceTimersByTimeAsync(REVEAL_MS);
		expect(shown()).toEqual(['c1']);
	});

	it('a change frame after the activity frame leaves the lane its hold: no wait timer releases it early', async () => {
		reloadOnChange();
		cleanups.push(startDocPresence());
		attach();
		emit('doc_activity', activityFrame('c1'));
		await settle();
		changeFrame('c1');
		await vi.advanceTimersByTimeAsync(5_000);
		expect(everShown('c1')).toBe(false);
		await vi.advanceTimersByTimeAsync(REVEAL_MS);
		expect(shown()).toEqual(['c1']);
	});

	it('releases the comment after five seconds when no activity frame comes', async () => {
		reloadOnChange();
		cleanups.push(startDocPresence());
		attach();
		changeFrame('c1');
		await vi.advanceTimersByTimeAsync(4_999);
		expect(everShown('c1')).toBe(false);
		await vi.advanceTimersByTimeAsync(1);
		await settle();
		expect(shown()).toEqual(['c1']);
	});

	it('a late activity frame moves the lane but does not pull the shown comment back out', async () => {
		reloadOnChange();
		cleanups.push(startDocPresence());
		attach();
		changeFrame('c1');
		await vi.advanceTimersByTimeAsync(5_000);
		await settle();
		expect(shown()).toEqual(['c1']);
		history = [];
		emit('doc_activity', activityFrame('c1'));
		await vi.advanceTimersByTimeAsync(REVEAL_MS + 1_000);
		expect(history.every((ids) => ids.includes('c1'))).toBe(true);
		expect(shown()).toEqual(['c1']);
	});

	it('reduced motion shows the comment at once', async () => {
		reloadOnChange();
		cleanups.push(startDocPresence());
		attach({ reducedMotion: true });
		changeFrame('c1');
		await settle();
		expect(shown()).toEqual(['c1']);
	});

	it('shows the comment at once when nobody is viewing the document', async () => {
		reloadOnChange();
		cleanups.push(startDocPresence());
		attach({ viewing: false });
		changeFrame('c1');
		await settle();
		expect(shown()).toEqual(['c1']);
	});

	it('a frame that names no agent comment holds nothing', async () => {
		reloadOnChange();
		cleanups.push(startDocPresence());
		attach();
		emit('doc_comment_changed', { type: 'doc_comment_changed', artifact_id: ART });
		await settle();
		expect(shown()).toEqual(['c1']);
	});
});
