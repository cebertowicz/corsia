import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ServerComment } from '$lib/api/docs';
import { recordDocActivity, resetDocPresenceForTests } from './docPresence';
import { attachDocPresenceSurface, startDocPresence } from './docPresenceRuntime';
import { reloadAfterReconnect } from './docViewerOperations';
import { commentLoads, docComments, loadComments, noteFramesBack, noteFramesLost } from './stores/docComments';
import { prepareCommentArrival, revealCommentArrival } from './stores/docCommentArrivals';

const wire = vi.hoisted(() => ({ rows: [] as unknown[], fail: false }));
vi.mock('$lib/api/docs', async (original) => ({
	...await original<typeof import('$lib/api/docs')>(),
	fetchComments: async () => {
		if (wire.fail) throw new Error('offline');
		return { comments: wire.rows, feedback: { open_count: 0, owner_label: null, undelivered_batches: [] } };
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

/** A surface whose editor types a suggestion's insert for TYPING_MS; `typed` records each insert it was asked to type. */
function attach(options: { viewing?: boolean; reducedMotion?: boolean } = {}) {
	const typed: Array<string | null | undefined> = [];
	cleanups.push(attachDocPresenceSurface(ART, 'surface', {
		isViewing: () => options.viewing ?? true,
		reducedMotion: () => options.reducedMotion ?? false,
		prepare: async (frame) => ({ chars: 10, payload: await prepareCommentArrival(ART, frame.comment_id) }),
		select: (_frame, prepared) => {
			typed.push((prepared.payload as Awaited<ReturnType<typeof prepareCommentArrival>>).comment?.replacement);
			return new Promise((resolve) => setTimeout(() => resolve('ended'), TYPING_MS));
		},
		reveal: (_frame, payload) => revealCommentArrival(ART, payload as Awaited<ReturnType<typeof prepareCommentArrival>>),
	}));
	return typed;
}

/** The canvas had loaded its comments (nothing yet), then the socket dropped and the reconnect reload runs. */
async function loadedThenReload(rows: ServerComment[]) {
	wire.rows = [];
	await loadComments(ART);
	wire.rows = rows;
	noteFramesLost(ART);
	noteFramesBack(ART);
	await loadComments(ART);
	await vi.advanceTimersByTimeAsync(0);
}

beforeEach(() => {
	vi.useFakeTimers();
	wire.fail = false;
	resetDocPresenceForTests();
	docComments.set({});
	commentLoads.set({});
	cleanups.push(startDocPresence());
});

afterEach(() => {
	for (const cleanup of cleanups.reverse()) cleanup();
	cleanups = [];
	vi.useRealTimers();
	resetDocPresenceForTests();
});

describe('a reload that finds a suggestion this canvas never showed', () => {
	it('types it through the arrival lane, withholding the card until the typing ends', async () => {
		const typed = attach();
		await loadedThenReload([suggestion('s1')]);
		expect(typed).toEqual(['a longer replacement']);
		expect(shown()).toEqual([]);
		await vi.advanceTimersByTimeAsync(TYPING_MS + 100);
		expect(shown()).toEqual(['s1']);
	});

	it('does not type it again on a later reload, while it is typing or after it showed', async () => {
		const typed = attach();
		await loadedThenReload([suggestion('s1')]);
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
	])('types it once when its real activity frame arrives after the unseen arrival, %s', async (_case, wait) => {
		const typed = attach();
		await loadedThenReload([suggestion('s1')]);
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

	it('leaves a reload on a live socket to the suggestion\'s own frames', async () => {
		const typed = attach();
		wire.rows = [];
		await loadComments(ART);
		wire.rows = [suggestion('s1')];
		await loadComments(ART);
		await vi.advanceTimersByTimeAsync(0);
		expect(typed).toEqual([]);
	});

	it('arrives one a load finds while the socket is down, and stops looking after the first load past the reconnect', async () => {
		const typed = attach();
		wire.rows = [];
		await loadComments(ART);
		noteFramesLost(ART);
		wire.rows = [suggestion('s1')];
		await loadComments(ART);
		await vi.advanceTimersByTimeAsync(0);
		expect(typed).toHaveLength(1);
		noteFramesBack(ART);
		await loadComments(ART);
		wire.rows = [suggestion('s1'), suggestion('s2')];
		await loadComments(ART);
		await vi.advanceTimersByTimeAsync(0);
		expect(typed).toHaveLength(1);
	});

	it('keeps looking after a reconnect load that fails', async () => {
		const typed = attach();
		wire.rows = [];
		await loadComments(ART);
		noteFramesLost(ART);
		noteFramesBack(ART);
		wire.fail = true;
		await expect(loadComments(ART)).rejects.toThrow('offline');
		wire.fail = false;
		wire.rows = [suggestion('s1')];
		await loadComments(ART);
		wire.rows = [suggestion('s1'), suggestion('s2')];
		await loadComments(ART);
		await vi.advanceTimersByTimeAsync(0);
		expect(typed).toEqual(['a longer replacement']);
	});

	it.each([
		['another document opens', (tracker: ReturnType<typeof reloadAfterReconnect>, open: (id: string) => void) => { open('other'); tracker.track('disconnected'); tracker.track('connected'); }],
		['the viewer closes', (tracker: ReturnType<typeof reloadAfterReconnect>) => tracker.close()],
	])('stops looking on a live socket when %s during a drop', async (_case, leave) => {
		const typed = attach();
		wire.rows = [];
		await loadComments(ART);
		let open = ART;
		const tracker = reloadAfterReconnect(() => open, () => undefined);
		tracker.track('disconnected');
		leave(tracker, (id) => { open = id; });
		wire.rows = [suggestion('s1')];
		await loadComments(ART);
		wire.rows = [suggestion('s1'), suggestion('s2')];
		await loadComments(ART);
		await loadComments('other');
		await vi.advanceTimersByTimeAsync(TYPING_MS * 3);
		expect(typed).toHaveLength(1);
		expect(shown().sort()).toEqual(['s1', 's2']);
	});

	it('leaves a suggestion already shown alone', async () => {
		const typed = attach();
		wire.rows = [suggestion('s1')];
		await loadComments(ART);
		noteFramesLost(ART);
		noteFramesBack(ART);
		await loadComments(ART);
		expect(typed).toEqual([]);
		expect(shown()).toEqual(['s1']);
	});

	it('shows it at once when the document is not in view', async () => {
		const typed = attach({ viewing: false });
		await loadedThenReload([suggestion('s1')]);
		expect(typed).toEqual([]);
		expect(shown()).toEqual(['s1']);
	});

	it('shows it at once under reduced motion, as its frames would have been', async () => {
		const typed = attach({ reducedMotion: true });
		await loadedThenReload([suggestion('s1')]);
		expect(typed).toEqual([]);
		expect(shown()).toEqual(['s1']);
	});

	it('does not arrive a first load, a plain comment, a user comment or a decided suggestion', async () => {
		const typed = attach();
		wire.rows = [suggestion('first')];
		await loadComments(ART);
		noteFramesLost(ART);
		noteFramesBack(ART);
		wire.rows = [
			suggestion('first'),
			suggestion('plain', { replacement: null }),
			suggestion('user', { author_kind: 'user', author_thread_id: null }),
			suggestion('decided', { decision: 'accepted' }),
		];
		await loadComments(ART);
		await vi.advanceTimersByTimeAsync(0);
		expect(typed).toEqual([]);
		expect(shown().sort()).toEqual(['decided', 'first', 'plain', 'user']);
	});
});
