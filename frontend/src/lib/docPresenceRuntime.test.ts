import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { docPresence, resetDocPresenceForTests, type DocActivityFrame } from './docPresence';
import {
	attachDocPresenceSurface,
	notifyDocPresenceViewingChanged,
	startDocPresence,
} from './docPresenceRuntime';
import { emit } from './ws/events';
import { commentArrivalDelay } from './typingPace';
import type { SelectOutcome } from './cm/typed-reveal-arrival';

function frame(threadId = 'agent-1'): DocActivityFrame {
	return {
		type: 'doc_activity',
		artifact_id: 'artifact-1',
		thread_id: threadId,
		agent: 'Documents',
		kind: 'edit',
		span: { from: 2, to: 8 },
		comment_id: null,
		revision: 'r2',
		at: '2026-09-30T05:00:00Z',
	};
}

let cleanups: Array<() => void> = [];

beforeEach(() => {
	vi.useFakeTimers();
	resetDocPresenceForTests();
});

afterEach(() => {
	for (const cleanup of cleanups.reverse()) cleanup();
	cleanups = [];
	vi.useRealTimers();
	resetDocPresenceForTests();
});

describe('document presence runtime', () => {
	it('carries a websocket frame through prepare, highlight, reveal and clear', async () => {
		const events: string[] = [];
		cleanups.push(startDocPresence());
		cleanups.push(attachDocPresenceSurface('artifact-1', 'surface-1', {
			isViewing: () => true,
			reducedMotion: () => true,
			prepare: async (arrival) => {
				events.push(`prepare:${arrival.thread_id}`);
				return { chars: 6, payload: 'loaded' };
			},
			reveal: async (arrival, payload) => {
				events.push(`reveal:${arrival.thread_id}:${payload}`);
			},
		}));

		emit('doc_activity', frame());
		await vi.waitFor(() => expect(events).toContain('reveal:agent-1:loaded'));
		expect(get(docPresence).artifacts['artifact-1'].agents['agent-1'].highlightSpan).toEqual({ from: 2, to: 8 });
		await vi.advanceTimersByTimeAsync(1_500);
		expect(get(docPresence).artifacts['artifact-1'].agents['agent-1'].highlightSpan).toBeNull();
	});

	it('holds arrivals off-view and drains them immediately when the document returns', async () => {
		let viewing = false;
		const events: string[] = [];
		cleanups.push(startDocPresence());
		cleanups.push(attachDocPresenceSurface('artifact-1', 'surface-1', {
			isViewing: () => viewing,
			prepare: async (arrival) => ({ chars: 500, payload: arrival.thread_id }),
			reveal: async (_arrival, payload) => { events.push(String(payload)); },
		}));

		emit('doc_activity', frame('agent-a'));
		emit('doc_activity', frame('agent-b'));
		await Promise.resolve();
		expect(events).toEqual([]);

		viewing = true;
		notifyDocPresenceViewingChanged('artifact-1');
		await vi.waitFor(() => expect(events).toEqual(['agent-a', 'agent-b']));
		expect(vi.getTimerCount()).toBeGreaterThan(0);
	});

	it('pulses recent activity even when the frame has no document span', async () => {
		cleanups.push(startDocPresence());
		cleanups.push(attachDocPresenceSurface('artifact-1', 'surface-1', {
			isViewing: () => true,
			reducedMotion: () => true,
			prepare: async () => ({ chars: 0 }),
			reveal: async () => {},
		}));
		const noSpan = frame();
		noSpan.span = null;
		emit('doc_activity', noSpan);
		await vi.waitFor(() => {
			const agent = get(docPresence).artifacts['artifact-1'].agents['agent-1'];
			expect(agent.lastSpan).toBeNull();
			expect(agent.arrivalActive).toBe(true);
		});
	});

	it('lets a surface suppress an unsafe highlight while retaining the latest cursor', async () => {
		cleanups.push(startDocPresence());
		cleanups.push(attachDocPresenceSurface('artifact-1', 'surface-1', {
			isViewing: () => true,
			reducedMotion: () => true,
			prepare: async () => ({ chars: 0, highlightSpan: null }),
			reveal: async () => {},
		}));

		emit('doc_activity', frame());
		await vi.waitFor(() => {
			const agent = get(docPresence).artifacts['artifact-1'].agents['agent-1'];
			expect(agent.lastSpan).toEqual({ from: 2, to: 8 });
			expect(agent.highlightSpan).toBeNull();
			expect(agent.arrivalActive).toBe(true);
		});
		await vi.advanceTimersByTimeAsync(1_500);
		expect(get(docPresence).artifacts['artifact-1'].agents['agent-1'].arrivalActive).toBe(false);
	});

	describe('agent comment: the editor select is the only clock', () => {
		const commentFrame = (threadId = 'agent-1', artifactId = 'artifact-1'): DocActivityFrame => ({ ...frame(threadId), artifact_id: artifactId, kind: 'comment', comment_id: 'c-' + threadId, span: { from: 4, to: 15 } });
		const highlightOf = (artifactId = 'artifact-1', threadId = 'agent-1') => get(docPresence).artifacts[artifactId].agents[threadId];

		/** A surface whose editor select settles when the test says so. */
		function surface(events: string[], options: { select?: boolean; body?: number } = {}) {
			const settle: Array<(outcome: SelectOutcome) => void> = [];
			const value = {
				isViewing: () => true,
				reducedMotion: () => false,
				prepare: async () => {
					events.push('prepare');
					return { chars: options.body ?? 100, delayMs: commentArrivalDelay(11, options.body ?? 100), highlightSpan: { from: 4, to: 15 } };
				},
				reveal: async () => { events.push('card'); },
				...(options.select === false ? {} : {
					select: (arrival: DocActivityFrame) => {
						events.push('select:' + arrival.comment_id);
						return new Promise<SelectOutcome>((resolve) => { settle.push(resolve); });
					},
				}),
			};
			return { value, settle };
		}

		it('starts one select for the arrival, draws no second highlight, and shows the card only when the select settles', async () => {
			const events: string[] = [];
			const { value, settle } = surface(events);
			cleanups.push(startDocPresence());
			cleanups.push(attachDocPresenceSurface('artifact-1', 'surface-1', value));
			emit('doc_activity', commentFrame());
			await vi.waitFor(() => expect(events).toEqual(['prepare', 'select:c-agent-1']));
			expect(highlightOf()).toMatchObject({ highlightSpan: null, arrivalActive: true });
			// Well past the longest hold (5.6 s), yet inside the 30 s presence expiry sweep, which would drop this old fixture frame.
			await vi.advanceTimersByTimeAsync(20_000);
			expect(events).toEqual(['prepare', 'select:c-agent-1']);
			settle[0]('ended');
			await vi.advanceTimersByTimeAsync(0);
			expect(events).toEqual(['prepare', 'select:c-agent-1', 'card']);
			expect(highlightOf()).toMatchObject({ highlightSpan: null, arrivalActive: false });
		});

		it('shows the card at once when the select settles interrupted', async () => {
			const events: string[] = [];
			const { value, settle } = surface(events);
			cleanups.push(startDocPresence());
			cleanups.push(attachDocPresenceSurface('artifact-1', 'surface-1', value));
			emit('doc_activity', commentFrame());
			await vi.waitFor(() => expect(settle).toHaveLength(1));
			settle[0]('interrupted');
			await vi.advanceTimersByTimeAsync(0);
			expect(events).toContain('card');
		});

		it('keeps its own highlight and delay when the surface has no editor to run the select', async () => {
			const events: string[] = [];
			const { value } = surface(events, { select: false, body: 100 });
			cleanups.push(startDocPresence());
			cleanups.push(attachDocPresenceSurface('artifact-1', 'surface-1', value));
			emit('doc_activity', commentFrame());
			await vi.waitFor(() => expect(highlightOf().highlightSpan).toEqual({ from: 4, to: 15 }));
			const hold = commentArrivalDelay(11, 100);
			await vi.advanceTimersByTimeAsync(hold - 150);
			expect(events).toEqual(['prepare']);
			await vi.advanceTimersByTimeAsync(300);
			expect(events).toEqual(['prepare', 'card']);
			expect(highlightOf().highlightSpan).toBeNull();
		});

		it('a select that cannot start (null) falls back to the queue delay for that arrival only', async () => {
			const events: string[] = [];
			const { value } = surface(events, { body: 100 });
			cleanups.push(startDocPresence());
			cleanups.push(attachDocPresenceSurface('artifact-1', 'surface-1', { ...value, select: () => null }));
			emit('doc_activity', commentFrame());
			await vi.waitFor(() => expect(highlightOf().highlightSpan).toEqual({ from: 4, to: 15 }));
			await vi.advanceTimersByTimeAsync(commentArrivalDelay(11, 100) + 150);
			expect(events).toEqual(['prepare', 'card']);
		});

		it("one arrival's select settling never moves another agent's or another document's arrival", async () => {
			const events: string[] = [];
			const one = surface(events);
			const two = surface(events);
			cleanups.push(startDocPresence());
			cleanups.push(attachDocPresenceSurface('artifact-1', 'surface-1', one.value));
			cleanups.push(attachDocPresenceSurface('artifact-2', 'surface-2', two.value));
			emit('doc_activity', commentFrame('agent-1'));
			emit('doc_activity', commentFrame('agent-2'));
			emit('doc_activity', commentFrame('agent-1', 'artifact-2'));
			await vi.waitFor(() => expect(one.settle).toHaveLength(2));
			await vi.waitFor(() => expect(two.settle).toHaveLength(1));
			one.settle[0]('interrupted');
			await vi.advanceTimersByTimeAsync(0);
			expect(events.filter((event) => event === 'card')).toHaveLength(1);
			one.settle[1]('ended');
			two.settle[0]('ended');
			await vi.advanceTimersByTimeAsync(0);
			expect(events.filter((event) => event === 'card')).toHaveLength(3);
		});
	});
});
