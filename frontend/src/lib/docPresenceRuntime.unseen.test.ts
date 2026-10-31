import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ServerComment } from '$lib/api/docs';
import { recordDocActivity, resetDocPresenceForTests } from './docPresence';
import { attachDocPresenceSurface, notifyDocPresenceViewingChanged, startDocPresence } from './docPresenceRuntime';
import { connectionState } from './stores/connection';
import { commentLoads, docComments, loadComments } from './stores/docComments';
import { prepareCommentArrival, revealCommentArrival } from './stores/docCommentArrivals';

/** The server's rows; `held`, when set, keeps the next fetch's answer back until it resolves. */
const wire = vi.hoisted(() => ({ rows: [] as unknown[], fail: false, held: null as Promise<void> | null }));
vi.mock('$lib/api/docs', async (original) => ({
	...await original<typeof import('$lib/api/docs')>(),
	fetchComments: async () => {
		if (wire.fail) throw new Error('offline');
		const rows = wire.rows;
		const held = wire.held;
		wire.held = null;
		if (held) await held;
		return { comments: rows, feedback: { open_count: 0, owner_label: null, undelivered_batches: [] } };
	},
}));

const ART = 'art';
const TYPING_MS = 3_000;
let cleanups: Array<() => void> = [];

const suggestion = (id: string, extra: Partial<ServerComment> = {}): ServerComment => ({
	id, artifact_id: ART, parent_id: null, author: 'agent-a', author_kind: 'agent', author_thread_id: 'agent-a', body: 'Why.',
	anchor_text: 'x', from_off: 1, to_off: 4, replacement: 'a longer replacement', review: 0, resolved: 0, acked: 0, orphaned: false,
	created_at: '2026-10-08T20:00:00Z', updated_at: '2026-10-08T20:00:00Z', replies: [], ...extra,
} as ServerComment);

const shown = () => (get(docComments)[ART] ?? []).map((row) => row.id);
const settle = () => vi.advanceTimersByTimeAsync(0);

/** A surface whose editor types a suggestion's insert for TYPING_MS; `typed` records each insert it was asked to type. */
function attach(options: { viewing?: boolean; reducedMotion?: boolean; textCurrent?: () => boolean } = {}) {
	const typed: Array<string | null | undefined> = [];
	cleanups.push(attachDocPresenceSurface(ART, 'surface', {
		isViewing: () => options.viewing ?? true,
		reducedMotion: () => options.reducedMotion ?? false,
		textCurrent: options.textCurrent,
		prepare: async (frame) => ({ chars: 10, payload: await prepareCommentArrival(ART, frame.comment_id) }),
		select: (_frame, prepared) => {
			typed.push((prepared.payload as Awaited<ReturnType<typeof prepareCommentArrival>>).comment?.replacement);
			return new Promise((resolve) => setTimeout(() => resolve('ended'), TYPING_MS));
		},
		reveal: (_frame, payload) => revealCommentArrival(ART, payload as Awaited<ReturnType<typeof prepareCommentArrival>>),
	}));
	return typed;
}

/** Close the surface attached last, as its viewer would. */
const detachLast = () => cleanups.pop()?.();

/** The canvas loaded its comments, then the app socket dropped. */
async function loadedThenDrop(rows: ServerComment[] = []) {
	wire.rows = rows;
	await loadComments(ART);
	connectionState.set('disconnected');
}

/** The socket comes back with `rows` on the server, and the reconnect reload runs. */
async function reconnect(rows: ServerComment[]) {
	wire.rows = rows;
	connectionState.set('connected');
	await settle();
}

beforeEach(() => {
	vi.useFakeTimers();
	wire.fail = false;
	wire.held = null;
	resetDocPresenceForTests();
	docComments.set({});
	commentLoads.set({});
	connectionState.set('connected');
	cleanups.push(startDocPresence());
});

afterEach(() => {
	for (const cleanup of cleanups.reverse()) cleanup();
	cleanups = [];
	connectionState.set('disconnected');
	vi.useRealTimers();
	resetDocPresenceForTests();
});

describe('a suggestion whose frames a socket drop lost', () => {
	it('types through the arrival lane after the reconnect, withholding the card until the typing ends', async () => {
		const typed = attach();
		await loadedThenDrop();
		await reconnect([suggestion('s1')]);
		expect(typed).toEqual(['a longer replacement']);
		expect(shown()).toEqual([]);
		await vi.advanceTimersByTimeAsync(TYPING_MS + 100);
		expect(shown()).toEqual(['s1']);
	});

	it('waits for the live text to come back before the reconnect reload, so it types', async () => {
		let current = true;
		const typed = attach({ textCurrent: () => current });
		await loadedThenDrop();
		current = false;
		await reconnect([suggestion('s1')]);
		expect(typed).toEqual([]);
		expect(shown()).toEqual([]);
		current = true;
		notifyDocPresenceViewingChanged(ART);
		await settle();
		await settle();
		expect(typed).toEqual(['a longer replacement']);
	});

	it('arrives when a load finds it while the socket is still down', async () => {
		const typed = attach();
		await loadedThenDrop();
		wire.rows = [suggestion('s1')];
		await loadComments(ART);
		await settle();
		expect(typed).toHaveLength(1);
	});

	it('does not type again on a later reload, while it is typing or after it showed', async () => {
		const typed = attach();
		await loadedThenDrop();
		await reconnect([suggestion('s1')]);
		await loadComments(ART);
		await vi.advanceTimersByTimeAsync(TYPING_MS / 2);
		await loadComments(ART);
		expect(typed).toHaveLength(1);
		await vi.advanceTimersByTimeAsync(TYPING_MS);
		await loadComments(ART);
		expect(typed).toHaveLength(1);
		expect(shown()).toEqual(['s1']);
	});

	it.each([
		['while it is typing', 0],
		['after it showed', TYPING_MS + 100],
	])('types once when its real activity frame comes after the reconnect arrival, %s', async (_case, wait) => {
		const typed = attach();
		await loadedThenDrop();
		await reconnect([suggestion('s1')]);
		await vi.advanceTimersByTimeAsync(wait);
		recordDocActivity({
			type: 'doc_activity', artifact_id: ART, thread_id: 'agent-a', agent: 'agent-a', kind: 'suggestion',
			span: { from: 1, to: 4 }, comment_id: 's1', revision: null, at: '2026-10-08T20:00:00Z', call_id: null,
		});
		if (wait) expect(shown()).toEqual(['s1']);
		await vi.advanceTimersByTimeAsync(TYPING_MS * 3);
		expect(typed).toHaveLength(1);
		expect(shown()).toEqual(['s1']);
	});

	it('still types for a surface remounted during the drop', async () => {
		attach();
		await loadedThenDrop();
		detachLast();
		const typed = attach();
		await reconnect([suggestion('s1')]);
		expect(typed).toEqual(['a longer replacement']);
	});

	it('keeps looking after a reconnect reload that fails, until a load succeeds', async () => {
		const typed = attach();
		await loadedThenDrop();
		wire.fail = true;
		await reconnect([]);
		wire.fail = false;
		wire.rows = [suggestion('s1')];
		await loadComments(ART);
		await settle();
		expect(typed).toEqual(['a longer replacement']);
		wire.rows = [suggestion('s1'), suggestion('s2')];
		await loadComments(ART);
		await settle();
		expect(typed).toHaveLength(1);
	});

	it('still types one lost to a second drop when the first reconnect reload answers after it', async () => {
		const typed = attach();
		await loadedThenDrop();
		let answer = () => undefined as void;
		wire.held = new Promise((resolve) => { answer = resolve; });
		await reconnect([]);
		connectionState.set('disconnected');
		await reconnect([suggestion('s1')]);
		answer();
		await settle();
		await settle();
		expect(typed).toEqual(['a longer replacement']);
	});

	it('leaves a suggestion already shown alone', async () => {
		const typed = attach();
		await loadedThenDrop([suggestion('s1')]);
		await reconnect([suggestion('s1')]);
		expect(typed).toEqual([]);
		expect(shown()).toEqual(['s1']);
	});

	it('shows it at once when the document is not in view', async () => {
		const typed = attach({ viewing: false });
		await loadedThenDrop();
		await reconnect([suggestion('s1')]);
		expect(typed).toEqual([]);
		expect(shown()).toEqual(['s1']);
	});

	it('shows it at once under reduced motion, as its frames would have', async () => {
		const typed = attach({ reducedMotion: true });
		await loadedThenDrop();
		await reconnect([suggestion('s1')]);
		expect(typed).toEqual([]);
		expect(shown()).toEqual(['s1']);
	});

	it('does not arrive a first load, a plain comment, a user comment or a decided suggestion', async () => {
		const typed = attach();
		wire.rows = [suggestion('first')];
		connectionState.set('disconnected');
		await loadComments(ART);
		await reconnect([
			suggestion('first'),
			suggestion('plain', { replacement: null }),
			suggestion('user', { author_kind: 'user', author_thread_id: null }),
			suggestion('decided', { decision: 'accepted' }),
		]);
		expect(typed).toEqual([]);
		expect(shown().sort()).toEqual(['decided', 'first', 'plain', 'user']);
	});
});

describe('a reload on a live socket', () => {
	it('leaves a new suggestion to its own frames', async () => {
		const typed = attach();
		wire.rows = [];
		await loadComments(ART);
		wire.rows = [suggestion('s1')];
		await loadComments(ART);
		await settle();
		expect(typed).toEqual([]);
	});

	it('leaves it to its own frames once the reconnect reload has succeeded', async () => {
		const typed = attach();
		await loadedThenDrop();
		await reconnect([]);
		wire.rows = [suggestion('s1')];
		await loadComments(ART);
		await settle();
		expect(typed).toEqual([]);
	});

	it.each([
		['during the drop', 'after', false],
		['during the drop, with loads failing at the reconnect', 'after', true],
		['after its reconnect reload failed', 'before', true],
	])('shows a suggestion whole when a document closed %s is reopened later, as with no drop', async (_case, closed, failing) => {
		attach();
		await loadedThenDrop();
		if (closed === 'after') detachLast();
		wire.fail = failing;
		await reconnect([]);
		wire.fail = false;
		if (closed === 'before') detachLast();
		const typed = attach();
		wire.rows = [suggestion('s1')];
		await loadComments(ART);
		await vi.advanceTimersByTimeAsync(TYPING_MS * 3);
		expect(typed).toEqual([]);
		expect(shown()).toEqual(['s1']);
	});
});
