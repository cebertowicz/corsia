import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { fromStore, writable } from 'svelte/store';
import { get } from 'svelte/store';
import type { ServerComment } from '$lib/api/docs';
import { displayedDocument } from '$lib/ws/presence';
import { docComments, loadComments } from '$lib/stores/docComments';
import { docPresence, recordDocActivity, resetDocPresenceForTests } from '$lib/docPresence';
import { notifyDocPresenceViewingChanged, revealPendingArrival, startDocPresence } from '$lib/docPresenceRuntime';
import { commentArrivalDelay, selectSweepMs } from '$lib/typingPace';
import DocPresenceStatus from './DocPresenceStatus.svelte';

const wire = vi.hoisted(() => ({ rows: [] as unknown[], failNext: false }));
vi.mock('$lib/api/docs', async (original) => ({
	...await original<typeof import('$lib/api/docs')>(),
	fetchComments: async () => {
		if (wire.failNext) { wire.failNext = false; throw new Error('offline'); }
		return { comments: wire.rows, feedback: { open_count: 0, owner_label: null, grace_closes_at: null, undelivered_batches: [] } };
	},
}));

const ART = 'art';
const artifact = writable<string>(ART);
let component: ReturnType<typeof mount> | undefined;
let stop: (() => void) | undefined;

function serverComment(id: string, textLength: number, from: number, to: number, author = 'agent'): ServerComment {
	return {
		id, artifact_id: ART, parent_id: null, author, author_kind: author === 'you' ? 'user' : 'agent', body: 'c'.repeat(textLength),
		anchor_text: 'x', from_off: from, to_off: to, review: 0, resolved: 0, acked: 0, orphaned: false,
		created_at: '2026-10-08T20:00:00Z', updated_at: '2026-10-08T20:00:00Z', replies: [],
	};
}

/** The agent writes the comment on the server; the page loads it at once on the change frame, as in production. */
function comment(id: string, textLength: number, from: number, to: number) {
	wire.rows = [...wire.rows, serverComment(id, textLength, from, to)];
}

function post(thread: string, id: string, atMs: number, kind: 'comment' | 'suggestion' = 'comment') {
	recordDocActivity({
		type: 'doc_activity', artifact_id: ART, thread_id: thread, agent: thread, kind,
		span: { from: 0, to: 1 }, comment_id: id, revision: null, at: new Date(atMs).toISOString(),
	});
}

const agent = (thread: string) => get(docPresence).artifacts[ART].agents[thread];
const shown = () => (get(docComments)[ART] ?? []).map((row) => row.id);
const settle = () => vi.advanceTimersByTimeAsync(0);
/** doc_comment_changed: the viewer reloads the document comments. */
const changeFrame = async () => { await loadComments(ART); await settle(); };

function mountStatus() {
	const id = fromStore(artifact);
	component = mount(DocPresenceStatus, {
		target: document.body,
		props: {
			target: { instanceId: 'tab', path: 'p.md', name: 'p.md', projectId: null, threadId: 'owner', artifactId: ART },
			get artifactId() { return id.current; }, docState: 'ready', onPrepareCleanReload: async () => null, onFeedback: () => {},
		},
	});
	flushSync();
}

function view(visible = true) {
	displayedDocument.set(visible ? { threadId: 'owner', value: { canShow: true, showing: ART } } : null);
}

beforeEach(() => {
	vi.useFakeTimers();
	vi.setSystemTime(new Date('2026-10-08T20:00:00Z'));
	resetDocPresenceForTests();
	wire.rows = []; wire.failNext = false;
	docComments.set({});
	artifact.set(ART);
	view();
	stop = startDocPresence();
	mountStatus();
});

afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	stop?.();
	vi.useRealTimers();
	vi.unstubAllGlobals();
	resetDocPresenceForTests();
	document.body.innerHTML = '';
});

describe('comment lanes', () => {
	it('row 9: the anchor highlights in the agent colour and the whole comment appears after its select sweep and hold', async () => {
		comment('c1', 100, 10, 20);
		post('agent-a', 'c1', Date.now());
		await settle();
		const lead = agent('agent-a');
		expect(lead.highlightSpan).toEqual({ from: 10, to: 20 });
		expect(lead.highlightKind).toBe('comment');
		expect(lead.lastSpan).toEqual({ from: 10, to: 20 });
		await vi.advanceTimersByTimeAsync(commentArrivalDelay(10, 100) - 1);
		expect(shown()).toEqual([]);
		await vi.advanceTimersByTimeAsync(1);
		expect(shown()).toEqual(['c1']);
		expect(agent('agent-a').highlightSpan).toBeNull();
	});

	it('row 9: a long comment holds five seconds at most after its select sweep', async () => {
		comment('long', 4_000, 0, 5);
		post('agent-a', 'long', Date.now());
		await settle();
		await vi.advanceTimersByTimeAsync(commentArrivalDelay(5, 4_000) - 1);
		expect(commentArrivalDelay(5, 4_000)).toBe(selectSweepMs(5) + 5_000);
		expect(shown()).toEqual([]);
		await vi.advanceTimersByTimeAsync(1);
		expect(shown()).toEqual(['long']);
	});

	it('row 9: agents comment in parallel, so a slow comment never holds another agent', async () => {
		comment('slow', 4_000, 0, 5); comment('quick', 5, 30, 35);
		post('agent-a', 'slow', Date.now()); post('agent-b', 'quick', Date.now());
		await settle();
		await vi.advanceTimersByTimeAsync(commentArrivalDelay(5, 5));
		expect(shown()).toEqual(['quick']);
		expect(agent('agent-a').highlightSpan).toEqual({ from: 0, to: 5 });
	});

	it('row 10: a burst arrives in each agent order, none showing before its turn', async () => {
		comment('a1', 100, 0, 5); comment('a2', 5, 10, 15); comment('b1', 5, 20, 25);
		const start = Date.now();
		post('agent-a', 'a1', start); post('agent-a', 'a2', start + 1); post('agent-b', 'b1', start);
		await settle();
		expect(shown()).toEqual([]);
		expect(agent('agent-a').highlightSpan).toEqual({ from: 0, to: 5 });
		await vi.advanceTimersByTimeAsync(commentArrivalDelay(5, 5));
		expect(shown()).toEqual(['b1']);
		await vi.advanceTimersByTimeAsync(commentArrivalDelay(5, 100) - commentArrivalDelay(5, 5));
		expect(shown().sort()).toEqual(['a1', 'b1']);
		expect(agent('agent-a').highlightSpan).toEqual({ from: 10, to: 15 });
		await vi.advanceTimersByTimeAsync(commentArrivalDelay(5, 5));
		expect(shown().sort()).toEqual(['a1', 'a2', 'b1']);
	});

	it('row 9a: clicking a pending anchor shows its comment at once', async () => {
		comment('c1', 100, 10, 20);
		post('agent-a', 'c1', Date.now());
		await settle();
		expect(shown()).toEqual([]);
		revealPendingArrival(ART, 'agent-a');
		await settle();
		expect(shown()).toEqual(['c1']);
	});

	it('reduced motion shows the comment at once', async () => {
		vi.stubGlobal('matchMedia', () => ({ matches: true }));
		comment('c1', 100, 10, 20);
		post('agent-a', 'c1', Date.now());
		await settle();
		expect(shown()).toEqual(['c1']);
	});
});

describe('the hold on a pending comment', () => {
	it('a document comments reload during the wait does not show it, and the reveal does', async () => {
		comment('c1', 100, 10, 20);
		post('agent-a', 'c1', Date.now());
		await settle();
		await changeFrame();
		expect(shown()).toEqual([]);
		await vi.advanceTimersByTimeAsync(commentArrivalDelay(10, 100));
		expect(shown()).toEqual(['c1']);
		await changeFrame();
		expect(shown()).toEqual(['c1']);
	});

	it('a comment already loaded before its arrival frame is set aside until its turn', async () => {
		comment('c1', 100, 10, 20);
		await changeFrame();
		expect(shown()).toEqual(['c1']);
		post('agent-a', 'c1', Date.now());
		await settle();
		expect(shown()).toEqual([]);
		await vi.advanceTimersByTimeAsync(commentArrivalDelay(10, 100));
		expect(shown()).toEqual(['c1']);
	});

	it('a failed arrival releases the hold and the comment shows', async () => {
		comment('c1', 100, 10, 20);
		wire.failNext = true;
		post('agent-a', 'c1', Date.now());
		await vi.advanceTimersByTimeAsync(2_000);
		expect(shown()).toEqual(['c1']);
	});

	it('the viewer leaving the document releases it at once', async () => {
		comment('c1', 100, 10, 20);
		post('agent-a', 'c1', Date.now());
		await settle();
		view(false);
		notifyDocPresenceViewingChanged(ART);
		await settle();
		expect(shown()).toEqual(['c1']);
	});

	it('a queue that pauses because the viewer left releases it without any notice', async () => {
		comment('c1', 100, 10, 20); comment('c2', 5, 30, 35);
		post('agent-a', 'c1', Date.now()); post('agent-a', 'c2', Date.now() + 1);
		await settle();
		view(false);
		await vi.advanceTimersByTimeAsync(commentArrivalDelay(10, 100));
		expect(shown().sort()).toEqual(['c1', 'c2']);
	});

	it('an arrival that finds nobody viewing is never held', async () => {
		view(false);
		comment('c1', 100, 10, 20);
		post('agent-a', 'c1', Date.now());
		await settle();
		await changeFrame();
		expect(shown()).toEqual(['c1']);
	});

	it('the surface detaching releases it', async () => {
		comment('c1', 100, 10, 20);
		post('agent-a', 'c1', Date.now());
		await settle();
		await unmount(component!);
		component = undefined;
		await settle();
		expect(shown()).toEqual(['c1']);
	});

	it('the artifact changing releases it', async () => {
		comment('c1', 100, 10, 20);
		post('agent-a', 'c1', Date.now());
		await settle();
		artifact.set('other');
		flushSync();
		await settle();
		expect(shown()).toEqual(['c1']);
	});

	it('stopping document presence releases it', async () => {
		comment('c1', 100, 10, 20);
		post('agent-a', 'c1', Date.now());
		await settle();
		stop?.();
		stop = undefined;
		await settle();
		expect(shown()).toEqual(['c1']);
	});

	it('the second comment of a burst is held from enqueue, not from its turn', async () => {
		comment('a1', 100, 0, 5); comment('a2', 5, 10, 15);
		post('agent-a', 'a1', Date.now()); post('agent-a', 'a2', Date.now() + 1);
		await settle();
		await changeFrame();
		expect(shown()).toEqual([]);
	});

	it('an agent suggestion is held until its turn, and the user own comments never are', async () => {
		wire.rows = [serverComment('s1', 50, 0, 5), serverComment('u1', 20, 6, 9, 'you')];
		post('agent-a', 's1', Date.now(), 'suggestion');
		await settle();
		await changeFrame();
		expect(shown()).toEqual(['u1']);
		await vi.advanceTimersByTimeAsync(commentArrivalDelay(5, 50));
		expect(shown().sort()).toEqual(['s1', 'u1']);
	});
});
