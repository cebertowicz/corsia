import { documentOpenCapability } from './artifactDocumentOpenCapability';
import { get } from 'svelte/store';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
	AGENT_PRESENT_FOR,
	REDUCED_HIGHLIGHT_FOR,
	ArrivalQueue,
	docPresence,
	isViewing,
	presenceDetail,
	presenceFor,
	prunePresence,
	recordDocActivity,
	resetDocPresenceForTests,
	revealDelay,
	shouldReloadPresenceEdit,
	type DocActivityFrame,
} from './docPresence';

function frame(overrides: Partial<DocActivityFrame> = {}): DocActivityFrame {
	return {
		type: 'doc_activity',
		artifact_id: 'artifact-1',
		thread_id: 'agent-1',
		agent: 'Documents',
		kind: 'edit',
		span: { from: 2, to: 8 },
		comment_id: null,
		revision: 'r2',
		at: '2026-09-30T05:00:00Z',
		...overrides,
	};
}

beforeEach(() => resetDocPresenceForTests());

describe('document presence identity and lifetime', () => {
	it('keeps agents with the same title separate across identity, color, cursor and queue', () => {
		recordDocActivity(frame({ thread_id: 'agent-a', agent: 'Reviewer' }));
		recordDocActivity(frame({ thread_id: 'agent-b', agent: 'Reviewer', span: { from: 10, to: 12 } }));
		const state = get(docPresence).artifacts['artifact-1'];
		expect(Object.keys(state.agents)).toEqual(['agent-a', 'agent-b']);
		expect(state.agents['agent-a'].color).not.toBe(state.agents['agent-b'].color);
		expect(state.agents['agent-a'].lastSpan).toEqual({ from: 2, to: 8 });
		expect(state.agents['agent-b'].lastSpan).toEqual({ from: 10, to: 12 });
		expect(state.pending['agent-a']).toHaveLength(1);
		expect(state.pending['agent-b']).toHaveLength(1);
	});

	it('updates a renamed label without resetting color, cursor or pending order', () => {
		recordDocActivity(frame({ agent: 'Old title', at: '2026-09-30T05:00:00Z' }));
		const before = get(docPresence).artifacts['artifact-1'].agents['agent-1'];
		recordDocActivity(frame({ agent: 'New title', span: null, at: '2026-09-30T05:00:01Z' }));
		const artifact = get(docPresence).artifacts['artifact-1'];
		expect(artifact.agents['agent-1']).toMatchObject({
			name: 'New title',
			color: before.color,
			lastSpan: { from: 2, to: 8 },
		});
		expect(artifact.pending['agent-1'].map((item) => item.at)).toEqual([
			'2026-09-30T05:00:00Z',
			'2026-09-30T05:00:01Z',
		]);
	});

	it('moves one cursor per agent to the latest non-null span', () => {
		recordDocActivity(frame({ span: { from: 1, to: 3 } }));
		recordDocActivity(frame({ kind: 'read', span: null }));
		expect(get(docPresence).artifacts['artifact-1'].agents['agent-1'].lastSpan).toEqual({ from: 1, to: 3 });
		recordDocActivity(frame({ span: { from: 7, to: 9 } }));
		expect(get(docPresence).artifacts['artifact-1'].agents['agent-1'].lastSpan).toEqual({ from: 7, to: 9 });
	});

	it('expires recent presence after five minutes', () => {
		const at = Date.parse('2026-09-30T05:00:00Z');
		recordDocActivity(frame());
		expect(presenceFor('artifact-1', at + AGENT_PRESENT_FOR - 1)).toHaveLength(1);
		prunePresence(at + AGENT_PRESENT_FOR + 1);
		expect(presenceFor('artifact-1', at + AGENT_PRESENT_FOR + 1)).toEqual([]);
	});

	it('describes only evidenced recent activity without saying now', () => {
		recordDocActivity(frame({ kind: 'comment' }));
		const agent = presenceFor('artifact-1', Date.parse('2026-09-30T05:00:20Z'))[0];
		const detail = presenceDetail(agent, Date.parse('2026-09-30T05:00:20Z'));
		expect(detail).toBe('commented 20 s ago');
		expect(detail).not.toContain('now');
	});

	it('drops malformed frames instead of creating presence', () => {
		expect(recordDocActivity({ type: 'doc_activity', artifact_id: 'artifact-1' })).toBe(false);
		expect(get(docPresence).artifacts).toEqual({});
	});
});

describe('document viewing and draft safety', () => {
	const workspace = (activeSurface: 'chat' | 'artifact', extras = {}) => ({
		tabs: [{ instanceId: 'tab-1', kind: 'document' as const, artifactId: 'artifact-1' }],
		activeTabId: 'tab-1', visible: true, activeSurface, ...extras,
	}) as never;
	// The canvas publishes what it actually shows; viewing reads that one signal.
	const shown = (activeSurface: 'chat' | 'artifact', layout: { mode: 'split'; artifactWidth: number } | { mode: 'rail' } | { mode: 'chat-only' }, compact: boolean, extras = {}) => ({
		threadId: 'thread-1',
		value: documentOpenCapability(workspace(activeSurface, extras), layout as never, compact),
	});
	const split = { mode: 'split' as const, artifactWidth: 500 };

	it('is viewing in split layout whichever pane has focus', () => {
		expect(isViewing('artifact-1', 'thread-1', shown('artifact', split, false), 'visible')).toBe(true);
		expect(isViewing('artifact-1', 'thread-1', shown('chat', split, false), 'visible')).toBe(true);
		expect(isViewing('artifact-2', 'thread-1', shown('chat', split, false), 'visible')).toBe(false);
		expect(isViewing('artifact-1', 'thread-1', shown('chat', split, false), 'hidden')).toBe(false);
	});

	it('needs the artifact surface in compact layout and is not viewing in rail or hidden workspace', () => {
		expect(isViewing('artifact-1', 'thread-1', shown('artifact', { mode: 'chat-only' }, true), 'visible')).toBe(true);
		expect(isViewing('artifact-1', 'thread-1', shown('chat', { mode: 'chat-only' }, true), 'visible')).toBe(false);
		expect(isViewing('artifact-1', 'thread-1', shown('chat', { mode: 'rail' }, false), 'visible')).toBe(false);
		expect(isViewing('artifact-1', 'thread-1', shown('artifact', { mode: 'chat-only' }, false, { visible: false }), 'visible')).toBe(false);
		expect(isViewing('artifact-1', 'thread-1', null, 'visible')).toBe(false);
		expect(isViewing('artifact-1', 'thread-2', shown('artifact', split, false), 'visible')).toBe(false);
	});

	it('allows external reload only for a settled clean draft', () => {
		expect(shouldReloadPresenceEdit(undefined)).toBe(true);
		expect(shouldReloadPresenceEdit({ editText: 'same', savedText: 'same', status: 'idle' })).toBe(true);
		expect(shouldReloadPresenceEdit({ editText: 'local', savedText: 'saved', status: 'dirty' })).toBe(false);
		expect(shouldReloadPresenceEdit({ editText: 'same', savedText: 'same', status: 'saving' })).toBe(false);
		expect(shouldReloadPresenceEdit({ editText: 'same', savedText: 'same', status: 'error' })).toBe(false);
	});
});

describe('arrival pacing', () => {
	it('clamps reveal delay at both ends', () => {
		expect(revealDelay(-5)).toBe(300);
		expect(revealDelay(10)).toBe(380);
		expect(revealDelay(10_000)).toBe(2_500);
	});

	it('plays one agent FIFO while separate agents can progress in parallel', async () => {
		const events: string[] = [];
		const gateA = Promise.withResolvers<void>();
		const gateB = Promise.withResolvers<void>();
		const queue = (agent: string, gate: Promise<void>) => new ArrivalQueue<string>({
			isViewing: () => true,
			reducedMotion: () => false,
			prepare: async (value) => ({ value, chars: 0 }),
			highlight: (value) => { events.push(`highlight:${agent}:${value}`); },
			reveal: (value) => { events.push(`reveal:${agent}:${value}`); },
			clear: () => {},
			sleep: async () => gate,
		});
		const a = queue('a', gateA.promise);
		const b = queue('b', gateB.promise);
		a.enqueue('one');
		a.enqueue('two');
		b.enqueue('other');
		const runningA = a.run();
		const runningB = b.run();
		await vi.waitFor(() => expect(events).toEqual(['highlight:a:one', 'highlight:b:other']));
		gateB.resolve();
		await runningB;
		expect(events).toContain('reveal:b:other');
		expect(events).not.toContain('highlight:a:two');
		gateA.resolve();
		await runningA;
		expect(events.filter((event) => event.startsWith('reveal:a'))).toEqual(['reveal:a:one', 'reveal:a:two']);
	});

	it('pauses off-view and drains queued arrivals without replay delay on return', async () => {
		let viewing = false;
		const events: string[] = [];
		const sleeps: number[] = [];
		const queue = new ArrivalQueue<string>({
			isViewing: () => viewing,
			reducedMotion: () => false,
			prepare: async (value) => ({ value, chars: 100 }),
			highlight: (value) => { events.push(`highlight:${value}`); },
			reveal: (value) => { events.push(`reveal:${value}`); },
			clear: () => {},
			sleep: async (ms) => { sleeps.push(ms); },
		});
		queue.enqueue('one');
		queue.enqueue('two');
		await queue.run();
		expect(queue.pending).toBe(2);
		viewing = true;
		await queue.wake();
		expect(events).toEqual(['reveal:one', 'reveal:two']);
		expect(sleeps).toEqual([]);
		expect(queue.pending).toBe(0);
	});

	it('reveals immediately under reduced motion and keeps only a static highlight', async () => {
		const events: string[] = [];
		const sleeps: number[] = [];
		const queue = new ArrivalQueue<string>({
			isViewing: () => true,
			reducedMotion: () => true,
			prepare: async (value) => ({ value, chars: 500 }),
			highlight: (value) => { events.push(`highlight:${value}`); },
			reveal: (value) => { events.push(`reveal:${value}`); },
			clear: (value) => { events.push(`clear:${value}`); },
			sleep: async (ms) => { sleeps.push(ms); },
		});
		queue.enqueue('one');
		await queue.run();
		expect(events).toEqual(['highlight:one', 'reveal:one', 'clear:one']);
		expect(sleeps).toEqual([REDUCED_HIGHLIGHT_FOR]);
	});
});
